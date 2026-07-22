"""FastAPI application: routes, SSE stream, and lifecycle wiring."""
import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import scraper
from .ao3_client import AO3Client, AO3Error
from .config import Settings
from .downloader import METADATA_FILE, DownloadManager, load_metadata, remove_metadata_entry
from .events import EventBus
from .models import EnqueueRequest, SearchRequest, SearchResponse
from .utils import safe_child

APP_DIR = Path(__file__).parent
SSE_KEEPALIVE_SECONDS = 15
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
    manager.start()

    app.state.settings = settings
    app.state.bus = bus
    app.state.client = client
    app.state.manager = manager
    yield
    await manager.stop()
    await client.close()


app = FastAPI(title="AO3 Downloader", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


@app.get("/")
async def index() -> HTMLResponse:
    """Serve the page with a cache-busted app.js URL.

    Without this a browser can pair freshly served HTML with a cached older
    app.js, leaving new controls wired to nothing.
    """
    script = APP_DIR / "static" / "app.js"
    html = (APP_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    html = html.replace("/static/app.js", f"/static/app.js?v={int(script.stat().st_mtime)}")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


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
    bus.log("info", f"Searching {req.search_type} '{query}' (max {max_results} works)...")

    try:
        works, message, truncated = await scraper.search(
            request.app.state.client, settings, bus, req, max_results
        )
    except AO3Error as exc:
        bus.log("error", f"Search failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    bus.log("info", f"Search finished: {len(works)} works for '{query}'.")
    return SearchResponse(works=works, message=message, truncated=truncated)


@app.post("/api/download")
async def api_download(req: EnqueueRequest, request: Request) -> dict:
    if not req.works:
        raise HTTPException(status_code=400, detail="No works selected.")
    if not req.category.strip():
        raise HTTPException(status_code=400, detail="Category must not be empty.")

    manager: DownloadManager = request.app.state.manager
    job = manager.enqueue(req.works, req.format, req.category.strip())
    return {"job_id": job.job_id, "queued": len(job.items)}


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
