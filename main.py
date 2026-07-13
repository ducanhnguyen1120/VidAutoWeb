"""
VidAuto Web — FastAPI backend
"""
import asyncio
import json
import shutil
import subprocess as _sp
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

try:
    from pillow_heif import register_heif_opener
    from PIL import Image as _PILImage
    register_heif_opener()
    _HEIC_SUPPORTED = True
except ImportError:
    _HEIC_SUPPORTED = False
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))
from job_queue import (
    create_job, get_pending, claim, update_progress, add_log,
    finish, cancel, get_job, latest_for_project, logs_since, list_jobs, cleanup_stale,
)
from project_manager import Project, PROJECTS_DIR
from template_manager import Template, TEMPLATES_DIR
from video_engine import (
    VideoEngine, DEFAULT_CONFIG,
    IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS,
    sort_images_smart,
)

import threading as _threading

app = FastAPI(title="VidAuto Web")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# Worker heartbeat
_worker_last_seen: float = 0.0


def _cleanup_loop():
    """Background thread: cancel stale 'cancelling' jobs sau 60s."""
    import time as _time
    while True:
        _time.sleep(30)
        try:
            cleanup_stale(timeout_seconds=60)
        except Exception:
            pass

_threading.Thread(target=_cleanup_loop, daemon=True).start()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_project(safe: str) -> Project:
    path = PROJECTS_DIR / safe
    if not path.exists():
        raise HTTPException(404, f"Project '{safe}' không tồn tại")
    try:
        return Project.load_from(path)
    except Exception as e:
        raise HTTPException(500, str(e))


def _count_rendered(p: Project) -> int:
    dirs_to_scan: set[Path] = set()
    if p.output_dir.exists():
        dirs_to_scan.add(p.output_dir)
    for job in list_jobs(project_safe=p.safe, limit=500):
        od = job.get("output_dir", "")
        if od:
            dirs_to_scan.add(Path(od))
    total = 0
    for d in dirs_to_scan:
        try:
            total += sum(1 for _ in d.rglob("*.mp4"))
        except Exception:
            pass
    return total


def _project_dict(p: Project) -> dict:
    job = latest_for_project(p.safe)
    return {
        "name": p.name,
        "safe": p.safe,
        "rendered_count": _count_rendered(p),
        "image_count": sum(1 for ip in p.image_paths if Path(ip).exists()),
        "updated_at": p.updated_at,
        "config": {**DEFAULT_CONFIG, **p.config},
        "fixed_video": p.fixed_video.name if p.fixed_video else None,
        "music": p.music.name if p.music else None,
        "voice": p.voice.name if p.voice else None,
        "overlay": p.overlay.name if p.overlay else None,
        "images": [
            {"name": Path(ip).name, "exists": Path(ip).exists()}
            for ip in p.image_paths
        ],
        "render_status": job["status"] if job else "idle",
        "render_progress": {
            "done": job["progress_done"],
            "total": job["progress_total"],
            "label": job["progress_label"],
        } if job else {"done": 0, "total": 0, "label": ""},
    }


def _ffmpeg():
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe():
    return shutil.which("ffprobe") or "ffprobe"

# ── Projects ──────────────────────────────────────────────────────────────────

@app.get("/api/projects")
def list_projects():
    return [_project_dict(p) for p in Project.list_all()]


@app.post("/api/projects")
def create_project(payload: dict):
    name = payload.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Tên project không được rỗng")
    try:
        return _project_dict(Project.create(name))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/projects/{safe}")
def get_project(safe: str):
    return _project_dict(_get_project(safe))


@app.delete("/api/projects/{safe}")
def delete_project(safe: str, keep_output: bool = False):
    _get_project(safe).delete(keep_output=keep_output)
    return {"ok": True}


@app.patch("/api/projects/{safe}/rename")
def rename_project(safe: str, payload: dict):
    p = _get_project(safe)
    try:
        p.rename(payload.get("name", "").strip())
        return _project_dict(p)
    except ValueError as e:
        raise HTTPException(400, str(e))

# ── Images ────────────────────────────────────────────────────────────────────

