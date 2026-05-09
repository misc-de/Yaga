"""Face detection, recognition and people management.

The ML stack (insightface + onnxruntime) is optional: importing this
subpackage never imports them. Call ``capabilities()`` to see whether
indexing is currently possible, and ``ensure_runtime()`` to perform the
lazy import when the user actually triggers a scan.
"""

from __future__ import annotations

from .models import Face, Person
from .repository import FaceRepository

# Bumped when the embedding model changes. Rows in face_index_state with a
# lower value are re-indexed on the next pass.
EMBEDDING_VERSION = 1


def capabilities() -> dict:
    """Cheap probe — does NOT import the heavy ML libs."""
    import importlib.util
    return {
        "insightface": importlib.util.find_spec("insightface") is not None,
        "onnxruntime": importlib.util.find_spec("onnxruntime") is not None,
        "hdbscan": importlib.util.find_spec("hdbscan") is not None,
        "sklearn": importlib.util.find_spec("sklearn") is not None,
    }


def is_available() -> bool:
    """True if detection + clustering can run right now."""
    caps = capabilities()
    return caps["insightface"] and caps["onnxruntime"] and (caps["hdbscan"] or caps["sklearn"])


__all__ = [
    "Face",
    "Person",
    "FaceRepository",
    "EMBEDDING_VERSION",
    "capabilities",
    "is_available",
]
