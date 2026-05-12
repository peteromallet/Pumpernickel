"""Tests for scripts/lint_artifact_reads.py (T16)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_lint_artifact_reads_clean():
    """The lint script must exit 0 when run against the current app/ directory."""
    script = Path(__file__).resolve().parent.parent / "scripts" / "lint_artifact_reads.py"
    result = subprocess.run(
        [sys.executable, str(script), "--dir", "app"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=30,
    )
    assert result.returncode == 0, (
        f"lint_artifact_reads exited {result.returncode}:\n"
        f"STDERR: {result.stderr}\n"
        f"STDOUT: {result.stdout}"
    )