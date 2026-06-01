from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _uv_run(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", *args, "python", "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_openai_embedder_import_path_is_available_via_uv() -> None:
    result = _uv_run(
        "import openai; "
        "from eval.retrieval.embeddings import OpenAIEmbedder; "
        "assert openai.OpenAI is not None; "
        "assert OpenAIEmbedder().model_name == 'text-embedding-3-small'"
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_direct_db_vector_import_path_is_available_via_uv() -> None:
    result = _uv_run(
        "import psycopg; "
        "from pgvector.psycopg import register_vector; "
        "from eval.retrieval.adapters import Corpus, CorpusMessage, DbBackedRetriever; "
        "from datetime import datetime, timezone; "
        "import os; "
        "os.environ['DIRECT_DATABASE_URL'] = "
        "'postgresql://postgres:postgres@localhost:5432/mediator'; "
        "corpus = Corpus(messages=[CorpusMessage("
        "id='m001', thread_id='thread-1', topic_id='topic-1', sender='A', recipient='B', "
        "sent_at=datetime(2025, 1, 1, tzinfo=timezone.utc), content='hello'"
        ")]); "
        "assert psycopg is not None; "
        "assert callable(register_vector); "
        "assert DbBackedRetriever(corpus) is not None",
        "--extra",
        "retrieval-db",
    )

    assert result.returncode == 0, result.stderr or result.stdout
