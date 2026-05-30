"""Retriever adapters for the retrieval evaluation harness.

Defines the Retriever protocol and two implementations:
- IlikeBaselineRetriever: Pure-python re-implementation of the ILIKE shape
  from the production search_messages (case-insensitive substring match across
  content and media_analysis fields).
- StubSemanticRetriever: Returns empty list deterministically.

MUST NOT import anything from app.* — this module is a pure-python
re-implementation from documentation only.
"""

from __future__ import annotations

from typing import Protocol

from eval.retrieval.schema import Corpus, CorpusMessage, Scope


class Retriever(Protocol):
    """Protocol for retrieval adapters.

    All retrievers must implement this interface so the runner can swap
    between baseline and semantic implementations.
    """

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None,
        topic_id: str | None,
        limit: int,
    ) -> list[str]:
        """Retrieve ranked message ids for a query.

        Args:
            query: The search query string.
            scope: Filter scope ('thread', 'topic', or 'all').
            thread_id: Required for scope=='thread', ignored otherwise.
            topic_id: Required for scope=='topic', ignored otherwise.
            limit: Maximum number of results to return.

        Returns:
            Ordered list of message ids (rank 1 = index 0), truncated to limit.
        """
        ...


class IlikeBaselineRetriever:
    """Pure-python re-implementation of production ILIKE search semantics.

    Matches case-insensitive substrings against:
        1. message.content
        2. media_analysis.explanation (if present)
        3. media_analysis.description (if present)
        4. media_analysis.summary (if present)

    Applies scope filter:
        - 'thread': Only messages with matching thread_id.
        - 'topic': Only messages with matching topic_id.
        - 'all': No filter.

    Results are ordered by (sent_at DESC, id DESC) for deterministic ranking
    with tiebreaker per SD3 / callers-3.
    """

    def __init__(self, corpus: Corpus) -> None:
        self._corpus = corpus

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
    ) -> list[str]:
        query_lower = query.lower()

        # Apply scope filter first.
        candidates = self._corpus.messages
        if scope == "thread":
            candidates = [m for m in candidates if m.thread_id == thread_id]
        elif scope == "topic":
            candidates = [m for m in candidates if m.topic_id == topic_id]
        # scope == 'all': no filter

        # Case-insensitive substring match against content and media_analysis.
        matches = []
        for msg in candidates:
            if query_lower in msg.content.lower():
                matches.append(msg)
                continue

            ma = msg.media_analysis
            if ma is not None:
                # Check each media_analysis field.
                explanation = ma.get("explanation")
                if isinstance(explanation, str) and query_lower in explanation.lower():
                    matches.append(msg)
                    continue

                description = ma.get("description")
                if isinstance(description, str) and query_lower in description.lower():
                    matches.append(msg)
                    continue

                summary = ma.get("summary")
                if isinstance(summary, str) and query_lower in summary.lower():
                    matches.append(msg)
                    continue

        # Order by (sent_at DESC, id DESC) per SD3.
        matches.sort(key=lambda m: (m.sent_at, m.id), reverse=True)

        # Slice to limit.
        return [m.id for m in matches[:limit]]


class StubSemanticRetriever:
    """Deterministic stub retriever that always returns an empty list.

    Used as a placeholder for the semantic retriever implementation.
    Returns [] for every query, scope, and limit combination.
    """

    def __init__(self, corpus: Corpus) -> None:
        self._corpus = corpus

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
    ) -> list[str]:
        return []


# ---------------------------------------------------------------------------
# Shared helpers for semantic / hybrid retrievers
# ---------------------------------------------------------------------------


def message_text(msg: CorpusMessage) -> str:
    """Build the text used to embed a message.

    Concatenates content with the same media_analysis fields the ILIKE
    baseline searches (explanation / description / summary), so the semantic
    and keyword retrievers see the same source material — the only difference
    is matching by meaning vs. matching by substring.
    """
    parts: list[str] = [msg.content]
    ma = msg.media_analysis
    if isinstance(ma, dict):
        for field in ("explanation", "description", "summary"):
            val = ma.get(field)
            if isinstance(val, str) and val.strip():
                parts.append(val)
    return "\n".join(parts)


def _scope_candidates(
    corpus: Corpus, scope: Scope, thread_id: str | None, topic_id: str | None
) -> list[CorpusMessage]:
    """Apply the same scope filter the baseline uses."""
    candidates = corpus.messages
    if scope == "thread":
        return [m for m in candidates if m.thread_id == thread_id]
    if scope == "topic":
        return [m for m in candidates if m.topic_id == topic_id]
    return list(candidates)


