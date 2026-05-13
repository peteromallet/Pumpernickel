import time
from typing import NamedTuple

import pytest

from app.services.inbound import process_inbound


pytestmark = pytest.mark.anyio


class _Charge(NamedTuple):
    charge: str


class _Coalescer:
    def __init__(self) -> None:
        self.calls = []

    async def add(self, user_id, message_id, user, *, source: str = "live", bot_id: str | None = None) -> None:
        self.calls.append((user_id, message_id, user, source))


def _payload(message_id: str = "wamid.source") -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "15555550100", "profile": {"name": "Maya"}}],
                            "messages": [
                                {
                                    "from": "15555550100",
                                    "id": message_id,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": "hello"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


async def test_process_inbound_defaults_coalescer_source_to_live(fake_pool, monkeypatch) -> None:
    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    coalescer = _Coalescer()

    await process_inbound(fake_pool, _payload(), coalescer, transport="whatsapp", bot_id="mediator")

    assert len(coalescer.calls) == 1
    assert coalescer.calls[0][3] == "live"


async def test_process_inbound_forwards_explicit_coalescer_source(fake_pool, monkeypatch) -> None:
    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    coalescer = _Coalescer()

    await process_inbound(
        fake_pool, _payload("wamid.catchup"), coalescer,
        transport="whatsapp", bot_id="mediator", coalescer_source="catch_up",
    )

    assert len(coalescer.calls) == 1
    assert coalescer.calls[0][3] == "catch_up"


async def test_process_inbound_requires_bot_id_and_transport(fake_pool) -> None:
    """Both transport and bot_id are required keyword-only — no silent defaults.

    Regression guard: the old code silently routed unknown senders to bot_id
    "mediator", which caused Tante Rosi inbound DMs to be answered by Véas.
    """
    with pytest.raises(TypeError):
        await process_inbound(fake_pool, _payload(), None)  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        await process_inbound(fake_pool, _payload(), None, transport="whatsapp")  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        await process_inbound(fake_pool, _payload(), None, bot_id="mediator")  # type: ignore[call-arg]


async def test_discord_inbound_writes_correct_bot_id(fake_pool, monkeypatch) -> None:
    """When the Discord gateway threads its own bot_id, the inbound row gets tagged
    with THAT bot_id — not the silent "mediator" default."""

    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    coalescer = _Coalescer()

    await process_inbound(
        fake_pool, _payload("wamid.rosi"), coalescer,
        transport="discord", bot_id="tante_rosi",
    )

    rows = [m for m in fake_pool.messages.values() if m["whatsapp_message_id"] == "wamid.rosi"]
    assert len(rows) == 1
    assert rows[0]["bot_id"] == "tante_rosi", (
        f"expected bot_id='tante_rosi', got {rows[0]['bot_id']!r} — "
        "regression of the silent-mediator-default bug"
    )
