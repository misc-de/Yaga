from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from .config import THUMB_DIR
from .models import MediaItem


class Thumbnailer:
    def __init__(self) -> None:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)

    def thumb_path_for(self, path: Path) -> Path:
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        return THUMB_DIR / f"{digest}.jpg"

    def ensure_thumbnail(self, item_or_path: MediaItem | Path, media_type: str | None = None) -> str | None:
        if isinstance(item_or_path, MediaItem):
            path = Path(item_or_path.path)
            media_type = item_or_path.media_type
        else:
            path = item_or_path
        target = self.thumb_path_for(path)
        if target.exists():
            return str(target)
        if media_type == "video":
            return self._video_thumbnail(path, target)
        return self._image_thumbnail(path, target)

    def clear(self) -> None:
        if THUMB_DIR.exists():
            shutil.rmtree(THUMB_DIR)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)

    def _image_thumbnail(self, path: Path, target: Path) -> str | None:
        try:
            import gi

            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf

            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 320, 320, True)
            if pixbuf:
                pixbuf.savev(str(target), "jpeg", ["quality"], ["85"])
                return str(target)
        except Exception:
            return None
        return None

    def _video_thumbnail(self, path: Path, target: Path) -> str | None:
        if shutil.which("ffmpegthumbnailer"):
            cmd = ["ffmpegthumbnailer", "-i", str(path), "-o", str(target), "-s", "320", "-q", "8"]
        elif shutil.which("ffmpeg"):
            cmd = ["ffmpeg", "-y", "-i", str(path), "-ss", "00:00:01", "-frames:v", "1", "-vf", "scale=320:-1", str(target)]
        else:
            return None
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            return None
        return str(target) if target.exists() else None