class SemanticRetriever:
    """Dense-vector semantic retriever.

    Embeds every corpus message once (cached to disk), embeds the query at
    retrieve time, and ranks scope-filtered candidates by cosine similarity.

    Scope filtering is identical to ``IlikeBaselineRetriever`` so the two are
    directly comparable; the only difference is the scoring function.

    Ranking is by cosine descending, with a deterministic ``(sent_at, id)``
    DESC tiebreaker matching the baseline so equal scores never produce
    unstable ordering.
    """

    def __init__(
        self,
        corpus: Corpus,
        embedder=None,
        *,
        cache_dir=None,
        use_cache: bool = True,
    ) -> None:
        import numpy as np

        from eval.retrieval.embeddings import get_default_embedder

        self._corpus = corpus
        self._np = np

        if embedder is None:
            embedder, is_real = get_default_embedder()
            self.is_real_embedding = is_real
        else:
            self.is_real_embedding = getattr(embedder, "is_real_embedding", True)
        self._embedder = embedder
        self.backend_name = getattr(embedder, "name", embedder.__class__.__name__)

        ids = [m.id for m in corpus.messages]
        texts = [message_text(m) for m in corpus.messages]

        # TF-IDF floor must be fit on the full corpus vocabulary first.
        if hasattr(embedder, "fit") and not getattr(embedder, "_fitted", False):
            embedder.fit(texts)

        matrix = self._embed(texts, cache_dir=cache_dir, use_cache=use_cache)
        self._ids = ids
        self._id_to_row = {mid: i for i, mid in enumerate(ids)}
        self._matrix = self._l2_normalize(matrix)

    def _embed(self, texts, *, cache_dir, use_cache):
        # The TF-IDF floor backend shares a fitted vocabulary across calls and
        # is cheap, so caching it to disk is both unnecessary and unsafe
        # (vectors are vocabulary-relative). Only cache real embedders.
        if use_cache and self.is_real_embedding:
            from eval.retrieval.cache import EmbeddingCache

            cache = EmbeddingCache(self.backend_name, cache_dir=cache_dir)
            return cache.embed_cached(texts, self._embedder.embed)
        return self._embedder.embed(texts)

    def _l2_normalize(self, mat):
        np = self._np
        mat = np.asarray(mat, dtype=np.float32)
        if mat.size == 0:
            return mat
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return mat / norms

    def _embed_query(self, query: str):
        vec = self._embedder.embed([query])
        return self._l2_normalize(vec)[0]

    def score_candidates(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
    ) -> list[tuple[CorpusMessage, float]]:
        """Return (message, cosine) for scope-filtered candidates, ranked."""
        candidates = _scope_candidates(self._corpus, scope, thread_id, topic_id)
        if not candidates:
            return []
        qvec = self._embed_query(query)
        scored: list[tuple[CorpusMessage, float]] = []
        for msg in candidates:
            row = self._matrix[self._id_to_row[msg.id]]
            scored.append((msg, float(qvec @ row)))
        # Cosine DESC, then (sent_at, id) DESC tiebreaker (baseline parity).
        scored.sort(key=lambda x: (x[0].sent_at, x[0].id), reverse=True)
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
    ) -> list[str]:
        scored = self.score_candidates(
            query, scope, thread_id=thread_id, topic_id=topic_id
        )
        return [m.id for m, _ in scored[:limit]]


class HybridRetriever:
    """Reciprocal Rank Fusion (RRF) of ILIKE keyword and semantic rankings.

    This is the retriever the Xen design proposes. For each candidate, RRF
    sums ``1 / (k + rank)`` across the keyword and semantic rankings (rank is
    1-indexed; a retriever that does not rank a document contributes nothing
    for it). The standard ``k = 60`` constant is used.

    Scope filtering is shared with both sub-retrievers. Final tiebreaker is
    ``(sent_at, id)`` DESC for determinism.
    """

    def __init__(
        self,
        corpus: Corpus,
        semantic: SemanticRetriever | None = None,
        *,
        embedder=None,
        rrf_k: int = 60,
        cache_dir=None,
        use_cache: bool = True,
    ) -> None:
        self._corpus = corpus
        self._rrf_k = rrf_k
        self._baseline = IlikeBaselineRetriever(corpus)
        self._semantic = semantic or SemanticRetriever(
            corpus, embedder=embedder, cache_dir=cache_dir, use_cache=use_cache
        )
        self.backend_name = self._semantic.backend_name
        self.is_real_embedding = self._semantic.is_real_embedding

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
    ) -> list[str]:
        k = self._rrf_k

        # Keyword ranking (already scope-filtered + ordered).
        kw_ranked = self._baseline.retrieve(
            query, scope, thread_id=thread_id, topic_id=topic_id, limit=10**9
        )
        # Semantic ranking over the same scope.
        sem_scored = self._semantic.score_candidates(
            query, scope, thread_id=thread_id, topic_id=topic_id
        )
        sem_ranked = [m.id for m, _ in sem_scored]

        rrf: dict[str, float] = {}
        for rank, mid in enumerate(kw_ranked, start=1):
            rrf[mid] = rrf.get(mid, 0.0) + 1.0 / (k + rank)
        for rank, mid in enumerate(sem_ranked, start=1):
            rrf[mid] = rrf.get(mid, 0.0) + 1.0 / (k + rank)

        # Deterministic tiebreaker: (sent_at, id) DESC.
        by_id = {m.id: m for m in self._corpus.messages}
        fused = sorted(
            rrf.items(),
            key=lambda kv: (kv[1], by_id[kv[0]].sent_at, kv[0]),
            reverse=True,
        )
        return [mid for mid, _ in fused[:limit]]
