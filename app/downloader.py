"""Download queue: a single asyncio worker that drains jobs one work at a time.

One worker is deliberate — the global rate limiter in AO3Client is the
bottleneck by design, so parallel downloads would gain nothing and would race
on files and metadata.json.
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import scraper
from .ao3_client import AO3Client, AO3Error, RestrictedWorkError
from .config import Settings
from .events import EventBus
from .grimmory import booklore_metas, load_grimmory_config, split_subjects
from .models import GrimmoryConfig, ItemStatus, Job, JobItem, Work
from .covers import generate_cover
from .utils import (
    add_epub_cover,
    add_epub_metadata,
    atomic_write_bytes,
    atomic_write_text,
    epub_has_cover,
    epub_subjects,
    sanitize_filename,
)

METADATA_FILE = "metadata.json"


def load_metadata(folder: Path) -> dict:
    meta_path = folder / METADATA_FILE
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def downloaded_formats(settings: Settings) -> dict[str, list[str]]:
    """Map work_id -> formats already downloaded, across every category.

    Deliberately global: the question the search results answer is "do I
    already have this?", regardless of which category it was filed under.
    """
    folders = [settings.config_dir]
    if settings.config_dir.exists():
        folders.extend(p for p in settings.config_dir.iterdir() if p.is_dir())

    result: dict[str, list[str]] = {}
    for folder in folders:
        for work_id, entry in load_metadata(folder).items():
            fmt = entry.get("format")
            if fmt and fmt not in result.setdefault(work_id, []):
                result[work_id].append(fmt)
    return result


def remove_metadata_entry(folder: Path, filename: str) -> bool:
    """Remove the entry claiming `filename`. Load-modify-write with no await in
    between, so it is atomic on the event loop against the worker's writes."""
    metadata = load_metadata(folder)
    key = next((wid for wid, entry in metadata.items() if entry.get("filename") == filename), None)
    if key is None:
        return False
    metadata.pop(key)
    atomic_write_text(folder / METADATA_FILE, json.dumps(metadata, ensure_ascii=False, indent=2))
    return True


