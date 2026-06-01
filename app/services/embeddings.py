"""Shared embedding contract for Xen M1 retrieval.

This module is the single source of truth for canonical message text,
content hashes, vector normalization, and provider-specific embedders.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from app.config import Settings, get_settings


DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_EMBEDDING_DIMENSION = 1536
LOCAL_BGE_SMALL_DIMENSION = 384

_CANONICAL_MEDIA_FIELDS = ("explanation", "description", "summary")


class EmbeddingError(RuntimeError):
    """Raised when an embedder cannot satisfy the shared embedding contract."""


@runtime_checkable
class Embedder(Protocol):
    """Async embedding provider interface used by workers and retrieval."""

    model_name: str
    dimension: int

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one L2-normalized vector per input text, preserving order."""


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _normalize_text_for_hash(text: str) -> str:
    return unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")


def canonical_embedding_text(
    content: str | None = None,
    media_analysis: Mapping[str, Any] | None = None,
) -> str:
    """Return the canonical text embedded and hashed for a message.

    Field order mirrors migration 0056:
    content, media_analysis.explanation, media_analysis.description,
    media_analysis.summary. Missing values are treated as empty strings and the
    four fields are joined with a single newline.
    """

    media = media_analysis or {}
    fields = [_coerce_text(content)]
    fields.extend(_coerce_text(media.get(field)) for field in _CANONICAL_MEDIA_FIELDS)
    return "\n".join(fields)


def content_hash(text: str) -> str:
    """Return the canonical SHA-256 hash for already-canonical embedding text."""

    return hashlib.sha256(_normalize_text_for_hash(text).encode("utf-8")).hexdigest()


def canonical_content_hash(
    content: str | None = None,
    media_analysis: Mapping[str, Any] | None = None,
) -> str:
    """Return the SHA-256 hash for ``canonical_embedding_text(...)``."""

    return content_hash(canonical_embedding_text(content, media_analysis))


def normalize_vector(vector: Sequence[float], *, dimension: int) -> list[float]:
    """Validate dimension and return an L2-normalized vector."""

    values = [float(value) for value in vector]
    if len(values) != dimension:
        raise ValueError(f"embedding dimension mismatch: expected {dimension}, got {len(values)}")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("embedding vector contains non-finite values")
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        raise ValueError("embedding vector must not be all zeros")
    return [value / norm for value in values]


def validate_vectors(vectors: Sequence[Sequence[float]], *, dimension: int) -> list[list[float]]:
    """Normalize and validate a batch of vectors."""

    return [normalize_vector(vector, dimension=dimension) for vector in vectors]


class OpenAIEmbedder:
    """Hosted async OpenAI embedder for ``text-embedding-3-small``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = DEFAULT_OPENAI_EMBEDDING_MODEL,
        dimension: int = DEFAULT_OPENAI_EMBEDDING_DIMENSION,
        timeout_s: float | None = None,
    ) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            kwargs: dict[str, Any] = {}
            if self._api_key is not None:
                kwargs["api_key"] = self._api_key
            if self._timeout_s is not None:
                kwargs["timeout"] = self._timeout_s
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._ensure_client()
        response = await client.embeddings.create(
            model=self.model_name,
            input=list(texts),
            dimensions=self.dimension,
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [item.embedding for item in ordered]
        if len(vectors) != len(texts):
            raise EmbeddingError(f"OpenAI returned {len(vectors)} vectors for {len(texts)} inputs")
        return validate_vectors(vectors, dimension=self.dimension)


class DeterministicFakeEmbedder:
    """Deterministic async test embedder with no network or model dependency."""

    model_name = "deterministic-fake"

    def __init__(self, *, dimension: int = 64) -> None:
        if dimension < 1:
            raise ValueError("dimension must be positive")
        self.dimension = dimension

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        canonical = _normalize_text_for_hash(text).casefold()
        tokens = canonical.split() or [canonical]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:8], "big") % self.dimension
            sign = 1.0 if digest[8] % 2 == 0 else -1.0
            vector[bucket] += sign
        return normalize_vector(vector, dimension=self.dimension)


class LocalBgeSmallEmbedder:
    """Lazy local bge-small embedder.

    ``sentence_transformers`` is imported only when this provider is used, so
    normal test and hosted OpenAI paths do not pull the local model dependency.
    """

    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-small-en-v1.5",
        dimension: int = LOCAL_BGE_SMALL_DIMENSION,
    ) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self._model: Any | None = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise EmbeddingError(
                    "Local bge-small embeddings require the optional "
                    "`sentence-transformers` dependency"
                ) from exc
            self._model = SentenceTransformer(self.model_name, device="cpu")
            self._model.eval()
        return self._model

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_sync, list(texts))

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        vectors = model.encode(
            texts,
            batch_size=64,
            convert_to_numpy=False,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return validate_vectors(vectors, dimension=self.dimension)


def embedder_from_settings(settings: Settings | None = None) -> Embedder:
    """Create the configured embedder without touching optional providers early."""

    settings = settings or get_settings()
    provider = settings.embedding_provider
    model = settings.embedding_model
    dimension = settings.embedding_dimension
    if provider == "openai":
        return OpenAIEmbedder(
            api_key=settings.openai_api_key.get_secret_value(),
            model_name=model,
            dimension=dimension,
            timeout_s=settings.query_embed_timeout_s,
        )
    if provider == "local":
        return LocalBgeSmallEmbedder(model_name=model, dimension=dimension)
    raise ValueError(
        "No built-in embedder is registered for "
        f"provider={provider!r}; inject a custom Embedder for this provider"
    )
