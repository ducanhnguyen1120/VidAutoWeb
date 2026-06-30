"""
Template = preset tái sử dụng: settings + music + voice (không có ảnh).
"""

import json
import re
import shutil
from pathlib import Path
from typing import Optional

from project_manager import APPDATA_DIR

TEMPLATES_DIR = APPDATA_DIR / "templates"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


def _make_id(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name).strip()
    s = re.sub(r"[\s-]+", "_", s)[:32] or "template"
    base, counter = s, 1
    while (TEMPLATES_DIR / s).exists():
        s = f"{base}_{counter}"
        counter += 1
    return s


class Template:
    def __init__(self):
        self.id: str = ""
        self.name: str = ""
        self.config: dict = {}
        self.music: Optional[Path] = None
        self.voice: Optional[Path] = None

    @property
    def dir(self) -> Path:
        return TEMPLATES_DIR / self.id

    # ── factory ─────────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        name: str,
        config: dict,
        music: Optional[Path] = None,
        voice: Optional[Path] = None,
    ) -> "Template":
        t = cls()
        t.id = _make_id(name)
        t.name = name
        t.config = config
        t.dir.mkdir(parents=True, exist_ok=True)
        if music and music.exists():
            dest = t.dir / music.name
            shutil.copy2(music, dest)
            t.music = dest
        if voice and voice.exists():
            dest = t.dir / voice.name
            shutil.copy2(voice, dest)
            t.voice = dest
        t.save()
        return t

    @classmethod
    def load_from(cls, dir_path: Path) -> "Template":
        meta = json.loads((dir_path / "template.json").read_text(encoding="utf-8"))
        t = cls()
        t.id = dir_path.name
        t.name = meta.get("name", dir_path.name)
        t.config = meta.get("config", {})
        m, v = meta.get("music"), meta.get("voice")
        t.music = (dir_path / m) if m and (dir_path / m).exists() else None
        t.voice = (dir_path / v) if v and (dir_path / v).exists() else None
        return t

    @classmethod
    def list_all(cls) -> list["Template"]:
        out = []
        for d in sorted(TEMPLATES_DIR.iterdir()):
            if d.is_dir() and (d / "template.json").exists():
                try:
                    out.append(cls.load_from(d))
                except Exception:
                    pass
        return out

    # ── persistence ──────────────────────────────────────────────────────────

    def save(self):
        meta = {
            "name": self.name,
            "config": self.config,
            "music": self.music.name if self.music else None,
            "voice": self.voice.name if self.voice else None,
        }
        (self.dir / "template.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── operations ───────────────────────────────────────────────────────────

    def delete(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rename(self, new_name: str):
        if not new_name:
            raise ValueError("Tên không được rỗng")
        self.name = new_name
        self.save()

    def duplicate(self) -> "Template":
        new_name = f"{self.name} (copy)"
        new_id = _make_id(new_name)
        new_dir = TEMPLATES_DIR / new_id
        shutil.copytree(self.dir, new_dir)
        nt = Template.load_from(new_dir)
        nt.id = new_id
        nt.name = new_name
        nt.save()
        return nt
