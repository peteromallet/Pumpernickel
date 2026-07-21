"""Tool schema visibility tests — verify all seven Hector tools appear with
non-empty input_schema and description via to_anthropic_tools.

T14 (SC14):
- Call to_anthropic_tools(allowed=hector_spec.tool_allowlist).
- Assert all seven tools appear in the result.
- Assert each has a non-empty input_schema and description.
"""

from __future__ import annotations

import os

import pytest

from app.services.tools.registry import HECTOR_ONLY_TOOLS, to_anthropic_tools


@pytest.fixture(autouse=True)
def _env_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set STAGING=1 so _maybe_register_staging_bots registers staging bots."""
    monkeypatch.setenv("STAGING", "1")


def _get_hector_allowlist() -> set[str]:
    """Return hector's tool_allowlist after staging registration."""
    from app.bots.registry import _maybe_register_staging_bots, BOT_SPECS

    _maybe_register_staging_bots()
    hector = BOT_SPECS["hector"]
    return hector.tool_allowlist or frozenset()


class TestHectorToolSchemas:
    """Verify all seven Hector tools are visible via to_anthropic_tools."""

    def test_all_seven_tools_present(self):
        """to_anthropic_tools returns exactly the seven Hector tools
        within hector's allowlist (there are more tools overall)."""
        allowed = _get_hector_allowlist()
        tools = to_anthropic_tools(allowed)
        tool_names = {t["name"] for t in tools}
        for name in HECTOR_ONLY_TOOLS:
            assert name in tool_names, (
                f"Hector tool '{name}' missing from to_anthropic_tools output"
            )

    def test_each_tool_has_non_empty_input_schema(self):
        """Every Hector tool must have a non-empty input_schema."""
        allowed = _get_hector_allowlist()
        tools = to_anthropic_tools(allowed)
        hector_tools = [t for t in tools if t["name"] in HECTOR_ONLY_TOOLS]
        for t in hector_tools:
            schema = t.get("input_schema", {})
            assert schema, (
                f"Tool '{t['name']}' has empty input_schema"
            )
            assert schema.get("properties") or schema.get("type"), (
                f"Tool '{t['name']}' input_schema has no properties or type: {schema}"
            )

    def test_each_tool_has_non_empty_description(self):
        """Every Hector tool must have a non-empty description."""
        allowed = _get_hector_allowlist()
        tools = to_anthropic_tools(allowed)
        hector_tools = [t for t in tools if t["name"] in HECTOR_ONLY_TOOLS]
        for t in hector_tools:
            desc = t.get("description", "")
            assert desc, (
                f"Tool '{t['name']}' has empty description"
            )
            assert len(desc) > 10, (
                f"Tool '{t['name']}' description too short: {desc!r}"
            )

    def test_correct_tool_count(self):
        """Hector must have at least the 10 tools (7 commitment/event + 3 health read)."""
        allowed = _get_hector_allowlist()
        assert len(allowed) >= 10, (
            f"hector tool_allowlist has only {len(allowed)} tools, expected >= 10"
        )
        hector_only_in_allowlist = HECTOR_ONLY_TOOLS & allowed
        assert len(hector_only_in_allowlist) == 10, (
            f"Expected all 10 Hector tools in allowlist, got {len(hector_only_in_allowlist)}"
        )
