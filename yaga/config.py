from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "yaga"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "yaga"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "yaga"
THUMB_DIR = CACHE_DIR / "thumbnails"
DB_PATH = DATA_DIR / "yaga.sqlite3"


def default_path(name: str) -> str:
    candidates = {
        "photos": [Path.home() / "Photos", Path.home() / "Bilder", Path.home() / "Pictures"],
        "pictures": [Path.home() / "Pictures", Path.home() / "Bilder"],
        "videos": [Path.home() / "Videos"],
        "screenshots": [Path.home() / "Pictures" / "Screenshots", Path.home() / "Bilder" / "Bildschirmfotos"],
    }
    for path in candidates[name]:
        if path.exists():
            return str(path)
    return str(candidates[name][0])


@dataclass
class Settings:
    photos_dir: str = field(default_factory=lambda: default_path("photos"))
    pictures_dir: str = field(default_factory=lambda: default_path("pictures"))
    videos_dir: str = field(default_factory=lambda: default_path("videos"))
    screenshots_dir: str = field(default_factory=lambda: default_path("screenshots"))
    extra_locations: list[str] = field(default_factory=list)
    sort_mode: str = "newest"
    theme: str = "system"
    language: str = "system"
    external_video_player: str = ""
    grid_columns: int = 4
    last_category: str = ""

    @classmethod
    def load(cls) -> "Settings":
        path = CONFIG_DIR / "settings.json"
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {field.name for field in cls.__dataclass_fields__.values()}
        settings = cls(**{key: value for key, value in data.items() if key in known})
        settings.grid_columns = min(max(int(settings.grid_columns), 2), 10)
        return settings

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = CONFIG_DIR / "settings.json"
        path.write_text(json.dumps(self.__dict__, indent=2, ensure_ascii=False), encoding="utf-8")

    def categories(self) -> list[tuple[str, str, str]]:
        return [
            ("photos", "Photos", self.photos_dir),
            ("pictures", "Pictures", self.pictures_dir),
            ("videos", "Videos", self.videos_dir),
            ("screenshots", "Screenshots", self.screenshots_dir),
            *[(f"location:{i}", Path(path).name or "Locations", path) for i, path in enumerate(self.extra_locations)],
        ]
