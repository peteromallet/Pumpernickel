"""Bot-aware coalescer registry for recovery-v2 readiness gating.

Replaces the previous ``app.state.coalescers`` dict with a structured
registry that tracks three sets:

* ``expected`` — bots the startup install loop has declared it intends to
  install.  Populated incrementally by :meth:`register` from the
  channel-row-driven install loop in ``app/main.py``; never read from
  ``BOT_SPECS`` directly so staging-registered bots without channels do not
  permanently block :meth:`is_ready`.
* ``installed`` — bots that actually have a :class:`BurstCoalescer`
  instance attached.
* ``ready`` — bots whose transport surface has finished wiring (Discord
  gateway connected, non-Discord branch returned from
  ``_install_bot_coalescer``).

Recovery-v2 (see SD-A1-T3 and ``app/services/recovery.py``) only runs once
:meth:`is_ready` returns True.  A missing-bot :meth:`get` returns ``None``
rather than raising; callers MUST log a structured warning and leave the
offending row in ``failed`` so it surfaces operationally instead of
crashing the recovery loop.

Design decisions: SD-A1-T3, SD-A1-T6.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.debouncer import BurstCoalescer


class CoalescerRegistry:
    """Registry of :class:`BurstCoalescer` instances keyed by ``bot_id``."""

    def __init__(self) -> None:
        self.expected: set[str] = set()
        self.installed: dict[str, BurstCoalescer] = {}
        self.ready: set[str] = set()

    def register(self, bot_id: str, coalescer: BurstCoalescer) -> None:
        """Declare *bot_id* expected and attach its coalescer.

        Called by the channel-row-driven install loop in ``app/main.py``
        each time a bot's :class:`BurstCoalescer` is constructed.  After
        ``register`` the bot is in ``expected`` and ``installed`` but NOT
        yet in ``ready`` — readiness flips only after :meth:`mark_ready`
        is called from the transport-wiring site.
        """
        self.expected.add(bot_id)
        self.installed[bot_id] = coalescer

    def get(self, bot_id: str) -> BurstCoalescer | None:
        """Return the coalescer for *bot_id*, or ``None`` if not installed.

        Returning ``None`` (rather than raising) is intentional: recovery
        and inbound paths must continue running for other bots when a
        single bot's coalescer is missing.  Callers MUST log a structured
        warning and leave the affected row in ``failed`` for operator
        inspection.
        """
        return self.installed.get(bot_id)

    def mark_ready(self, bot_id: str) -> None:
        """Mark *bot_id*'s transport surface as fully wired.

        MUST be called at BOTH:

        * the non-Discord/mediator branch in ``app/main.py`` immediately
          after ``_install_bot_coalescer`` returns, AND
        * the Discord gateway loop in ``app/main.py`` once the gateway
          transport is connected.

        Omitting either call site silently keeps :meth:`is_ready` ``False``
        and disables recovery-v2 for affected deployments.
        """
        self.ready.add(bot_id)

    def is_ready(self) -> bool:
        """Return True iff ``installed.keys() == expected == ready``.

        Recovery-v2 (``_recover_v2_inbound``) gates on this flag in
        addition to the kill switch.
        """
        installed_keys = set(self.installed.keys())
        return installed_keys == self.expected == self.ready
