"""Embedding backends for the semantic retriever.

This module is intentionally self-contained and MUST NOT import from app.*.
It provides a small `Embedder` protocol plus three concrete backends, chosen
in this priority order by `get_default_embedder()`:

1. OpenAIEmbedder       -- text-embedding-3-small, used ONLY if OPENAI_API_KEY
                           is already present in the environment. The key is
                           never logged or hardcoded.
2. SentenceTransformerEmbedder
                        -- local all-MiniLM-L6-v2 (sentence-transformers).
                           No network at query time once the model is cached.
3. TfidfFloorEmbedder   -- a TF-IDF / char-ngram vector. This is NOT a real
                           embedding; it is a deterministic "floor" sanity
                           backend used only when neither real backend is
                           available. It is clearly labelled as such.

All backends expose `embed(texts: list[str]) -> np.ndarray` returning an
(N, dim) float32 array, plus a stable `.name` used for the on-disk cache key.

Embeddings are cached to disk (see `cache.py`) keyed by backend name + a hash
of the input text, so reruns are cheap and require no network.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class Embedder(Protocol):
    """Protocol for embedding backends."""

    #: Stable identifier used as part of the on-disk cache key.
    name: str

    def embed(self, texts: list[str]) -> np.ndarray:
        """Embed a list of texts into an (N, dim) float32 array."""
        ...


# ---------------------------------------------------------------------------
# OpenAI backend (text-embedding-3-small)
# ---------------------------------------------------------------------------


class OpenAIEmbedder:
    """OpenAI text-embedding-3-small backend.

    Only usable when OPENAI_API_KEY is present in the environment. The key is
    read by the openai SDK itself; this class never reads, stores, logs, or
    prints it.
    """

    name = "openai-text-embedding-3-small"

    def __init__(self, model: str = "text-embedding-3-small") -> None:
        from openai import OpenAI  # lazy import

        self._model = model
        self._client = OpenAI()  # reads OPENAI_API_KEY from env

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1536), dtype=np.float32)
        # Replace empty strings; the API rejects empty inputs.
        cleaned = [t if t.strip() else " " for t in texts]
        resp = self._client.embeddings.create(model=self._model, input=cleaned)
        vecs = [d.embedding for d in resp.data]
        return np.asarray(vecs, dtype=np.float32)


# ---------------------------------------------------------------------------
# Local sentence-transformers backend (all-MiniLM-L6-v2)
# ---------------------------------------------------------------------------


class SentenceTransformerEmbedder:
    """Local sentence-transformers backend (default: all-MiniLM-L6-v2)."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer  # lazy import

        self._model_name = model_name
        self._model = SentenceTransformer(model_name)
        # Normalise the name for cache keying.
        self.name = f"sentence-transformers-{model_name}"

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            dim = self._model.get_sentence_embedding_dimension()
            return np.zeros((0, dim), dtype=np.float32)
        vecs = self._model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=False,
            show_progress_bar=False,
        )
        return np.asarray(vecs, dtype=np.float32)


# ---------------------------------------------------------------------------
# TF-IDF / char-ngram FLOOR backend (NOT a real embedding)
# ---------------------------------------------------------------------------


class TfidfFloorEmbedder:
    """Deterministic TF-IDF char-ngram vectoriser used as a sanity FLOOR.

    This is NOT a semantic embedding. It captures lexical / sub-word overlap
    only and exists solely so the harness can still produce *some* dense-vector
    comparison when neither OpenAI nor sentence-transformers is available.
    Results from this backend must be labelled "TF-IDF floor (not a real
    embedding)" in any report.

    It must be `fit` on the full corpus + queries before embedding so the
    vocabulary is shared; the SemanticRetriever handles that by fitting on the
    corpus and transforming queries against the same fitted vectoriser.
    """

    name = "tfidf-char-ngram-floor"

    def __init__(self) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer  # lazy

        self._vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(3, 5),
            lowercase=True,
        )
        self._fitted = False

    def fit(self, texts: list[str]) -> None:
        self._vectorizer.fit(texts if texts else [" "])
        self._fitted = True

    def embed(self, texts: list[str]) -> np.ndarray:
        if not self._fitted:
            # Fit lazily on whatever we're given (degrades gracefully).
            self.fit(texts)
        if not texts:
            return np.zeros((0, len(self._vectorizer.vocabulary_)), dtype=np.float32)
        mat = self._vectorizer.transform(texts)
        return mat.toarray().astype(np.float32)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def get_default_embedder() -> tuple[Embedder, bool]:
    """Pick the best available embedding backend.

    Returns:
        (embedder, is_real_embedding) where is_real_embedding is False only
        for the TF-IDF floor backend.

    Priority:
        1. OpenAI text-embedding-3-small  (iff OPENAI_API_KEY already set)
        2. sentence-transformers all-MiniLM-L6-v2  (iff importable)
        3. TF-IDF char-ngram floor  (sklearn; labelled not-a-real-embedding)
    """
    if os.environ.get("OPENAI_API_KEY"):
        try:
            return OpenAIEmbedder(), True
        except Exception:
            pass  # fall through to local backends

    try:
        import sentence_transformers  # noqa: F401

        return SentenceTransformerEmbedder(), True
    except Exception:
        pass

    return TfidfFloorEmbedder(), False