@app.post("/api/projects/{safe}/images")
async def upload_images(safe: str, files: list[UploadFile] = File(...)):
    p = _get_project(safe)
    images_dir = p.dir / "images"
    images_dir.mkdir(exist_ok=True)
    added = []
    for f in files:
        if not any(f.filename.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
            continue
        stem, suffix = Path(f.filename).stem, Path(f.filename).suffix
        dest = images_dir / f.filename
        counter = 1
        while dest.exists():
            dest = images_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        raw = await f.read()
        if Path(f.filename).suffix.lower() in (".heic", ".heif") and _HEIC_SUPPORTED:
            import io
            img = _PILImage.open(io.BytesIO(raw))
            dest = dest.with_suffix(".jpg")
            while dest.exists():
                dest = images_dir / f"{stem}_{counter}.jpg"
                counter += 1
            img.save(dest, "JPEG", quality=95)
        else:
            dest.write_bytes(raw)
        if dest not in p.image_paths:
            p.image_paths.append(dest)
        added.append(dest.name)
    p.image_paths = sort_images_smart(p.image_paths)
    p.save()
    return {"added": added, "total": len(p.image_paths)}


@app.delete("/api/projects/{safe}/images")
def remove_images(safe: str, payload: dict):
    p = _get_project(safe)
    to_remove = set(payload.get("names", []))
    p.image_paths = [ip for ip in p.image_paths if Path(ip).name not in to_remove]
    p.save()
    return {"total": len(p.image_paths)}


@app.get("/api/projects/{safe}/image/{filename}")
def serve_image(safe: str, filename: str):
    p = _get_project(safe)
    for ip in p.image_paths:
        if Path(ip).name == filename and Path(ip).exists():
            return FileResponse(str(ip))
    candidate = p.dir / "images" / filename
    if candidate.exists():
        return FileResponse(str(candidate))
    raise HTTPException(404, "Image not found")

# ── Assets ────────────────────────────────────────────────────────────────────

ASSET_KINDS = {"fixed_video", "music", "voice", "overlay"}


@app.post("/api/projects/{safe}/asset/{kind}")
async def upload_asset(safe: str, kind: str, file: UploadFile = File(...)):
    if kind not in ASSET_KINDS:
        raise HTTPException(400, f"kind phải là một trong {ASSET_KINDS}")
    p = _get_project(safe)
    p.assets_dir.mkdir(exist_ok=True)
    dest = p.assets_dir / file.filename
    dest.write_bytes(await file.read())
    p.set_asset(kind, dest)
    p.save()
    return {"name": dest.name}


@app.delete("/api/projects/{safe}/asset/{kind}")
def clear_asset(safe: str, kind: str):
    if kind not in ASSET_KINDS:
        raise HTTPException(400, f"kind phải là một trong {ASSET_KINDS}")
    p = _get_project(safe)
    p.set_asset(kind, None)
    p.save()
    return {"ok": True}


@app.get("/api/projects/{safe}/asset-file/{kind}")
def download_asset_file(safe: str, kind: str):
    """Worker uses this to download assets before rendering."""
    if kind not in ASSET_KINDS:
        raise HTTPException(400, "Invalid kind")
    p = _get_project(safe)
    asset = getattr(p, kind)
    if not asset or not Path(asset).exists():
        raise HTTPException(404, f"{kind} not found")
    return FileResponse(str(asset))

# ── Config ────────────────────────────────────────────────────────────────────

@app.put("/api/projects/{safe}/config")
def update_config(safe: str, payload: dict):
    p = _get_project(safe)
    p.config = payload
    p.save()
    return {"ok": True}

# ── Render ────────────────────────────────────────────────────────────────────

@app.post("/api/projects/{safe}/render/start")
def start_render(safe: str, payload: dict = {}):
    p = _get_project(safe)

    existing = latest_for_project(safe)
    if existing and existing["status"] in ("pending", "running", "cancelling"):
        # Auto-cancel nếu worker offline hoặc job không update > 5 phút
        worker_online = (time.time() - _worker_last_seen) < 60
        try:
            from datetime import timezone
            updated = datetime.fromisoformat(existing["updated_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated).total_seconds()
        except Exception:
            age = 999
        if not worker_online or age > 300:
            cancel(existing["id"])
        else:
            raise HTTPException(400, "Render đang chạy hoặc đang chờ")

    images = [ip for ip in p.image_paths if Path(ip).exists()]
    if not images:
        raise HTTPException(400, "Không có ảnh nào để render")
    if not p.music or not p.music.exists():
        raise HTTPException(400, "Chưa upload music")

    batch_name = (payload.get("batch_name") or "").strip()
    custom_dir = (payload.get("output_dir") or "").strip()
    output_dir = custom_dir if custom_dir else ""
    if not batch_name:
        out_path = Path(output_dir) if output_dir else p.output_dir
        batch_name = _auto_batch_name(p.name, out_path)

    job_id = create_job(
        project_safe=safe,
        output_dir=output_dir,
        batch_name=batch_name,
    )
    return {"ok": True, "job_id": job_id, "batch_name": batch_name}


@app.post("/api/projects/{safe}/render/stop")
def stop_render(safe: str):
    job = latest_for_project(safe)
    if not job:
        raise HTTPException(404, "Không có render job")
    cancel(job["id"])
    return {"ok": True}


@app.get("/api/projects/{safe}/render/status")
def render_status(safe: str):
    job = latest_for_project(safe)
    if not job:
        return {"status": "idle", "progress": {"done": 0, "total": 0, "label": ""}}
    return {
        "status": job["status"],
        "progress": {
            "done": job["progress_done"],
            "total": job["progress_total"],
            "label": job["progress_label"],
        },
    }


@app.get("/api/projects/{safe}/render/stream")
async def stream_render(safe: str):
    job = latest_for_project(safe)
    if not job or job["status"] not in ("pending", "running", "cancelling"):
        raise HTTPException(404, "Không có render job đang chạy")

    job_id = job["id"]

    async def generator():
        last_log_id = 0
        last_progress = None

        while True:
            current = get_job(job_id)
            if not current:
                return

            # New log lines
            for row in logs_since(job_id, last_log_id):
                yield f"data: {json.dumps({'type': 'log', 'msg': row['msg']}, ensure_ascii=False)}\n\n"
                last_log_id = row["id"]

            # Progress changed
            prog = {
                "done": current["progress_done"],
                "total": current["progress_total"],
                "label": current["progress_label"],
            }
            if prog != last_progress:
                yield f"data: {json.dumps({'type': 'progress', **prog}, ensure_ascii=False)}\n\n"
                last_progress = prog

            # Finished?
            status = current["status"]
            if status in ("done", "error", "stopped", "cancelled"):
                # Flush remaining log lines before closing
                for row in logs_since(job_id, last_log_id):
                    yield f"data: {json.dumps({'type': 'log', 'msg': row['msg']}, ensure_ascii=False)}\n\n"
                    last_log_id = row["id"]
                yield f"data: {json.dumps({'type': 'done', 'status': status})}\n\n"
                return
            if status == "cancelling":
                pass  # wait for worker to finish cancelling

            await asyncio.sleep(0.3)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Worker API ────────────────────────────────────────────────────────────────

@app.post("/api/worker/heartbeat")
def worker_heartbeat():
    global _worker_last_seen
    _worker_last_seen = time.time()
    return {"ok": True}


@app.get("/api/worker/status")
def worker_status():
    online = (time.time() - _worker_last_seen) < 60
    return {"online": online, "last_seen": _worker_last_seen}


@app.get("/api/worker/jobs/pending")
def worker_get_pending():
    return get_pending()


@app.post("/api/worker/jobs/{job_id}/claim")
def worker_claim(job_id: str):
    if not claim(job_id):
        raise HTTPException(409, "Job đã được claim hoặc không còn pending")
    return {"ok": True}


@app.post("/api/worker/jobs/{job_id}/progress")
def worker_progress(job_id: str, payload: dict):
    update_progress(
        job_id,
        payload.get("done", 0),
        payload.get("total", 0),
        payload.get("label", ""),
    )
    return {"ok": True}


@app.post("/api/worker/jobs/{job_id}/log")
def worker_log(job_id: str, payload: dict):
    add_log(job_id, payload.get("msg", ""))
    return {"ok": True}


@app.post("/api/worker/jobs/{job_id}/complete")
def worker_complete(job_id: str, payload: dict):
    finish(job_id, payload.get("status", "done"))
    return {"ok": True}


@app.get("/api/worker/jobs/{job_id}/status")
def worker_job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"status": job["status"]}

# ── Helpers: batch naming ─────────────────────────────────────────────────────

def _auto_batch_name(project_name: str, output_dir: Path) -> str:
    # Giữ dấu _ giữa các từ, bỏ ký tự đặc biệt khác, gộp __ liên tiếp
    safe = ''.join(c if c.isalnum() else '_' for c in project_name)
    while '__' in safe:
        safe = safe.replace('__', '_')
    safe = safe.strip('_')[:20] or "batch"
    existing = (
        len([d for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith("_")])
        if output_dir.exists() else 0
    )
    timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
    return f"{safe}_{existing + 1:02d}_{timestamp}"


@app.get("/api/projects/{safe}/next-batch-name")
def next_batch_name(safe: str):
    p = _get_project(safe)
    return {"name": _auto_batch_name(p.name, p.output_dir)}


@app.get("/api/browse-folder")
def browse_folder():
    # Dùng tkinter subprocess để tránh conflict với event loop của server
    script = (
        "import sys; sys.stdout.reconfigure(encoding='utf-8'); "
        "import tkinter as tk; from tkinter import filedialog; "
        "r = tk.Tk(); r.withdraw(); r.wm_attributes('-topmost', 1); "
        "p = filedialog.askdirectory(title='Chon thu muc luu video'); "
        "r.destroy(); print(p or 'CANCELLED')"
    )
    try:
        r = _sp.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=120, encoding="utf-8",
        )
        path = r.stdout.strip()
        if not path or path == "CANCELLED":
            raise HTTPException(400, "Cancelled")
        return {"path": path}
    except _sp.TimeoutExpired:
        raise HTTPException(408, "Timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Capacity ──────────────────────────────────────────────────────────────────

@app.get("/api/projects/{safe}/capacity")
def get_capacity(safe: str):
    p = _get_project(safe)
    images = [ip for ip in p.image_paths if Path(ip).exists()]
    engine = VideoEngine(
        config=p.config,
        image_paths=images,
        fixed_video=str(p.fixed_video) if p.fixed_video else str(p.dir),
        music_file=str(p.music) if p.music else str(p.dir),
        voice_file=str(p.voice) if p.voice else str(p.dir),
        overlay_video=None,
        output_dir=str(p.output_dir),
        history_csv=str(p.history_csv),
        ffmpeg_path=_ffmpeg(),
        ffprobe_path=_ffprobe(),
    )
    return engine.capacity_summary()

# ── Outputs ───────────────────────────────────────────────────────────────────

@app.get("/api/projects/{safe}/outputs")
def list_outputs(safe: str, custom_dir: Optional[str] = None):
    p = _get_project(safe)
    scan = Path(custom_dir) if custom_dir else p.output_dir
    result = []
    if scan.exists():
        for batch in sorted(scan.iterdir(), reverse=True):
            if batch.is_dir() and not batch.name.startswith("_"):
                videos = sorted(f.name for f in batch.glob("*.mp4"))
                if videos:
                    result.append({"batch": batch.name, "videos": videos, "dir": str(scan)})
    return result


@app.get("/api/projects/{safe}/output/{batch}/{filename}")
def download_output(safe: str, batch: str, filename: str):
    p = _get_project(safe)
    path = p.output_dir / batch / filename
    if not path.exists():
        raise HTTPException(404, "File không tồn tại")
    return FileResponse(str(path), media_type="video/mp4", filename=filename)


@app.post("/api/projects/{safe}/open-folder")
def open_output_folder(safe: str):
    import subprocess, sys as _sys
    p = _get_project(safe)
    job = latest_for_project(safe)
    output_dir_str = job["output_dir"] if job and job.get("output_dir") else ""
    folder = Path(output_dir_str) if output_dir_str else p.output_dir
    folder.mkdir(parents=True, exist_ok=True)
    if _sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    elif _sys.platform.startswith("win"):
        subprocess.Popen(["explorer", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])
    return {"ok": True}

# ── Job Queue API ─────────────────────────────────────────────────────────────

@app.get("/api/jobs")
def api_list_jobs(project: str | None = None, limit: int = 50):
    return list_jobs(project_safe=project, limit=limit)


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job không tìm thấy")
    cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/redo")
def api_redo_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job không tìm thấy")
    safe = job["project_safe"]
    p = _get_project(safe)
    # Cancel job đang chạy nếu có
    existing = latest_for_project(safe)
    if existing and existing["status"] in ("pending", "running", "cancelling"):
        cancel(existing["id"])
    # Tạo job mới với cùng output_dir, batch_name mới
    out_path = Path(job["output_dir"]) if job.get("output_dir") else p.output_dir
    batch_name = _auto_batch_name(p.name, out_path)
    new_id = create_job(project_safe=safe, output_dir=job.get("output_dir", ""), batch_name=batch_name)
    return {"ok": True, "job_id": new_id, "batch_name": batch_name}


# ── Templates ─────────────────────────────────────────────────────────────────

def _template_dict(t: Template) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "config": t.config,
        "music": t.music.name if t.music else None,
        "voice": t.voice.name if t.voice else None,
    }


@app.get("/api/templates")
def list_templates():
    return [_template_dict(t) for t in Template.list_all()]


@app.post("/api/templates")
def create_template(payload: dict):
    name = payload.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Tên template không được rỗng")
    config = payload.get("config", {})
    music: Path | None = None
    voice: Path | None = None
    project_safe = payload.get("from_project", "")
    if project_safe:
        p = _get_project(project_safe)
        if not config:
            config = dict(p.config)
        music = p.music
        voice = p.voice
    t = Template.create(name, config, music, voice)
    return _template_dict(t)


@app.delete("/api/templates/{tid}")
def delete_template(tid: str):
    path = TEMPLATES_DIR / tid
    if not path.exists():
        raise HTTPException(404, "Template không tồn tại")
    Template.load_from(path).delete()
    return {"ok": True}


@app.patch("/api/templates/{tid}/rename")
def rename_template(tid: str, payload: dict):
    path = TEMPLATES_DIR / tid
    if not path.exists():
        raise HTTPException(404, "Template không tồn tại")
    t = Template.load_from(path)
    try:
        t.rename(payload.get("name", "").strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _template_dict(t)


@app.post("/api/templates/{tid}/duplicate")
def duplicate_template(tid: str):
    path = TEMPLATES_DIR / tid
    if not path.exists():
        raise HTTPException(404, "Template không tồn tại")
    return _template_dict(Template.load_from(path).duplicate())


@app.post("/api/projects/{safe}/apply-template/{tid}")
def apply_template(safe: str, tid: str):
    p = _get_project(safe)
    path = TEMPLATES_DIR / tid
    if not path.exists():
        raise HTTPException(404, "Template không tồn tại")
    t = Template.load_from(path)
    if t.config:
        p.config = {**p.config, **t.config}
    p.assets_dir.mkdir(exist_ok=True)
    if t.music and t.music.exists():
        dest = p.assets_dir / t.music.name
        shutil.copy2(t.music, dest)
        p.set_asset("music", dest)
    if t.voice and t.voice.exists():
        dest = p.assets_dir / t.voice.name
        shutil.copy2(t.voice, dest)
        p.set_asset("voice", dest)
    p.save()
    return _project_dict(p)


@app.post("/api/projects/{safe}/duplicate")
def duplicate_project(safe: str):
    p = _get_project(safe)
    base_name = f"{p.name} (copy)"
    try:
        new_p = Project.create(base_name)
    except ValueError:
        from datetime import datetime as _dt
        base_name = f"{p.name} copy {_dt.now().strftime('%H%M%S')}"
        new_p = Project.create(base_name)
    new_p.config = dict(p.config)
    images_dir = new_p.dir / "images"
    images_dir.mkdir(exist_ok=True)
    new_paths = []
    for ip in p.image_paths:
        ip = Path(ip)
        if ip.exists():
            dest = images_dir / ip.name
            shutil.copy2(ip, dest)
            new_paths.append(dest)
    new_p.image_paths = new_paths
    new_p.assets_dir.mkdir(exist_ok=True)
    for kind in ("fixed_video", "music", "voice", "overlay"):
        asset = getattr(p, kind)
        if asset and Path(asset).exists():
            dest = new_p.assets_dir / Path(asset).name
            shutil.copy2(asset, dest)
            new_p.set_asset(kind, dest)
    new_p.save()
    return _project_dict(new_p)

# ── Replace Audio ─────────────────────────────────────────────────────────────

_TEMP_RA_DIR = Path("/tmp/vidauto_replace_audio")
_ra_stop_requested: bool = False


@app.get("/api/all-batches")
def get_all_batches():
    """Trả về tất cả batch dirs từ mọi project + job history."""
    result: list[dict] = []
    seen: set[str] = set()

    def _add(batch_dir: Path, project_safe: str):
        key = str(batch_dir)
        if key in seen:
            return
        seen.add(key)
        if not (batch_dir.exists() and batch_dir.is_dir() and not batch_dir.name.startswith("_")):
            return
        vids = (list(batch_dir.glob("*.mp4")) + list(batch_dir.glob("*.mov")) +
                list(batch_dir.glob("*.m4v")))
        if vids:
            result.append({
                "batch": batch_dir.name,
                "path": str(batch_dir),
                "count": len(vids),
                "project": project_safe,
            })

    for p in Project.list_all():
        if p.output_dir.exists():
            for d in sorted(p.output_dir.iterdir(), reverse=True):
                _add(d, p.safe)

    for job in list_jobs(limit=200):
        od = job.get("output_dir", "")
        if od:
            d = Path(od)
            if d.exists():
                for bd in sorted(d.iterdir(), reverse=True):
                    _add(bd, job.get("project_safe", ""))

    result.sort(key=lambda x: x["path"], reverse=True)
    return result


@app.post("/api/replace-audio/upload/{kind}")
async def replace_audio_upload(kind: str, file: UploadFile = File(...)):
    if kind not in ("music", "voice"):
        raise HTTPException(400, "kind must be 'music' or 'voice'")
    _TEMP_RA_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename).suffix or ".mp3"
    dest = _TEMP_RA_DIR / f"{kind}{suffix}"
    dest.write_bytes(await file.read())
    return {"path": str(dest), "name": dest.name}


@app.post("/api/replace-audio/stop")
def replace_audio_stop():
    global _ra_stop_requested
    _ra_stop_requested = True
    return {"ok": True}


@app.post("/api/replace-audio/run")
async def replace_audio_run(payload: dict):
    global _ra_stop_requested
    _ra_stop_requested = False

    input_dir  = (payload.get("input_dir")  or "").strip()
    output_dir = (payload.get("output_dir") or "").strip()
    music_start  = float(payload.get("music_start",  72))
    music_volume = float(payload.get("music_volume", 0.6))
    voice_volume = float(payload.get("voice_volume", 4.5))
    limit_n = payload.get("limit")

    # Resolve audio paths — music and voice each have independent source
    music_source = payload.get("music_source", payload.get("audio_source", "upload"))
    voice_source = payload.get("voice_source", payload.get("audio_source", "upload"))

    # Load project (for audio and/or naming)
    project_safe = payload.get("project_safe", "")
    proj = _get_project(project_safe) if project_safe else None

    music_path = (str(proj.music) if (proj and proj.music and proj.music.exists()) else "") \
        if music_source == "project" else (payload.get("music_path") or "").strip()
    voice_path = (str(proj.voice) if (proj and proj.voice and proj.voice.exists()) else "") \
        if voice_source == "project" else (payload.get("voice_path") or "").strip()

    # Build base name: {ProjName}_R[_{suffix}]
    name_suffix = "".join(c if c.isalnum() or c in "-_" else "_"
                          for c in (payload.get("name_suffix") or "").strip()).strip("_")
    if proj:
        proj_slug = "".join(c if c.isalnum() or c in "-_" else "_"
                            for c in proj.name).strip("_")
    else:
        proj_slug = "video"
    base_name = f"{proj_slug}_R_{name_suffix}" if name_suffix else f"{proj_slug}_R"

    # Validate
    errors = []
    if not input_dir or not Path(input_dir).exists():
        errors.append(f"Input dir không tồn tại: {input_dir}")
    if not music_path or not Path(music_path).exists():
        errors.append("Music file chưa có hoặc không tìm thấy")
    if not voice_path or not Path(voice_path).exists():
        errors.append("Voice file chưa có hoặc không tìm thấy")
    if errors:
        raise HTTPException(400, "; ".join(errors))

    # Collect videos
    in_path = Path(input_dir)
    videos: list[Path] = []
    for ext in [".mp4", ".mov", ".m4v"]:
        videos.extend(in_path.glob(f"*{ext}"))
        videos.extend(in_path.glob(f"*{ext.upper()}"))
    videos = sorted(set(videos))
    if limit_n:
        try:
            videos = videos[:int(limit_n)]
        except Exception:
            pass

    if not videos:
        raise HTTPException(400, "Không tìm thấy video nào trong input dir")

    out_path = Path(output_dir) if output_dir else in_path.parent / base_name
    out_path.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin  = _ffmpeg()
    ffprobe_bin = _ffprobe()
    total = len(videos)

    async def generator():
        global _ra_stop_requested
        yield f"data: {json.dumps({'type':'log','msg':f'=== Replace Audio: {total} video ==='}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type':'log','msg':f'Input:  {input_dir}'}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type':'log','msg':f'Output: {str(out_path)}'}, ensure_ascii=False)}\n\n"

        ok_count = err_count = 0

        for i, video_path in enumerate(videos, 1):
            if _ra_stop_requested:
                yield f"data: {json.dumps({'type':'log','msg':'■ Đã dừng theo yêu cầu'}, ensure_ascii=False)}\n\n"
                break

            yield f"data: {json.dumps({'type':'progress','done':i-1,'total':total,'label':f'{i-1}/{total}'}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type':'log','msg':f'[{i}/{total}] {video_path.name}'}, ensure_ascii=False)}\n\n"

            out_file  = out_path / f"{base_name}_{i:03d}.mp4"
            tmp_audio = Path(f"/tmp/vidauto_ra_{i}_{int(time.time())}.m4a")

            try:
                # Step 1: lấy duration
                probe = await asyncio.create_subprocess_exec(
                    ffprobe_bin, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(video_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await probe.communicate()
                duration = float(stdout.decode().strip())

                # Step 2: mix audio → /tmp (tránh OneDrive lock)
                fc = (
                    f"[1:a]volume={music_volume},"
                    f"atrim=duration={duration},"
                    f"asetpts=PTS-STARTPTS[music];"
                    f"[2:a]volume={voice_volume},"
                    f"atrim=duration={duration},"
                    f"asetpts=PTS-STARTPTS[voice];"
                    f"[music][voice]"
                    f"amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
                    f"atrim=duration={duration},"
                    f"asetpts=PTS-STARTPTS[aout]"
                )
                cmd1 = [
                    ffmpeg_bin, "-y",
                    "-ss", str(music_start), "-i", music_path,
                    "-i", voice_path,
                    "-filter_complex", fc,
                    "-map", "[aout]", "-c:a", "aac",
                    str(tmp_audio),
                ]
                p1 = await asyncio.create_subprocess_exec(
                    *cmd1,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, err1 = await p1.communicate()
                if p1.returncode != 0:
                    raise Exception("audio mix: " + err1.decode(errors="replace")[-200:])

                # Step 3: mux video + audio
                cmd2 = [
                    ffmpeg_bin, "-y",
                    "-i", str(video_path),
                    "-i", str(tmp_audio),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "copy",
                    "-shortest",
                    str(out_file),
                ]
                p2 = await asyncio.create_subprocess_exec(
                    *cmd2,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, err2 = await p2.communicate()
                if p2.returncode != 0:
                    raise Exception("mux: " + err2.decode(errors="replace")[-200:])

                yield f"data: {json.dumps({'type':'log','msg':f'  ✓ {out_file.name}'}, ensure_ascii=False)}\n\n"
                ok_count += 1

            except Exception as exc:
                yield f"data: {json.dumps({'type':'log','msg':f'  ✗ {exc}'}, ensure_ascii=False)}\n\n"
                err_count += 1
            finally:
                try:
                    tmp_audio.unlink(missing_ok=True)
                except Exception:
                    pass

        yield f"data: {json.dumps({'type':'progress','done':total,'total':total,'label':f'✓ {ok_count}/{total}'}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type':'log','msg':f'=== DONE: {ok_count} ok · {err_count} lỗi · {str(out_path)} ==='}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type':'done','ok':ok_count,'error':err_count,'output_dir':str(out_path)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Static files ──────────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=7861, reload=True)
