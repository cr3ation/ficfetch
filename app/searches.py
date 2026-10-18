"""Searches as short-lived background jobs.

A search can take minutes: pages are 3-6 s apart, a big AO3 tag listing can
take 100 s to render, and 5xx/429 retries back off for longer still. Holding
one HTTP request open for all of that loses to any browser or reverse-proxy
timeout, so POST /api/search only starts a job here; progress streams over
SSE and the browser fetches the result once `search_done` arrives.
"""
import asyncio
import time
from dataclasses import dataclass, field

from . import scraper
from .ao3_client import AO3Client, AO3Error
from .config import Settings
from .downloader import downloaded_formats
from .events import EventBus, current_search_id
from .models import SearchRequest, SearchResponse

# Finished jobs stay fetchable this long — enough for an SSE reconnect to
# recover a missed `search_done`, short enough that results don't pile up.
RESULT_TTL_SECONDS = 600


class DuplicateSearchError(Exception):
    pass


@dataclass
class SearchJob:
    search_id: str
    user_id: int
    query: str
    status: str = "running"  # running | done | error | cancelled
    response: SearchResponse | None = None
    error: str | None = None
    finished_at: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def detail(self) -> dict:
        return {
            "search_id": self.search_id,
            "query": self.query,
            "status": self.status,
            "error": self.error,
            "result": self.response.model_dump() if self.response else None,
        }


class SearchManager:
    def __init__(self, client: AO3Client, settings: Settings, bus: EventBus) -> None:
        self._client = client
        self._settings = settings
        self._bus = bus
        self._jobs: dict[str, SearchJob] = {}

    def start(self, user_id: int, req: SearchRequest, max_results: int) -> SearchJob:
        self._purge_finished()
        if req.search_id in self._jobs:
            raise DuplicateSearchError(req.search_id)
        job = SearchJob(search_id=req.search_id, user_id=user_id, query=req.query)
        self._jobs[job.search_id] = job
        # The task copies the current context, so every event it publishes —
        # scraper and AO3Client logs included — carries this search's id.
        token = current_search_id.set(job.search_id)
        try:
            job.task = asyncio.create_task(self._run(job, req, max_results))
        finally:
            current_search_id.reset(token)
        return job

    def get(self, search_id: str, user_id: int) -> SearchJob | None:
        job = self._jobs.get(search_id)
        return job if job and job.user_id == user_id else None

    def cancel(self, search_id: str, user_id: int) -> SearchJob | None:
        job = self.get(search_id, user_id)
        if job and job.task and not job.task.done():
            job.task.cancel()
        return job

    async def stop(self) -> None:
        tasks = [job.task for job in self._jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job: SearchJob, req: SearchRequest, max_results: int) -> None:
        settings = self._settings
        query = req.query
        self._bus.log("info", f"Searching {req.search_type} '{query}' (max {max_results} works)...")
        try:
            works, message, truncated = await scraper.search(
                self._client, settings, self._bus, req, max_results
            )
            known = downloaded_formats(settings)
            downloaded = {w.work_id: known[w.work_id] for w in works if w.work_id in known}
            job.response = SearchResponse(
                works=works, message=message, truncated=truncated, downloaded=downloaded
            )
            job.status = "done"
            self._bus.log(
                "info",
                f"Search finished: {len(works)} works for '{query}'"
                + (f" ({len(downloaded)} already in your library)." if downloaded else "."),
            )
        except asyncio.CancelledError:
            job.status = "cancelled"
            self._bus.log("warning", f"Search for '{query}' cancelled.")
            raise
        except AO3Error as exc:
            job.status = "error"
            job.error = str(exc)
            self._bus.log("error", f"Search failed: {exc}")
        except Exception as exc:  # a crash must still end the job, or the browser waits forever
            job.status = "error"
            job.error = f"Unexpected error: {exc}"
            self._bus.log("error", f"Search crashed: {exc}")
        finally:
            job.finished_at = time.monotonic()
            self._bus.publish("search_done", {"status": job.status})

    def _purge_finished(self) -> None:
        cutoff = time.monotonic() - RESULT_TTL_SECONDS
        for search_id in [
            sid for sid, job in self._jobs.items() if job.finished_at and job.finished_at < cutoff
        ]:
            del self._jobs[search_id]
