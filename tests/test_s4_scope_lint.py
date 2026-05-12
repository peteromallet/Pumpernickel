"""S4 T10 — scope lint.

Three checks:
1. Every TOOL_DISPATCH entry (except update_turn_plan) takes ctx as first
   positional arg AND references ctx somewhere in its body source.
2. Every production TurnContext( constructor in the five plumbed services
   passes read_scopes= and write_scopes= kwargs.
3. Sanity: a mediator-shaped ctx with read_scopes={'own'} +
   primary_topic_slug='relationship' resolves check_read_scope(ctx, 'own')
   to None.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

from app.bots.base import ReadScopes
from app.services.tools.registry import TOOL_DISPATCH
from app.services.tools.scope_guard import check_read_scope


_REPO = Path(__file__).resolve().parents[1]
_PLUMBED = [
    "app/services/agentic.py",
    "app/services/recovery.py",
    "app/services/scheduled_jobs.py",
    "app/services/checkins.py",
    "app/services/inbound.py",
]


def test_every_dispatch_callable_takes_ctx_and_uses_it() -> None:
    failures: list[str] = []
    for name, fn in TOOL_DISPATCH.items():
        if name == "update_turn_plan":
            continue
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if not params or params[0].name != "ctx":
            failures.append(f"{name}: first arg is {params[0].name if params else '<none>'}, expected 'ctx'")
            continue
        try:
            body = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if "ctx." not in body and "ctx," not in body and "ctx)" not in body:
            failures.append(f"{name}: body never references ctx")
    assert not failures, "TOOL_DISPATCH lint failures:\n" + "\n".join(failures)


def test_production_turncontext_constructors_pass_scope_kwargs() -> None:
    """Every TurnContext( constructor in plumbed services must pass read_scopes/write_scopes."""
    failures: list[str] = []
    for rel in _PLUMBED:
        text = (_REPO / rel).read_text()
        # Find every TurnContext( ... ) constructor by index scanning.
        idx = 0
        while True:
            i = text.find("TurnContext(", idx)
            if i == -1:
                break
            # Find the matching ')' — naive: span up to balanced close.
            depth = 0
            end = i
            for j in range(i, len(text)):
                ch = text[j]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            block = text[i:end + 1]
            if "read_scopes=" not in block or "write_scopes=" not in block:
                line = text[:i].count("\n") + 1
                failures.append(f"{rel}:{line} TurnContext( missing read_scopes= or write_scopes=")
            idx = end + 1
    assert not failures, "production-builder lint failures:\n" + "\n".join(failures)


def test_mediator_shaped_ctx_passes_check_read_scope_own() -> None:
    ctx = SimpleNamespace(
        read_scopes=ReadScopes(topics=frozenset({"own"})),
        primary_topic_slug="relationship",
    )
    assert check_read_scope(ctx, "own") is None
