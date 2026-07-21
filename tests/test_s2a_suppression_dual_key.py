"""bot_id-only newer-inbound suppression tests.

Verifies:
- EXISTS query includes `bot_id = $4` filter (bot_id IS NULL removed)
- Both agentic.py and read_tools.py suppression paths use the bot_id-only filter
- _newer_inbound_exists requires `bot_id: str` (no default, no None)
- No `bot_id IS NULL` residues remain
"""

from __future__ import annotations

import pytest


class TestSuppressionBotIdOnlyFilter:
    """Verify the EXISTS subquery contains the bot_id-only filter."""

    def test_agentic_suppression_has_bot_id_filter(self):
        """agentic.py _newer_inbound_exists includes `bot_id = $4`, NOT `OR bot_id IS NULL`."""
        content = open("app/services/agentic.py").read()
        assert "bot_id = $4" in content, (
            "agentic.py suppression query must filter by bot_id = $4"
        )
        assert "OR bot_id IS NULL" not in content, (
            "agentic.py must NOT contain legacy OR bot_id IS NULL fallback"
        )

    def test_read_tools_suppression_has_bot_id_filter(self):
        """read_tools.py incremental-send suppression includes the bot_id-only filter."""
        content = open("app/services/tools/read_tools.py").read()
        assert ("bot_id = $4" in content or "bot_id = $ N" in content), (
            "read_tools.py suppression query must filter by bot_id"
        )
        assert "OR bot_id IS NULL" not in content, (
            "read_tools.py must NOT contain legacy OR bot_id IS NULL fallback"
        )

    def test_suppression_requires_bot_id_parameter(self):
        """_newer_inbound_exists in agentic.py requires `bot_id: str` (no default, no None)."""
        content = open("app/services/agentic.py").read()
        # Find _newer_inbound_exists function signature specifically
        lines = content.split("\n")
        in_func = False
        func_lines = []
        for line in lines:
            if "async def _newer_inbound_exists" in line:
                in_func = True
            if in_func:
                func_lines.append(line)
                if in_func and line.strip().endswith(":"):
                    if not line.strip().startswith("async def"):
                        break
                    if len(func_lines) > 1 and func_lines[-1].strip() == ") -> bool:":
                        break
                    # Continue collecting until we see the end of the signature
                    if line.strip() == ") -> bool:":
                        break
                if line.strip().endswith(") -> bool:") or line.strip() == ") -> bool:":
                    break
                if in_func and len(func_lines) > 10:
                    break
        func_sig = "\n".join(func_lines)
        assert "bot_id: str" in func_sig, (
            f"_newer_inbound_exists must require bot_id: str, got sig:\n{func_sig}"
        )
        assert "bot_id: str | None = None" not in func_sig, (
            f"_newer_inbound_exists must NOT have a None default, got sig:\n{func_sig}"
        )

    def test_no_null_bot_id_filter(self):
        """Legacy suppression fallback must stay absent from both code paths."""
        agentic_content = open("app/services/agentic.py").read()
        read_tools_content = open("app/services/tools/read_tools.py").read()
        assert "OR bot_id IS NULL" not in agentic_content, (
            "agentic.py must NOT contain legacy OR bot_id IS NULL fallback"
        )
        assert "OR bot_id IS NULL" not in read_tools_content, (
            "read_tools.py must NOT contain legacy OR bot_id IS NULL fallback"
        )

    def test_call_sites_pass_bot_id(self):
        """Call sites of _newer_inbound_exists pass bot_id=ctx.bot_id."""
        content = open("app/services/agentic.py").read()
        assert "bot_id=ctx.bot_id" in content, (
            "call sites must pass bot_id=ctx.bot_id to _newer_inbound_exists"
        )