class DownloadManager:
    def __init__(self, client: AO3Client, settings: Settings, bus: EventBus) -> None:
        self._client = client
        self._settings = settings
        self._bus = bus
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._jobs: dict[str, Job] = {}
        self._current_progress: dict | None = None
        self._worker_task: asyncio.Task | None = None

    def start(self) -> None:
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    def enqueue(self, works: list[Work], fmt: str, category: str, cover_style: str = "hybrid") -> Job:
        job = Job(
            job_id=uuid.uuid4().hex[:8],
            category=category,
            format=fmt,
            cover_style=cover_style,
            items=[JobItem(work=w) for w in works],
        )
        self._jobs[job.job_id] = job
        self._queue.put_nowait(job)
        self._bus.log("info", f"Queued job {job.job_id}: {len(works)} works as {fmt.upper()} into '{category}'.")
        return job

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def snapshot(self) -> dict:
        return {
            "jobs": [job.summary() for job in self._jobs.values()],
            "current": self._current_progress,
        }

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            job.state = "running"
            try:
                await self._run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one job kill the worker loop
                self._bus.log("error", f"Job {job.job_id} crashed: {exc}")
            finally:
                job.state = "finished"
                self._current_progress = None
                self._queue.task_done()

    async def _run_job(self, job: Job) -> None:
        if self._settings.flat_downloads:
            folder = self._settings.downloads_dir
            meta_folder = self._settings.config_dir
        else:
            sub = sanitize_filename(job.category, fallback="uncategorized")
            folder = self._settings.downloads_dir / sub
            meta_folder = self._settings.config_dir / sub
        folder.mkdir(parents=True, exist_ok=True)
        meta_folder.mkdir(parents=True, exist_ok=True)
        total = len(job.items)
        # Read once per job rather than at startup, so the System → Library
        # integration toggle applies to the next download, not the next restart.
        grimmory = load_grimmory_config(self._settings.db_path)

        for i, item in enumerate(job.items, start=1):
            work = item.work
            self._current_progress = {
                "job_id": job.job_id,
                "current": i,
                "total": total,
                "title": work.title,
            }
            self._bus.publish("progress", self._current_progress)

            path = self._target_path(folder, meta_folder, work, job.format)
            item.filename = path.name

            if path.exists():
                item.status = ItemStatus.skipped
                item.message = "Already exists"
                self._bus.log("info", f"Skipped (already exists): {path.name}")
                self._publish_item(job, item)
                continue

            # The file may be gone because Calibre's automatic adding imported
            # and removed it — metadata.json is the durable memory of what was
            # already downloaded.
            entry = load_metadata(meta_folder).get(work.work_id)
            if entry and entry.get("format") == job.format:
                item.status = ItemStatus.skipped
                item.message = "Already downloaded earlier (file since imported/removed)"
                self._bus.log("info", f"Skipped (downloaded earlier, per metadata): {work.title}")
                self._publish_item(job, item)
                continue

            item.status = ItemStatus.downloading
            try:
                data = await scraper.download_work(self._client, self._settings, work, job.format)
            except RestrictedWorkError:
                item.status = ItemStatus.skipped
                item.message = "Restricted — requires AO3 login"
                self._bus.log("warning", f"Skipped (restricted — login required): {work.title}")
                self._publish_item(job, item)
                continue
            except (AO3Error, httpx.HTTPError) as exc:
                item.status = ItemStatus.error
                item.message = str(exc)
                self._bus.log("error", f"Failed: {work.title} — {exc}")
                self._publish_item(job, item)
                continue

            if job.format == "epub" and (self._settings.epub_tag or grimmory.enabled):
                try:
                    data = self._embed_metadata(data, work, grimmory)
                except Exception as exc:
                    self._bus.log("warning", f"Could not embed tag in EPUB for {work.title}: {exc}")

            if (
                job.format == "epub"
                and self._settings.cover_generation_enabled
                and job.cover_style != "none"
            ):
                # Only when the EPUB brings no cover of its own — never overwrite one.
                try:
                    if not epub_has_cover(data):
                        data = add_epub_cover(data, generate_cover(work, job.cover_style))
                except Exception as exc:
                    self._bus.log("warning", f"Could not generate cover for {work.title}: {exc}")

            atomic_write_bytes(path, data)
            self._update_metadata(meta_folder, work, path.name, job.format)
            item.status = ItemStatus.done
            self._bus.log("info", f"Downloaded: {path.name} ({len(data) // 1024} KB)")
            self._publish_item(job, item)

        counts = job.counts()
        self._bus.publish(
            "job_done",
            {
                "job_id": job.job_id,
                "done": counts["done"],
                "skipped": counts["skipped"],
                "errors": counts["error"],
            },
        )
        self._bus.log(
            "info",
            f"Job {job.job_id} finished — done: {counts['done']}, "
            f"skipped: {counts['skipped']}, errors: {counts['error']}.",
        )

    def _embed_metadata(self, data: bytes, work: Work, grimmory: GrimmoryConfig) -> bytes:
        """Write the configured tag, and — in Grimmory mode — the genre/tag split.

        The split is computed over the subjects AO3 shipped plus the one we are
        about to add, so the tag itself is always classified as a genre.
        """
        subjects = [self._settings.epub_tag] if self._settings.epub_tag else []
        if not grimmory.enabled:
            return add_epub_metadata(data, subjects=subjects)

        _, tags = split_subjects(
            epub_subjects(data) + subjects,
            work,
            epub_tag=self._settings.epub_tag,
            genre_mode=grimmory.genres,
        )
        return add_epub_metadata(
            data,
            subjects=subjects,
            metas=booklore_metas(work, tags, map_rating=grimmory.map_rating),
            drop_subjects=set(tags) if grimmory.trim_subjects else frozenset(),
        )

    def _publish_item(self, job: Job, item: JobItem) -> None:
        self._bus.publish(
            "item_done",
            {
                "job_id": job.job_id,
                "work_id": item.work.work_id,
                "status": item.status.value,
                "filename": item.filename,
                "message": item.message,
            },
        )

    def _target_path(self, folder: Path, meta_folder: Path, work: Work, fmt: str) -> Path:
        author = work.authors[0] if work.authors else "Anonymous"
        if len(work.authors) > 1:
            author += " et al"
        base = sanitize_filename(f"{work.title} - {author}", fallback=f"work_{work.work_id}")
        path = folder / f"{base}.{fmt}"

        # Same title+author from a different work must not overwrite: consult
        # metadata.json and disambiguate with the (globally unique) work id.
        metadata = self._load_metadata(meta_folder)
        claimed_by = next(
            (wid for wid, entry in metadata.items() if entry.get("filename") == path.name),
            None,
        )
        if (claimed_by and claimed_by != work.work_id) or (path.exists() and claimed_by is None):
            path = folder / f"{base} [{work.work_id}].{fmt}"
        return path

    def _load_metadata(self, folder: Path) -> dict:
        metadata = load_metadata(folder)
        if not metadata and (folder / METADATA_FILE).exists():
            self._bus.log("warning", f"Empty or corrupt {METADATA_FILE} in {folder.name} — rebuilding.")
        return metadata

    def _update_metadata(self, folder: Path, work: Work, filename: str, fmt: str) -> None:
        metadata = self._load_metadata(folder)
        metadata[work.work_id] = {
            "title": work.title,
            "authors": work.authors,
            "word_count": work.word_count,
            "tags": work.tags,
            "fandoms": work.fandoms,
            "summary": work.summary,
            "series": work.series,
            "kudos": work.kudos,
            "hits": work.hits,
            "bookmarks": work.bookmarks,
            "chapters": work.chapters,
            "rating": work.rating,
            "complete": work.complete,
            "filename": filename,
            "format": fmt,
            "url": f"https://archiveofourown.org/works/{work.work_id}",
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_text(folder / METADATA_FILE, json.dumps(metadata, ensure_ascii=False, indent=2))
