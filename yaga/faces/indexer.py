"""Background pass that runs detection over un-indexed media items.

Mirrors the structure of MediaScanner: idempotent, batched commits, runs
in the scanner thread. Keeps every heavy import inside ``index_pending``
so importing this module never pulls in numpy/insightface.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from ..config import THUMB_DIR
from ..database import Database
from . import EMBEDDING_VERSION
from .repository import FaceRepository

LOGGER = logging.getLogger(__name__)

# How often to commit during a long scan. Same rationale as the NC scanner —
# release the lock so the UI thread can read between batches.
COMMIT_EVERY = 25
SLEEP_BETWEEN_BATCHES = 0.01

# Faces below this det_score are noise (motion blur, side profiles, false
# positives on textures). Tuning point — 0.6 is the InsightFace default for
# usable embeddings.
MIN_QUALITY = 0.6

ProgressCb = Callable[[int, int], None]


class FaceIndexer:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.repo = FaceRepository(database)
        self._face_thumb_dir = THUMB_DIR / "faces"
        self._face_thumb_dir.mkdir(parents=True, exist_ok=True)

    def index_pending(
        self,
        progress: ProgressCb | None = None,
        limit: int | None = None,
    ) -> int:
        """Run detection over media items missing/stale in face_index_state.
        Returns the number of media items processed."""
        # Lazy imports — keep the module importable without the ML stack.
        try:
            import numpy as np
            from PIL import Image
        except ImportError as exc:
            LOGGER.error("Face indexing dependencies missing: %s", exc)
            return 0

        from .detector import ensure_detector, RuntimeUnavailable
        try:
            detector = ensure_detector()
        except RuntimeUnavailable as exc:
            LOGGER.warning("Skipping face indexing: %s", exc)
            return 0

        pending = self.repo.pending_paths(EMBEDDING_VERSION, limit=limit)
        if not pending:
            return 0

        LOGGER.info("Face indexing %d pending media item(s)", len(pending))
        processed = 0
        started = time.time()

        for path_str, category, mtime in pending:
            path = Path(path_str)
            if not path.exists():
                # Stale entry — let the next prune_missing pass clean it up.
                continue
            try:
                img = Image.open(path).convert("RGB")
                arr = np.array(img)
                detections = detector.analyze(arr)
            except Exception as e:
                LOGGER.debug("Face detection failed for %s: %s", path_str, e)
                self.repo.mark_indexed(path_str, category, mtime, EMBEDDING_VERSION)
                processed += 1
                continue

            kept = []
            for det in detections:
                if det["quality"] < MIN_QUALITY:
                    continue
                thumb_path = self._save_face_thumb(img, det["bbox"], path.stem)
                kept.append({**det, "thumb_path": thumb_path})

            self.repo.replace_faces(path_str, category, kept)
            self.repo.mark_indexed(path_str, category, mtime, EMBEDDING_VERSION)
            processed += 1

            if progress and processed % 10 == 0:
                progress(processed, len(pending))

            if processed % COMMIT_EVERY == 0:
                self.database.commit()
                time.sleep(SLEEP_BETWEEN_BATCHES)

        self.database.commit()
        LOGGER.info(
            "Face indexing finished: %d media item(s) in %.2fs",
            processed, time.time() - started,
        )
        if progress:
            progress(processed, len(pending))
        return processed

    def _save_face_thumb(self, image, bbox, stem: str) -> str | None:
        """Crop the bbox out of the source PIL image and save as a small
        face thumbnail. Returns absolute path or None on failure."""
        try:
            x, y, w, h = bbox
            pad = int(0.2 * max(w, h))
            left = max(0, x - pad)
            top = max(0, y - pad)
            right = min(image.width, x + w + pad)
            bottom = min(image.height, y + h + pad)
            crop = image.crop((left, top, right, bottom))
            crop.thumbnail((160, 160))
            # Use a stable-ish filename; we accept collisions because faces
            # for the same media are wiped by replace_faces() before inserts.
            import hashlib
            digest = hashlib.sha1(f"{stem}-{x}-{y}-{w}-{h}".encode()).hexdigest()[:16]
            out = self._face_thumb_dir / f"{digest}.jpg"
            crop.save(out, "JPEG", quality=85)
            return str(out)
        except Exception as e:
            LOGGER.debug("Face thumb crop failed: %s", e)
            return None
