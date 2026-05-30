"""Retriever adapters for the retrieval evaluation harness.

Defines the Retriever protocol and its implementations:
- IlikeBaselineRetriever: Pure-python re-implementation of the ILIKE shape
  from the production search_messages (case-insensitive substring match across
  content and media_analysis fields).
- StubSemanticRetriever: Returns empty list deterministically.
- SemanticRetriever: Cosine similarity over local MiniLM dense embeddings,
  with the same scope filtering and deterministic tiebreaker as the baseline.
- HybridRetriever: Reciprocal Rank Fusion (RRF) of the baseline and semantic
  rankings.

MUST NOT import anything from app.* — this module is a pure-python
re-implementation from documentation only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from eval.retrieval.schema import Corpus, CorpusMessage, Scope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from eval.retrieval.embeddings import MiniLMEmbedder


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def message_text(msg: CorpusMessage) -> str:
    """Return the full searchable text for a message.

    Concatenates content with any media_analysis explanation/description/summary
    so semantic scoring sees the same signal the ILIKE baseline can match on.
    Deterministic field order: content, explanation, description, summary.
    """
    parts: list[str] = [msg.content]
    ma = msg.media_analysis
    if ma is not None:
        for field in ("explanation", "description", "summary"):
            val = ma.get(field)
            if isinstance(val, str) and val:
                parts.append(val)
    return " ".join(parts)


def _scope_filter(
    messages: list[CorpusMessage],
    scope: Scope,
    thread_id: str | None,
    topic_id: str | None,
) -> list[CorpusMessage]:
    if scope == "thread":
        return [m for m in messages if m.thread_id == thread_id]
    if scope == "topic":
        return [m for m in messages if m.topic_id == topic_id]
    return list(messages)


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


class SemanticRetriever:
    """Dense-embedding retriever using cosine similarity over MiniLM vectors.

    Builds an L2-normalized embedding matrix over the full corpus text
    (content + media_analysis) once, then for each query embeds it and ranks
    candidates by cosine similarity (= dot product, since vectors are
    normalized). Applies the SAME scope filter as the baseline so the only
    difference being measured is lexical-vs-semantic matching, not scoping.

    Ranking: primary key cosine score DESC; deterministic tiebreaker
    (sent_at DESC, id DESC) matches the baseline so equal-score ties are
    resolved identically across adapters.
    """

    def __init__(self, corpus: Corpus, embedder: "MiniLMEmbedder | None" = None) -> None:
        from eval.retrieval.embeddings import MiniLMEmbedder

        self._corpus = corpus
        self._embedder = embedder or MiniLMEmbedder()
        self._messages = list(corpus.messages)
        texts = [message_text(m) for m in self._messages]
        self._matrix = self._embedder.embed_corpus(texts)  # (N, 384)
        self._index_by_id = {m.id: i for i, m in enumerate(self._messages)}

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
    ) -> list[str]:
        candidates = _scope_filter(self._messages, scope, thread_id, topic_id)
        if not candidates:
            return []

        qvec = self._embedder.embed_query(query)
        scored: list[tuple[float, CorpusMessage]] = []
        for msg in candidates:
            row = self._matrix[self._index_by_id[msg.id]]
            score = float(row @ qvec)  # cosine, vectors are normalized
            scored.append((score, msg))

        # Sort by (score DESC, sent_at DESC, id DESC) deterministically.
        scored.sort(key=lambda t: (t[0], t[1].sent_at, t[1].id), reverse=True)
        return [m.id for _, m in scored[:limit]]


class HybridRetriever:
    """Reciprocal Rank Fusion (RRF) of the baseline and semantic rankings.

    For each candidate, RRF score = sum over rankers of 1 / (k + rank), where
    rank is 1-indexed and k=60 (Cormack et al. default). A document missing
    from a ranker simply contributes nothing from that ranker. This rewards
    documents ranked highly by *either* retriever and is robust to score-scale
    differences between lexical and semantic scorers.

    Both sub-rankers are queried over the full candidate set (limit large)
    before fusion so the fusion sees complete rankings, then the fused list is
    truncated to `limit`. Deterministic tiebreaker (sent_at DESC, id DESC).
    """

    RRF_K = 60

    def __init__(
        self,
        corpus: Corpus,
        embedder: "MiniLMEmbedder | None" = None,
        *,
        baseline: IlikeBaselineRetriever | None = None,
        semantic: SemanticRetriever | None = None,
    ) -> None:
        self._corpus = corpus
        self._baseline = baseline or IlikeBaselineRetriever(corpus)
        self._semantic = semantic or SemanticRetriever(corpus, embedder)
        self._msg_by_id = {m.id: m for m in corpus.messages}

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
    ) -> list[str]:
        full = len(self._corpus.messages)
        kwargs = dict(scope=scope, thread_id=thread_id, topic_id=topic_id, limit=full)
        lex = self._baseline.retrieve(query, **kwargs)
        sem = self._semantic.retrieve(query, **kwargs)

        rrf: dict[str, float] = {}
        for ranking in (lex, sem):
            for rank, mid in enumerate(ranking, start=1):
                rrf[mid] = rrf.get(mid, 0.0) + 1.0 / (self.RRF_K + rank)

        def sort_key(mid: str) -> tuple[float, object, str]:
            msg = self._msg_by_id[mid]
            return (rrf[mid], msg.sent_at, msg.id)

        fused = sorted(rrf.keys(), key=sort_key, reverse=True)
        return fused[:limit]
