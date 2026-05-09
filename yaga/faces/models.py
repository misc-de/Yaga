from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Face:
    id: int
    media_path: str
    media_category: str
    bbox: tuple[int, int, int, int]
    quality: float
    thumb_path: str | None
    person_id: int | None
    cluster_id: int | None


@dataclass(frozen=True)
class Person:
    id: int
    name: str
    cover_face_id: int | None
    hidden: bool
    face_count: int = 0
