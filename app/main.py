"""FastAPI application: routes, SSE stream, and lifecycle wiring."""
import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import covers, db, routes_auth, routes_prefs, routes_system, scraper
from .ao3_client import AO3Client, AO3Error
from .auth import AuthMiddleware
from .bootstrap import apply_password_reset, seed_admin
from .config import Settings
from .downloader import (
    METADATA_FILE,
    DownloadManager,
    downloaded_formats,
    load_metadata,
    remove_metadata_entry,
)
from .events import EventBus, current_search_id
from .models import EnqueueRequest, SearchRequest, SearchResponse
from .utils import safe_child

APP_DIR = Path(__file__).parent
SSE_KEEPALIVE_SECONDS = 15
# How often a running search checks whether its browser is still connected.
DISCONNECT_POLL_SECONDS = 1.0
# Pseudo-category addressing files directly in the downloads root (flat mode).
ROOT_CATEGORY = "_root"


def _migrate_metadata(settings: Settings, bus: EventBus) -> None:
    """Move metadata.json out of downloads_dir (pre-config_dir layout).

    Losing it would make the app re-download everything Calibre has already
    imported, so the files are moved rather than left behind.
    """
    sources = [(settings.downloads_dir / METADATA_FILE, settings.config_dir / METADATA_FILE)]
    if settings.downloads_dir.exists():
        for folder in settings.downloads_dir.iterdir():
            if folder.is_dir():
                sources.append((folder / METADATA_FILE, settings.config_dir / folder.name / METADATA_FILE))

    for src, dst in sources:
        if not src.exists() or dst.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # shutil.move, not os.replace: the two volumes are separate mounts.
            shutil.move(str(src), str(dst))
            bus.log("info", f"Moved {src} to {dst} (metadata now lives outside the downloads folder).")
        except OSError as exc:
            # Never let a migration failure take down startup — worst case the
            # old file stays put and dedup starts from an empty record.
            bus.log("warning", f"Could not move {src} to {dst}: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    bus = EventBus()
    client = AO3Client(settings, bus)
    manager = DownloadManager(client, settings, bus)
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    _migrate_metadata(settings, bus)

    db.init_db(settings.db_path)
    seed_admin(settings.db_path)
    apply_password_reset(settings.db_path)
    now = datetime.now(timezone.utc)
    db.purge_expired_sessions(settings.db_path, now.isoformat())
    db.purge_oidc_states(
        settings.db_path, (now - timedelta(seconds=settings.oidc_state_ttl)).isoformat()
    )

    manager.start()

    app.state.settings = settings
    app.state.bus = bus
    app.state.client = client
    app.state.manager = manager
    app.state.templates = Jinja2Templates(directory=APP_DIR / "templates")
    yield
    await manager.stop()
    await client.close()


app = FastAPI(title="FicFetch", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.include_router(routes_auth.router)
app.include_router(routes_system.router)
app.include_router(routes_prefs.router)
# Added last so it runs first: every route above is protected unless it is
# explicitly listed in auth.PUBLIC_PATHS.
app.add_middleware(AuthMiddleware, db_path=Settings.from_env().db_path)


@app.get("/")
async def index(request: Request) -> HTMLResponse:
    """Serve the app shell with a cache-busted app.js URL.

    Without the version parameter a browser can pair freshly served HTML with a
    cached older app.js, leaving new controls wired to nothing.
    """
    script = APP_DIR / "static" / "app.js"
    response = request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        {"user": request.state.user, "csrf_token": request.state.session.csrf_token,
         "app_js_version": int(script.stat().st_mtime)},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/search")
async def api_search(req: SearchRequest, request: Request) -> SearchResponse:
    settings: Settings = request.app.state.settings
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query must not be empty.")
    req.query = query

    max_results = max(1, min(req.max_results, settings.max_results_cap))
    bus: EventBus = request.app.state.bus
    token = current_search_id.set(req.search_id)
    try:
        return await _run_search(request, settings, bus, req, max_results)
    finally:
        current_search_id.reset(token)


async def _run_search(
    request: Request, settings: Settings, bus: EventBus, req: SearchRequest, max_results: int
) -> SearchResponse | Response:
    query = req.query
    bus.log("info", f"Searching {req.search_type} '{query}' (max {max_results} works)...")

    # Starlette never cancels a plain endpoint when its client goes away, so a
    # search the browser gave up on (fetch timeout, proxy timeout, reload) would
    # keep paging AO3 to the end — competing with the next search for the
    # shared rate-limit lock. Run it as a task and cancel it on disconnect.
    task = asyncio.create_task(
        scraper.search(request.app.state.client, settings, bus, req, max_results)
    )
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=DISCONNECT_POLL_SECONDS)
            if done:
                break
            if await request.is_disconnected():
                task.cancel()
                bus.log("warning", f"Search for '{query}' cancelled — the browser disconnected.")
                # Nobody is listening; nginx's "client closed request" code for the access log.
                return Response(status_code=499)
        works, message, truncated = task.result()
    except AO3Error as exc:
        bus.log("error", f"Search failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        # Also covers this handler itself being cancelled (e.g. shutdown).
        task.cancel()

    known = downloaded_formats(settings)
    downloaded = {w.work_id: known[w.work_id] for w in works if w.work_id in known}

    bus.log(
        "info",
        f"Search finished: {len(works)} works for '{query}'"
        + (f" ({len(downloaded)} already in your library)." if downloaded else "."),
    )
    return SearchResponse(works=works, message=message, truncated=truncated, downloaded=downloaded)


@app.post("/api/download")
async def api_download(req: EnqueueRequest, request: Request) -> dict:
    if not req.works:
        raise HTTPException(status_code=400, detail="No works selected.")
    if not req.category.strip():
        raise HTTPException(status_code=400, detail="Category must not be empty.")

    manager: DownloadManager = request.app.state.manager
    # Cover style is a per-user preference, sourced server-side rather than
    # trusted from the request body.
    job = manager.enqueue(
        req.works, req.format, req.category.strip(), request.state.user.cover_style
    )
    return {"job_id": job.job_id, "queued": len(job.items)}


@app.get("/api/cover-preview/{style}")
async def api_cover_preview(style: str) -> Response:
    """Render the sample cover for a style — drives the /preferences gallery."""
    if style not in covers.ALLOWED_STYLES:
        raise HTTPException(status_code=404, detail="Unknown cover style.")
    return Response(
        content=covers.generate_preview(style),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/jobs")
async def api_jobs(request: Request) -> dict:
    manager: DownloadManager = request.app.state.manager
    return manager.snapshot()


@app.get("/api/jobs/{job_id}")
async def api_job_detail(job_id: str, request: Request) -> dict:
    manager: DownloadManager = request.app.state.manager
    job = manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found (jobs do not survive restarts).")
    return job.detail()


def _category_listing(folder: Path, meta_folder: Path, name: str) -> dict:
    """List everything downloaded for a category.

    Driven by metadata rather than the filesystem: Calibre's automatic adding
    removes each file once imported, so a file-only listing would go blank.
    """
    meta = load_metadata(meta_folder)
    by_filename = {entry.get("filename"): (wid, entry) for wid, entry in meta.items()}

    files = []
    on_disk: set[str] = set()
    if folder.exists():
        for f in sorted(folder.iterdir()):
            if not f.is_file() or f.name == METADATA_FILE or f.name.endswith(".part") or f.name.startswith("."):
                continue
            on_disk.add(f.name)
            work_id, entry = by_filename.get(f.name, (None, None))
            files.append(
                {
                    "filename": f.name,
                    "size": f.stat().st_size,
                    "work_id": work_id,
                    "entry": entry,
                    "present": True,
                }
            )

    # Recorded downloads whose file is gone — imported by Calibre, or removed.
    for work_id, entry in meta.items():
        filename = entry.get("filename")
        if not filename or filename in on_disk:
            continue
        files.append(
            {
                "filename": filename,
                "size": None,
                "work_id": work_id,
                "entry": entry,
                "present": False,
            }
        )

    files.sort(key=lambda f: f["filename"].lower())
    return {"name": name, "files": files, "metadata_entries": len(meta)}


@app.get("/api/downloads")
async def api_downloads(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    categories = []

    # Flat-mode files live directly in the downloads root.
    root = _category_listing(settings.downloads_dir, settings.config_dir, ROOT_CATEGORY)
    if root["files"] or root["metadata_entries"]:
        categories.append(root)

    # Union of both trees: a category survives Calibre emptying (or removing)
    # its download folder as long as its metadata is still around.
    names = set()
    for base in (settings.downloads_dir, settings.config_dir):
        if base.exists():
            names.update(p.name for p in base.iterdir() if p.is_dir())

    for name in sorted(names):
        listing = _category_listing(settings.downloads_dir / name, settings.config_dir / name, name)
        if listing["files"] or listing["metadata_entries"]:
            categories.append(listing)

    return {"categories": categories}


def _resolve_library_file(settings: Settings, category: str, filename: str) -> tuple[Path, Path]:
    """Returns (file path in downloads_dir, matching metadata folder in config_dir)."""
    if filename == METADATA_FILE or filename.endswith(".part"):
        raise HTTPException(status_code=400, detail="Invalid path.")
    if category == ROOT_CATEGORY:
        path = safe_child(settings.downloads_dir, filename)
        meta_folder = settings.config_dir
    else:
        path = safe_child(settings.downloads_dir, category, filename)
        meta_folder = safe_child(settings.config_dir, category)
    if path is None or meta_folder is None:
        raise HTTPException(status_code=400, detail="Invalid path.")
    return path, meta_folder


@app.get("/api/downloads/{category}/{filename}")
async def api_download_file(category: str, filename: str, request: Request) -> FileResponse:
    path, _ = _resolve_library_file(request.app.state.settings, category, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@app.delete("/api/downloads/{category}/{filename}")
async def api_delete_file(category: str, filename: str, request: Request) -> dict:
    # async def with no await between metadata load and write: the read-modify-
    # write cannot interleave with the download worker's on the event loop.
    path, meta_folder = _resolve_library_file(request.app.state.settings, category, filename)
    file_removed = path.is_file()
    if file_removed:
        path.unlink()
    metadata_removed = remove_metadata_entry(meta_folder, filename)
    if not file_removed and not metadata_removed:
        raise HTTPException(status_code=404, detail="File not found.")
    bus: EventBus = request.app.state.bus
    bus.log("info", f"Deleted {category}/{filename} (metadata entry removed: {metadata_removed}).")
    return {"deleted": file_removed, "metadata_removed": metadata_removed, "filename": filename}


@app.get("/api/events")
async def api_events(request: Request) -> StreamingResponse:
    bus: EventBus = request.app.state.bus
    manager: DownloadManager = request.app.state.manager

    async def stream():
        q = bus.subscribe()
        try:
            snapshot = json.dumps(manager.snapshot(), ensure_ascii=False)
            yield f"event: snapshot\ndata: {snapshot}\n\n"
            while True:
                try:
                    frame = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_SECONDS)
                    yield frame
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
