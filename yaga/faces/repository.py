from __future__ import annotations

import json
import sqlite3
import time

from ..database import Database
from .models import Face, Person


class FaceRepository:
    """SQLite access for face/person/index-state tables. Holds no ML state."""

    def __init__(self, database: Database) -> None:
        self.database = database

    # ── State tracking ────────────────────────────────────────────────────

    def needs_indexing(self, path: str, category: str, mtime: float, version: int) -> bool:
        """True if (path, category) has no row in face_index_state, or its
        mtime/version is stale."""
        with self.database.lock:
            row = self.database.conn.execute(
                "SELECT media_mtime, detected_version FROM face_index_state "
                "WHERE media_path = ? AND media_category = ?",
                (path, category),
            ).fetchone()
        if row is None:
            return True
        return row["media_mtime"] != mtime or row["detected_version"] < version

    def mark_indexed(self, path: str, category: str, mtime: float, version: int) -> None:
        with self.database.lock:
            self.database.conn.execute(
                "INSERT INTO face_index_state(media_path, media_category, media_mtime, detected_version) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(media_path, media_category) DO UPDATE SET "
                "  media_mtime=excluded.media_mtime, detected_version=excluded.detected_version",
                (path, category, mtime, version),
            )

    def pending_paths(self, version: int, limit: int | None = None) -> list[tuple[str, str, float]]:
        """Return media (path, category, mtime) tuples whose face index is
        missing or outdated. Only image media."""
        sql = (
            "SELECT m.path, m.category, m.mtime FROM media m "
            "LEFT JOIN face_index_state s "
            "  ON s.media_path = m.path AND s.media_category = m.category "
            "WHERE m.media_type = 'image' "
            "  AND (s.media_path IS NULL OR s.media_mtime != m.mtime OR s.detected_version < ?) "
            "ORDER BY m.mtime DESC"
        )
        args: list = [version]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self.database.lock:
            rows = self.database.conn.execute(sql, args).fetchall()
        return [(r["path"], r["category"], r["mtime"]) for r in rows]

    # ── Face writes ───────────────────────────────────────────────────────

    def replace_faces(
        self,
        path: str,
        category: str,
        faces: list[dict],
    ) -> None:
        """Drop existing faces for this media item and insert the new set.
        Each dict needs: bbox, landmarks, embedding (bytes), quality, thumb_path."""
        now = time.time()
        with self.database.lock:
            self.database.conn.execute(
                "DELETE FROM faces WHERE media_path = ? AND media_category = ?",
                (path, category),
            )
            self.database.conn.executemany(
                "INSERT INTO faces("
                "  media_path, media_category, bbox, landmarks, embedding,"
                "  quality, thumb_path, detected_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        path, category,
                        json.dumps(f["bbox"]),
                        json.dumps(f.get("landmarks")) if f.get("landmarks") is not None else None,
                        f["embedding"],
                        float(f["quality"]),
                        f.get("thumb_path"),
                        now,
                    )
                    for f in faces
                ],
            )

    def delete_faces_for_media(self, path: str, category: str) -> None:
        with self.database.lock:
            self.database.conn.execute(
                "DELETE FROM faces WHERE media_path = ? AND media_category = ?",
                (path, category),
            )
            self.database.conn.execute(
                "DELETE FROM face_index_state WHERE media_path = ? AND media_category = ?",
                (path, category),
            )

    # ── Reads for clustering / UI ─────────────────────────────────────────

    def all_unassigned_embeddings(self) -> list[tuple[int, bytes]]:
        """(face_id, embedding_blob) for faces without a person."""
        with self.database.lock:
            rows = self.database.conn.execute(
                "SELECT id, embedding FROM faces WHERE person_id IS NULL"
            ).fetchall()
        return [(r["id"], r["embedding"]) for r in rows]

    def set_cluster_ids(self, assignments: list[tuple[int, int | None]]) -> None:
        if not assignments:
            return
        with self.database.lock:
            self.database.conn.executemany(
                "UPDATE faces SET cluster_id = ? WHERE id = ?",
                [(cluster_id, face_id) for face_id, cluster_id in assignments],
            )

    # ── Persons ───────────────────────────────────────────────────────────

    def create_person(self, name: str, cover_face_id: int | None = None) -> int:
        with self.database.lock:
            cur = self.database.conn.execute(
                "INSERT INTO persons(name, cover_face_id, created_at) VALUES(?, ?, ?)",
                (name, cover_face_id, time.time()),
            )
        return cur.lastrowid

    def assign_cluster_to_person(self, cluster_id: int, person_id: int) -> int:
        with self.database.lock:
            cur = self.database.conn.execute(
                "UPDATE faces SET person_id = ? WHERE cluster_id = ? AND person_id IS NULL",
                (person_id, cluster_id),
            )
        return cur.rowcount

    def list_persons(self) -> list[Person]:
        with self.database.lock:
            rows = self.database.conn.execute(
                "SELECT p.id, p.name, p.cover_face_id, p.hidden, "
                "       (SELECT COUNT(*) FROM faces f WHERE f.person_id = p.id) AS face_count "
                "FROM persons p ORDER BY p.name COLLATE NOCASE"
            ).fetchall()
        return [
            Person(
                id=r["id"],
                name=r["name"],
                cover_face_id=r["cover_face_id"],
                hidden=bool(r["hidden"]),
                face_count=r["face_count"],
            )
            for r in rows
        ]

    def media_paths_for_person(self, person_id: int) -> list[tuple[str, str]]:
        with self.database.lock:
            rows = self.database.conn.execute(
                "SELECT DISTINCT media_path, media_category FROM faces WHERE person_id = ?",
                (person_id,),
            ).fetchall()
        return [(r["media_path"], r["media_category"]) for r in rows]

    @staticmethod
    def _row_to_face(row: sqlite3.Row) -> Face:
        bbox = tuple(json.loads(row["bbox"]))
        return Face(
            id=row["id"],
            media_path=row["media_path"],
            media_category=row["media_category"],
            bbox=bbox,  # type: ignore[arg-type]
            quality=row["quality"],
            thumb_path=row["thumb_path"],
            person_id=row["person_id"],
            cluster_id=row["cluster_id"],
        )
