from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.background import BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from yt_dlp import YoutubeDL

ROOT = Path(__file__).resolve().parent
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "sigma-brewer"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="Sigma Brewer", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
jobs: dict[str, dict] = {}
lock = threading.Lock()
active_job: str | None = None
last_request_by_ip: dict[str, float] = {}

class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    quality: int = Field(default=720, ge=360, le=1080)


def validate_youtube_url(raw: str) -> str:
    parsed = urlparse(raw.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
    if parsed.scheme not in {"http", "https"} or host not in allowed or parsed.username or parsed.password:
        raise HTTPException(400, "Please enter a valid YouTube video URL.")
    return raw.strip()


def progress_hook(job_id: str):
    def hook(data: dict):
        with lock:
            job = jobs.get(job_id)
            if not job:
                return
            if data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                downloaded = data.get("downloaded_bytes", 0)
                job["progress"] = round(min(99.0, downloaded * 100 / total), 1) if total else None
                job["downloaded"] = downloaded
                job["total"] = total
                job["speed"] = data.get("speed")
                job["eta"] = data.get("eta")
                job["phase"] = "Downloading"
            elif data.get("status") == "finished":
                job["progress"] = 99.0
                job["phase"] = "Processing video and audio"
    return hook


def run_download(job_id: str, url: str, quality: int):
    global active_job
    folder = DOWNLOAD_ROOT / job_id
    folder.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best",
        "outtmpl": str(folder / "%(title).180B [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "progress_hooks": [progress_hook(job_id)],
        "quiet": True,
        "no_warnings": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "continuedl": True,
        "overwrites": True,
        "socket_timeout": 30,
        # Enable yt-dlp's supported JavaScript challenge solver for YouTube extraction.
        "js_runtimes": {"deno": {}},
        "restrictfilenames": True,
        "max_filesize": 12 * 1024 * 1024 * 1024,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
        if not files:
            raise RuntimeError("The download finished but no video file was produced.")
        output = max(files, key=lambda p: p.stat().st_size)
        with lock:
            jobs[job_id].update({"status": "ready", "phase": "Ready", "progress": 100, "file": str(output), "filename": output.name, "title": info.get("title", output.stem)})
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raw_error = str(exc)
        if "Sign in to confirm" in raw_error or "not a bot" in raw_error.lower():
            user_error = (
                "YouTube is requiring an authenticated session for requests from this server. "
                "This is a YouTube-side access check, not a video-quality or file-size problem. "
                "The server-side JavaScript runtime is enabled; if this continues, this hosting IP "
                "may not be allowed to access the video. No account cookies are stored by Sigma Brewer."
            )
        else:
            user_error = raw_error[:500]
        with lock:
            if job_id in jobs:
                jobs[job_id].update({"status": "error", "phase": "Failed", "error": user_error})
    finally:
        with lock:
            active_job = None


@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")

@app.get("/health")
def health():
    return {"ok": True, "service": "sigma-brewer"}

@app.post("/api/download")
async def start_download(payload: DownloadRequest, request: Request):
    global active_job
    url = validate_youtube_url(payload.url)
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    with lock:
        if now - last_request_by_ip.get(client_ip, 0) < 10:
            raise HTTPException(429, "Please wait a few seconds before starting another download.")
        if active_job is not None:
            raise HTTPException(429, "The downloader is busy with another video. Try again shortly.")
        job_id = uuid.uuid4().hex
        active_job = job_id
        last_request_by_ip[client_ip] = now
        jobs[job_id] = {"status": "working", "phase": "Starting", "progress": 0, "created": now}
    asyncio.create_task(asyncio.to_thread(run_download, job_id, url, payload.quality))
    return {"id": job_id}

@app.get("/api/status/{job_id}")
def get_status(job_id: str):
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Download job not found.")
        return {k: v for k, v in job.items() if k not in {"file", "created"}}

@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "ready" or not job.get("file"):
            raise HTTPException(409, "The video is not ready yet.")
        path = Path(job["file"])
        filename = job.get("filename", "video.mp4")
    if not path.exists():
        raise HTTPException(404, "The temporary video file has expired.")
    def cleanup():
        shutil.rmtree(path.parent, ignore_errors=True)
        with lock:
            jobs.pop(job_id, None)
    return FileResponse(path, filename=filename, media_type="application/octet-stream", background=BackgroundTask(cleanup))

@app.on_event("startup")
def clean_old_jobs():
    now = time.time()
    for folder in DOWNLOAD_ROOT.iterdir():
        try:
            if folder.is_dir() and now - folder.stat().st_mtime > 6 * 3600:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass
