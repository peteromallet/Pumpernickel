"""Tiny on-disk embedding cache for the retrieval eval harness.

Embeddings are cached per (backend name, text) so reruns of the harness are
cheap and require no network. The cache is a single .npz-style directory of
JSON-indexed float32 vectors keyed by a stable hash.

This module has zero app.* imports and uses only numpy + stdlib.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np


DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / ".embedding_cache"


def _key(backend_name: str, text: str) -> str:
    h = hashlib.sha256(f"{backend_name}\x00{text}".encode("utf-8")).hexdigest()
    return h


class EmbeddingCache:
    """File-backed cache mapping (backend, text) -> vector.

    Stored as one .npy file per key plus a small index.json for diagnostics.
    Misses are computed by the caller and written back via `put`.
    """

    def __init__(self, backend_name: str, cache_dir: Path | None = None) -> None:
        self._backend = backend_name
        self._dir = (cache_dir or DEFAULT_CACHE_DIR) / _safe(backend_name)
        self._dir.mkdir(parents=True, exist_ok=True)

    def get(self, text: str) -> np.ndarray | None:
        path = self._dir / f"{_key(self._backend, text)}.npy"
        if path.exists():
            return np.load(path)
        return None

    def put(self, text: str, vector: np.ndarray) -> None:
        path = self._dir / f"{_key(self._backend, text)}.npy"
        np.save(path, vector.astype(np.float32))

    def embed_cached(self, texts: list[str], embed_fn) -> np.ndarray:
        """Return stacked embeddings for texts, computing only cache misses.

        Args:
            texts: input strings (order preserved in output).
            embed_fn: callable(list[str]) -> (M, dim) ndarray for the misses.
        """
        results: list[np.ndarray | None] = [self.get(t) for t in texts]
        miss_idx = [i for i, r in enumerate(results) if r is None]
        if miss_idx:
            miss_texts = [texts[i] for i in miss_idx]
            computed = embed_fn(miss_texts)
            for j, i in enumerate(miss_idx):
                vec = np.asarray(computed[j], dtype=np.float32)
                self.put(texts[i], vec)
                results[i] = vec
        return np.vstack([np.asarray(r, dtype=np.float32) for r in results])


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
