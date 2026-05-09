"""Thin wrapper around InsightFace. ML imports are lazy.

The detector is a singleton — initialising the model pack costs a few hundred
milliseconds and ~250 MB RAM, so we keep one instance alive for the lifetime
of the process once indexing has been triggered.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

LOGGER = logging.getLogger(__name__)

_detector: "FaceDetector | None" = None


class RuntimeUnavailable(RuntimeError):
    """Raised when the optional ML stack isn't installed."""


class FaceDetector:
    """Detect + embed faces in a single forward pass via InsightFace."""

    def __init__(self, model_name: str = "buffalo_l", model_dir: Path | None = None) -> None:
        try:
            from insightface.app import FaceAnalysis  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeUnavailable(
                "insightface is not installed. Install with: pip install 'yaga-gallery[faces]'"
            ) from exc

        kwargs: dict = {"name": model_name, "providers": ["CPUExecutionProvider"]}
        if model_dir is not None:
            kwargs["root"] = str(model_dir)
        self._app = FaceAnalysis(**kwargs)
        # det_size: 640 is the buffalo default, balance between recall and CPU cost.
        self._app.prepare(ctx_id=-1, det_size=(640, 640))
        LOGGER.info("FaceDetector ready (model=%s)", model_name)

    def analyze(self, image: "np.ndarray") -> list[dict]:
        """Run detection + embedding on an RGB image array.
        Returns a list of dicts with bbox, landmarks, embedding, quality."""
        results = self._app.get(image)
        out: list[dict] = []
        for r in results:
            x1, y1, x2, y2 = (int(v) for v in r.bbox)
            out.append({
                "bbox": [x1, y1, max(0, x2 - x1), max(0, y2 - y1)],
                "landmarks": r.kps.tolist() if r.kps is not None else None,
                "embedding": r.normed_embedding.astype("float32").tobytes(),
                "quality": float(r.det_score),
            })
        return out


def ensure_detector(model_dir: Path | None = None) -> FaceDetector:
    """Return the process-wide detector, building it on first call."""
    global _detector
    if _detector is None:
        _detector = FaceDetector(model_dir=model_dir)
    return _detector


def reset_detector() -> None:
    """Drop the cached detector — used by tests and after a model swap."""
    global _detector
    _detector = None
