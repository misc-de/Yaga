from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .config import DB_PATH
from .models import MediaItem


SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    media_type TEXT NOT NULL,
    folder TEXT NOT NULL,
    name TEXT NOT NULL,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    thumb_path TEXT,
    seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_category ON media(category);
CREATE INDEX IF NOT EXISTS idx_media_folder ON media(folder);
CREATE INDEX IF NOT EXISTS idx_media_mtime ON media(mtime);
"""


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript(SCHEMA)

    def upsert_media(self, *, path: Path, category: str, media_type: str, folder: str, thumb_path: str | None) -> None:
        stat = path.stat()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO media(path, category, media_type, folder, name, mtime, size, thumb_path, seen_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    category=excluded.category,
                    media_type=excluded.media_type,
                    folder=excluded.folder,
                    name=excluded.name,
                    mtime=excluded.mtime,
                    size=excluded.size,
                    thumb_path=COALESCE(excluded.thumb_path, media.thumb_path),
                    seen_at=excluded.seen_at
                """,
                (str(path), category, media_type, folder, path.name, stat.st_mtime, stat.st_size, thumb_path, time.time()),
            )

    def prune_missing(self, seen_since: float, categories: list[str]) -> None:
        if not categories:
            return
        placeholders = ",".join("?" for _category in categories)
        with self.lock:
            self.conn.execute(f"DELETE FROM media WHERE seen_at < ? AND category IN ({placeholders})", [seen_since, *categories])

    def set_thumb(self, path: str, thumb_path: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE media SET thumb_path = ? WHERE path = ?", (thumb_path, path))

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    def list_media(self, category: str, sort_mode: str = "newest", folder: str | None = None) -> list[MediaItem]:
        order = {
            "newest": "mtime DESC, name COLLATE NOCASE ASC",
            "oldest": "mtime ASC, name COLLATE NOCASE ASC",
            "name": "name COLLATE NOCASE ASC",
            "folder": "folder COLLATE NOCASE ASC, mtime DESC",
        }.get(sort_mode, "mtime DESC")
        args: list[str] = [category]
        where = "category = ?"
        if folder:
            where += " AND folder = ?"
            args.append(folder)
        with self.lock:
            rows = self.conn.execute(f"SELECT * FROM media WHERE {where} ORDER BY {order}", args).fetchall()
        return [self._row_to_item(row) for row in rows]

    def folders(self, category: str) -> list[tuple[str, int, str | None]]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT folder, COUNT(*) AS count, MAX(thumb_path) AS thumb
                FROM media
                WHERE category = ?
                GROUP BY folder
                ORDER BY folder COLLATE NOCASE ASC
                """,
                (category,),
            ).fetchall()
        return [(row["folder"], row["count"], row["thumb"]) for row in rows]

    def child_folders(self, category: str, parent: str | None) -> list[tuple[str, int, str | None]]:
        with self.lock:
            rows = self.conn.execute("SELECT folder, thumb_path FROM media WHERE category = ?", (category,)).fetchall()
        children: dict[str, tuple[int, str | None]] = {}
        parent_prefix = "" if parent in (None, "/") else f"{parent}/"
        for row in rows:
            folder = row["folder"]
            if folder == "/":
                continue
            if parent in (None, "/"):
                remainder = folder
            elif folder.startswith(parent_prefix):
                remainder = folder[len(parent_prefix):]
            else:
                continue
            if not remainder or "/" not in remainder and folder == parent:
                continue
            child_name = remainder.split("/", 1)[0]
            child_path = child_name if parent in (None, "/") else f"{parent}/{child_name}"
            count, thumb = children.get(child_path, (0, None))
            children[child_path] = (count + 1, thumb or row["thumb_path"])
        return [(folder, count, thumb) for folder, (count, thumb) in sorted(children.items(), key=lambda item: item[0].lower())]

    def delete_path(self, path: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM media WHERE path = ?", (path,))
            self.conn.commit()

    def _row_to_item(self, row: sqlite3.Row) -> MediaItem:
        return MediaItem(
            id=row["id"],
            path=row["path"],
            category=row["category"],
            media_type=row["media_type"],
            folder=row["folder"],
            name=row["name"],
            mtime=row["mtime"],
            size=row["size"],
            thumb_path=row["thumb_path"],
        )
