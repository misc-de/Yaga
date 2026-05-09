"""Cluster un-assigned face embeddings into groups.

Two backends, in order of preference:
  1. HDBSCAN — handles variable density, gives a "noise" label for outliers
  2. scikit-learn DBSCAN — good fallback, slightly worse on uneven cluster sizes

The clusterer never touches person_id; it only writes cluster_id. Promoting
a cluster to a named person is a separate UI step.
"""

from __future__ import annotations

import logging

from ..database import Database
from .repository import FaceRepository

LOGGER = logging.getLogger(__name__)

# Cosine distance threshold for "same person". 0.4 is a conservative starting
# point for ArcFace embeddings; tighten if you see merges, loosen if you see
# the same person split across clusters.
COSINE_EPS = 0.4
MIN_CLUSTER_SIZE = 3


class FaceClusterer:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.repo = FaceRepository(database)

    def recluster(self) -> dict:
        """Recompute clusters for all unassigned faces. Returns a small stats
        dict: {clusters, noise, total}."""
        try:
            import numpy as np
        except ImportError:
            LOGGER.warning("numpy missing — clustering skipped")
            return {"clusters": 0, "noise": 0, "total": 0}

        rows = self.repo.all_unassigned_embeddings()
        if not rows:
            return {"clusters": 0, "noise": 0, "total": 0}

        face_ids = [face_id for face_id, _ in rows]
        # Embeddings come from InsightFace already L2-normalised, so cosine
        # distance == 1 - dot product. Reshape into (N, 512) float32.
        embeddings = np.frombuffer(b"".join(blob for _, blob in rows), dtype="float32")
        embeddings = embeddings.reshape(len(face_ids), -1)

        labels = self._cluster(embeddings)
        if labels is None:
            return {"clusters": 0, "noise": 0, "total": len(face_ids)}

        # HDBSCAN/DBSCAN both use -1 for noise. Translate to None so it shows
        # up in DB as NULL (= still ungrouped).
        assignments: list[tuple[int, int | None]] = []
        cluster_ids: set[int] = set()
        noise = 0
        for face_id, label in zip(face_ids, labels):
            if label < 0:
                assignments.append((face_id, None))
                noise += 1
            else:
                cluster_ids.add(int(label))
                assignments.append((face_id, int(label)))

        self.repo.set_cluster_ids(assignments)
        self.database.commit()
        LOGGER.info(
            "Clustering: %d faces → %d clusters, %d noise",
            len(face_ids), len(cluster_ids), noise,
        )
        return {"clusters": len(cluster_ids), "noise": noise, "total": len(face_ids)}

    def _cluster(self, embeddings) -> list[int] | None:
        """Try HDBSCAN first, fall back to DBSCAN. Returns labels list."""
        try:
            import hdbscan  # type: ignore[import-not-found]
            clusterer = hdbscan.HDBSCAN(
                min_cluster_size=MIN_CLUSTER_SIZE,
                min_samples=2,
                metric="euclidean",  # on L2-normalised vectors ~= cosine
                cluster_selection_epsilon=COSINE_EPS,
            )
            return list(clusterer.fit_predict(embeddings))
        except ImportError:
            pass

        try:
            from sklearn.cluster import DBSCAN  # type: ignore[import-not-found]
            clusterer = DBSCAN(eps=COSINE_EPS, min_samples=MIN_CLUSTER_SIZE, metric="cosine")
            return list(clusterer.fit_predict(embeddings))
        except ImportError:
            LOGGER.error("Neither hdbscan nor scikit-learn installed — cannot cluster")
            return None
