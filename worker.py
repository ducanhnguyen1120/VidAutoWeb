"""
VidAuto Mac Worker
─────────────────
Chạy trên Mac. Poll server cloud để nhận render jobs,
download assets, chạy FFmpeg local, lưu video vào folder bạn chọn.

Cấu hình: worker_config.json
  server_url  — URL của cloud server
  output_dir  — Folder lưu video trên Mac
"""

import json
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import quote

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "worker_config.json"

def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        default = {
            "server_url": "https://YOUR_SERVER_URL",
            "output_dir": str(Path.home() / "Movies" / "VidAuto"),
            "poll_interval": 5,
            "heartbeat_interval": 30,
        }
        CONFIG_FILE.write_text(json.dumps(default, indent=2, ensure_ascii=False))
        print(f"⚠️  Tạo config mới tại: {CONFIG_FILE}")
        print("   Sửa server_url và output_dir rồi chạy lại.")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

cfg = _load_config()
SERVER   = cfg["server_url"].rstrip("/")
OUT_DIR  = Path(cfg.get("output_dir", str(Path.home() / "Movies" / "VidAuto")))
POLL_S   = int(cfg.get("poll_interval", 5))
BEAT_S   = int(cfg.get("heartbeat_interval", 30))

# ── Imports ───────────────────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent))

try:
    import requests
except ImportError:
    print("❌ requests chưa được cài. Chạy: pip install requests")
    sys.exit(1)

from video_engine import VideoEngine

session = requests.Session()
session.headers["X-VidAuto-Worker"] = "1"

# History dir — phải trùng với project_manager: ~/AutoVideoMaker/projects/{safe}/history.csv
PROJECTS_DIR = Path.home() / "AutoVideoMaker" / "projects"

# ── Heartbeat loop ────────────────────────────────────────────────────────────

def _heartbeat_loop():
    while True:
        try:
            session.post(f"{SERVER}/api/worker/heartbeat", timeout=5)
        except Exception:
            pass
        time.sleep(BEAT_S)

# ── Job processing ────────────────────────────────────────────────────────────

def _process_job(job: dict):
    job_id      = job["id"]
    safe        = job["project_safe"]
    output_dir  = job.get("output_dir") or str(OUT_DIR)
    batch_name  = job.get("batch_name", "")
    history_csv = str(PROJECTS_DIR / safe / "history.csv")

    def log(msg: str):
        print(msg)
        try:
            session.post(f"{SERVER}/api/worker/jobs/{job_id}/log",
                         json={"msg": msg}, timeout=5)
        except Exception:
            pass

    def progress(done: int, total: int, label: str = ""):
        try:
            session.post(f"{SERVER}/api/worker/jobs/{job_id}/progress",
                         json={"done": done, "total": total, "label": label}, timeout=5)
        except Exception:
            pass

    def stop_flag() -> bool:
        try:
            r = session.get(f"{SERVER}/api/worker/jobs/{job_id}/status", timeout=5)
            return r.ok and r.json().get("status") == "cancelling"
        except Exception:
            return False

    # Claim
    try:
        r = session.post(f"{SERVER}/api/worker/jobs/{job_id}/claim", timeout=5)
        if not r.ok or not r.json().get("ok"):
            return
    except Exception:
        return

    with tempfile.TemporaryDirectory(prefix="vidauto_") as tmp:
        tmp = Path(tmp)
        log(f"=== Bắt đầu job {job_id} — project: {safe} ===")

        try:
            # Project info
            proj = session.get(f"{SERVER}/api/projects/{quote(safe)}", timeout=10).json()

            # Download images
            img_dir = tmp / "images"
            img_dir.mkdir()
            image_paths = []
            for img in [i for i in proj.get("images", []) if i.get("exists")]:
                name = img["name"]
                r = session.get(
                    f"{SERVER}/api/projects/{quote(safe)}/image/{quote(name)}",
                    timeout=60, stream=True,
                )
                if r.ok:
                    dest = img_dir / name
                    dest.write_bytes(r.content)
                    image_paths.append(dest)

            # Download assets
            assets: dict[str, Path] = {}
            for kind in ("fixed_video", "music", "voice", "overlay"):
                if proj.get(kind):
                    r = session.get(
                        f"{SERVER}/api/projects/{quote(safe)}/asset-file/{kind}",
                        timeout=120, stream=True,
                    )
                    if r.ok:
                        ext  = Path(proj[kind]).suffix
                        dest = tmp / f"{kind}{ext}"
                        dest.write_bytes(r.content)
                        assets[kind] = dest

            # Validate
            if not image_paths:
                raise ValueError("Không có ảnh nào tải được")
            if "music" not in assets:
                raise ValueError("Thiếu music")

            log(f"✓ Download: {len(image_paths)} ảnh + {len(assets)} assets")

            # Prepare output dir
            Path(output_dir).mkdir(parents=True, exist_ok=True)

            # Run render
            engine = VideoEngine(
                config=proj.get("config", {}),
                image_paths=image_paths,
                fixed_video=str(assets["fixed_video"]) if "fixed_video" in assets else None,
                music_file=str(assets["music"]),
                voice_file=str(assets["voice"]) if "voice" in assets else None,
                overlay_video=str(assets["overlay"]) if "overlay" in assets else None,
                output_dir=output_dir,
                history_csv=history_csv,
                log_cb=log,
                progress_cb=progress,
                ffmpeg_path=shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if sys.platform == "darwin" else "ffmpeg"),
                ffprobe_path=shutil.which("ffprobe") or ("/opt/homebrew/bin/ffprobe" if sys.platform == "darwin" else "ffprobe"),
                stop_flag=stop_flag,
                batch_name_prefix=batch_name,
                project_name=proj.get("name", safe),
            )
            videos_ok = engine.run()

            session.post(f"{SERVER}/api/worker/jobs/{job_id}/complete",
                         json={"status": "done", "videos_ok": videos_ok}, timeout=5)
            log("✓ Render hoàn tất.")

        except Exception as exc:
            log(f"LỖI: {exc}")
            log(traceback.format_exc())
            try:
                session.post(f"{SERVER}/api/worker/jobs/{job_id}/complete",
                             json={"status": "error"}, timeout=5)
            except Exception:
                pass

# ── Poll loop ─────────────────────────────────────────────────────────────────

def _poll_loop():
    while True:
        try:
            jobs = session.get(f"{SERVER}/api/worker/jobs/pending", timeout=5).json()
            for job in jobs:
                _process_job(job)
        except Exception as exc:
            print(f"[worker] poll error: {exc}")
        time.sleep(POLL_S)

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 40)
    print("  VidAuto Worker")
    print(f"  Server : {SERVER}")
    print(f"  Output : {OUT_DIR}")
    print(f"  Poll   : mỗi {POLL_S}s")
    print("=" * 40)

    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    _poll_loop()
