"""Local, offline sentence-embedding backend for the retrieval eval harness.

Wraps sentence-transformers `all-MiniLM-L6-v2` (384-dim) so the semantic and
hybrid retrievers can score messages by cosine similarity. Everything runs
locally: the model is loaded from the on-disk Hugging Face cache and corpus
embeddings are cached to a `.npy` file keyed by a content hash, so repeated
runs are deterministic and require no network.

MUST NOT import anything from app.* — this module is a self-contained eval
utility.

Design notes:
- Determinism: the model runs in eval mode with a fixed input order; MiniLM is
  deterministic on CPU for a given input. We additionally cache the corpus
  matrix to disk so re-runs read identical vectors.
- Offline: we set HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE before importing the
  library so a missing-network environment never blocks on a download attempt
  (the model is expected to already be in the local cache).
- Lazy import: `sentence_transformers` and `numpy` are imported inside the
  class so that the baseline adapter and the rest of the harness stay
  dependency-free for callers that never touch semantics.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np  # noqa: F401


DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "reports" / ".emb_cache"


class MiniLMEmbedder:
    """Deterministic, offline embedder over all-MiniLM-L6-v2.

    Embeddings are L2-normalized so that a plain dot product equals cosine
    similarity. Corpus embeddings are cached to disk keyed by a hash of the
    (model_name, ordered texts) so re-runs are byte-identical without recompute.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        *,
        cache_dir: Path | None = None,
    ) -> None:
        self.model_name = model_name
        self._cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        self._model = None  # lazy

    # -- model loading -----------------------------------------------------

    def _ensure_model(self):
        if self._model is None:
            # Force offline so a download attempt never wedges the run.
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device="cpu")
            self._model.eval()
        return self._model

    # -- embedding ---------------------------------------------------------

    def _encode(self, texts: list[str]):
        import numpy as np

        if not texts:
            return np.zeros((0, 384), dtype=np.float32)
        model = self._ensure_model()
        vecs = model.encode(
            texts,
            batch_size=64,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vecs.astype(np.float32)

    def embed_query(self, text: str):
        """Return an L2-normalized 1-D embedding for a single query string."""
        return self._encode([text])[0]

    def embed_corpus(self, texts: list[str]):
        """Return an L2-normalized (N, 384) matrix for ordered corpus texts.

        Cached to disk keyed by a hash of (model_name, texts). The cache is a
        plain .npy matrix; if the corpus text changes the hash changes and a
        fresh matrix is computed and stored.
        """
        import numpy as np

        key = hashlib.sha256(
            ("␟".join([self.model_name, *texts])).encode("utf-8")
        ).hexdigest()[:16]
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self._cache_dir / f"corpus_{key}.npy"

        if cache_path.exists():
            return np.load(cache_path)

        matrix = self._encode(texts)
        np.save(cache_path, matrix)
        return matrix
