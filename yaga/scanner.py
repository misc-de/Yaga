from __future__ import annotations

import time
from pathlib import Path

from .database import Database
from .models import media_type_for
from .thumbnails import Thumbnailer


class MediaScanner:
    def __init__(self, database: Database, thumbnailer: Thumbnailer) -> None:
        self.database = database
        self.thumbnailer = thumbnailer

    def scan(self, categories: list[tuple[str, str, str]]) -> None:
        started = time.time()
        scanned_categories: list[str] = []
        for category, _label, root_text in categories:
            root = Path(root_text).expanduser()
            if not root.exists():
                continue
            scanned_categories.append(category)
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                media_type = media_type_for(path)
                if not media_type:
                    continue
                folder = self._relative_folder(root, path.parent)
                thumb = self.thumbnailer.ensure_thumbnail(path, media_type)
                self.database.upsert_media(path=path, category=category, media_type=media_type, folder=folder, thumb_path=thumb)
        self.database.prune_missing(started, scanned_categories)
        self.database.commit()

    def _relative_folder(self, root: Path, folder: Path) -> str:
        try:
            rel = folder.relative_to(root)
        except ValueError:
            return str(folder)
        if str(rel) == ".":
            return "/"
        return str(rel)
