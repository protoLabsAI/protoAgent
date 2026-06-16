"""Discord return-address store (ADR 0015).

When the operator DMs the agent, the gateway records that **DM channel id** here.
Scheduler-fired and proactive turns have no originating caller — they need a
stored "where do I deliver?" address. With one captured, reactive Activity-thread
output (ADR 0003) is forwarded to the operator's Discord DM, so "remind me in 30
minutes" actually lands somewhere.

Single small JSON file, instance-scoped via ``paths.scope_leaf`` (ADR 0004),
mirroring the bespoke stores' ``/sandbox`` → ``~/.protoagent`` fallback. Override
the location with ``DISCORD_RETURN_ADDRESS_PATH`` (used by tests).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("protoagent.discord.return_address")

_LEAF = Path("discord") / "return-address.json"
_KEY = "discord_dm_channel_id"


def _path() -> Path:
    override = os.environ.get("DISCORD_RETURN_ADDRESS_PATH")
    if override:
        p = Path(override)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    from infra.paths import scope_leaf

    configured = scope_leaf(Path("/sandbox") / _LEAF)
    try:
        configured.parent.mkdir(parents=True, exist_ok=True)
        if os.access(configured.parent, os.W_OK):
            return configured
    except OSError:
        pass
    fallback = scope_leaf(Path.home() / ".protoagent" / _LEAF)
    fallback.parent.mkdir(parents=True, exist_ok=True)
    return fallback


def get() -> str | None:
    """The captured Discord DM channel id, or ``None`` if none recorded yet."""
    try:
        p = _path()
        if not p.exists():
            return None
        return json.loads(p.read_text()).get(_KEY) or None
    except Exception:  # noqa: BLE001 — a missing/corrupt file just means "no address"
        return None


def record(channel_id: str) -> None:
    """Idempotently store ``channel_id`` as the operator's Discord return address.
    Best-effort — never raises (a failure just means proactive delivery is off)."""
    if not channel_id:
        return
    try:
        if get() == channel_id:
            return
        _path().write_text(json.dumps({_KEY: channel_id}))
        log.info("[discord] recorded return address (DM channel %s)", channel_id)
    except Exception:
        log.exception("[discord] failed to record return address (non-fatal)")
