"""
Project = state lưu lại: settings + ảnh + assets + history.
Khác Template: Template = preset tái sử dụng, không có ảnh + history.
"""

import csv
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

APPDATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "AutoVideoMaker"
PROJECTS_DIR = APPDATA_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


def safe_name(name: str) -> str:
    s = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    s = re.sub(r"\s+", " ", s)
    return s


class Project:
    def __init__(self, name: str, root_dir: Path | None = None):
        self.name = name
        self.safe = safe_name(name) or "untitled"
        self.dir = Path(root_dir) if root_dir else (PROJECTS_DIR / self.safe)
        self.assets_dir = self.dir / "assets"
        self.history_csv = self.dir / "history.csv"
        self.output_dir = self.dir / "output"
        self.json_path = self.dir / "project.json"
        # State
        self.config: dict = {}
        self.image_paths: list[Path] = []
        self.fixed_video: Path | None = None
        self.music: Path | None = None
        self.voice: Path | None = None
        self.overlay: Path | None = None
        self.created_at: str | None = None
        self.updated_at: str | None = None

    # ---------------- class methods ----------------

    @classmethod
    def list_all(cls) -> list["Project"]:
        if not PROJECTS_DIR.exists():
            return []
        out = []
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir() and (d / "project.json").exists():
                try:
                    out.append(cls.load_from(d))
                except Exception:
                    pass
        # Sort by updated_at desc
        out.sort(key=lambda p: p.updated_at or "", reverse=True)
        return out

    @classmethod
    def load_from(cls, dir_path) -> "Project":
        dir_path = Path(dir_path)
        meta_file = dir_path / "project.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        p = cls(meta.get("name", dir_path.name), root_dir=dir_path)
        p.config = meta.get("config", {})
        p.image_paths = []
        for rp in meta.get("image_paths", []):
            rp_path = Path(rp)
            if rp_path.is_absolute():
                p.image_paths.append(rp_path)
            else:
                p.image_paths.append(dir_path / rp_path)
        p.fixed_video = (p.assets_dir / meta["fixed_video"]) if meta.get("fixed_video") else None
        p.music = (p.assets_dir / meta["music"]) if meta.get("music") else None
        p.voice = (p.assets_dir / meta["voice"]) if meta.get("voice") else None
        p.overlay = (p.assets_dir / meta["overlay"]) if meta.get("overlay") else None
        p.created_at = meta.get("created_at")
        p.updated_at = meta.get("updated_at")
        return p

    @classmethod
    def create(cls, name: str) -> "Project":
        if not safe_name(name):
            raise ValueError("Tên project không hợp lệ (chỉ chữ/số/_/-/space).")
        p = cls(name)
        if p.dir.exists():
            raise ValueError(f"Project '{p.safe}' đã tồn tại.")
        p.dir.mkdir(parents=True)
        p.assets_dir.mkdir()
        p.output_dir.mkdir()
        now = datetime.now().isoformat(timespec="seconds")
        p.created_at = now
        p.updated_at = now
        p.save()
        return p

    # ---------------- save / rename / delete ----------------

    def save(self):
        self.updated_at = datetime.now().isoformat(timespec="seconds")
        paths_to_save = []
        for img in self.image_paths:
            img = Path(img)
            try:
                paths_to_save.append(str(img.relative_to(self.dir)))
            except ValueError:
                paths_to_save.append(str(img))
        meta = {
            "name": self.name,
            "config": self.config,
            "image_paths": paths_to_save,
            "fixed_video": self.fixed_video.name if self.fixed_video else None,
            "music": self.music.name if self.music else None,
            "voice": self.voice.name if self.voice else None,
            "overlay": self.overlay.name if self.overlay else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(exist_ok=True)
        self.output_dir.mkdir(exist_ok=True)
        self.json_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def set_asset(self, kind: str, src_path):
        """
        kind ∈ {'fixed_video', 'music', 'voice', 'overlay'}
        Copy file vào assets/ nếu cần và set thuộc tính.
        """
        if not src_path:
            setattr(self, kind, None)
            return None
        src = Path(src_path)
        if not src.exists():
            raise FileNotFoundError(f"{kind}: {src}")
        try:
            if src.parent.resolve() == self.assets_dir.resolve():
                setattr(self, kind, src)
                return src
        except Exception:
            pass
        self.assets_dir.mkdir(exist_ok=True)
        dst = self.assets_dir / src.name
        if (not dst.exists()) or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
        setattr(self, kind, dst)
        return dst

    def rendered_count(self) -> int:
        if not self.output_dir.exists():
            return 0
        try:
            return sum(1 for f in self.output_dir.rglob("*.mp4"))
        except Exception:
            return 0

    def delete(self, keep_output: bool = False):
        """Xoá folder project. keep_output=True → giữ output ra ngoài."""
        if keep_output and self.output_dir.exists():
            ts = datetime.now().strftime("%y%m%d_%H%M%S")
            external = PROJECTS_DIR / f"_kept_{self.safe}_{ts}"
            shutil.move(str(self.output_dir), str(external))
        shutil.rmtree(self.dir, ignore_errors=True)

    def rename(self, new_name: str):
        new_safe = safe_name(new_name)
        if not new_safe:
            raise ValueError("Tên mới không hợp lệ.")
        if new_safe == self.safe:
            self.name = new_name
            self.save()
            return
        new_dir = PROJECTS_DIR / new_safe
        if new_dir.exists():
            raise ValueError(f"Đã có project '{new_safe}'.")
        shutil.move(str(self.dir), str(new_dir))
        self.dir = new_dir
        self.assets_dir = new_dir / "assets"
        self.history_csv = new_dir / "history.csv"
        self.output_dir = new_dir / "output"
        self.json_path = new_dir / "project.json"
        # Asset paths cần update lại
        if self.fixed_video:
            self.fixed_video = self.assets_dir / self.fixed_video.name
        if self.music:
            self.music = self.assets_dir / self.music.name
        if self.voice:
            self.voice = self.assets_dir / self.voice.name
        if self.overlay:
            self.overlay = self.assets_dir / self.overlay.name
        self.name = new_name
        self.safe = new_safe
        self.save()

    def missing_images(self) -> list[Path]:
        """Trả về list ảnh đang lưu trong project nhưng không còn trên đĩa."""
        return [p for p in self.image_paths if not p.exists()]
