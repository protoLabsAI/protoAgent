"""Developer flags (ADR 0068) — a small, local/static feature-flag system that gates
pre-release functionality behind tiers (``off`` < ``dev`` < ``beta`` < ``on``), measured
against a runtime *channel* (``prod`` ⊂ ``beta`` ⊂ ``dev``).

A flag is a **temporary** gate on a core code path, meant to be deleted when the feature
graduates — *not* a plugin (a permanent capability toggle) and *not* a setting (permanent
user config). See ``docs/adr/0068-developer-flags-and-panel.md``.

This module is slice 1 (backend core): the registry + resolution. ``flag_enabled(id)`` is
the check core code wraps a pre-release path in; ``resolved_flags()`` is the payload the
``/api/flags`` route (slice 2) serves and the Developer panel (slice 4) renders.

Resolution precedence (ADR 0068 D3, backend half):
    ``PROTOAGENT_FLAG_<ID>`` env override  >  the flag's tier vs. the runtime channel  >  off
(The query-param and panel-toggle override layers are frontend, slices 3–4.)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Tier = Literal["off", "dev", "beta", "on"]
Channel = Literal["prod", "beta", "dev"]

# Channel openness — higher sees more. A flag at tier T is enabled when the channel's rank
# meets the tier's requirement. ``off`` requires more than any channel can offer (never on);
# ``on`` requires nothing (always on).
_CHANNEL_RANK: dict[str, int] = {"prod": 0, "beta": 1, "dev": 2}
_TIER_REQUIRES: dict[str, int] = {"on": 0, "beta": 1, "dev": 2, "off": 99}


@dataclass(frozen=True)
class Flag:
    """One pre-release feature gate. Declared once in ``FLAGS`` — the single source of truth."""

    id: str  # dotted, stable — the override + lookup key, e.g. "chat.new_dashboard"
    description: str  # what it gates (shown in the Developer panel)
    tier: Tier = "off"  # rollout stage: off (nobody) · dev · beta · on (everybody)
    owner: str = ""  # who to ask — makes a stale flag actionable
    remove_by: str = ""  # a version or ISO date; a staleness test (slice 5) will guard it


# The registry — the SINGLE source of truth. Add a flag here; check it with ``flag_enabled``.
FLAGS: list[Flag] = [
    Flag(
        id="chat.compact",
        description="/compact — summarize + archive a chat thread, rewrite the checkpoint (#1527).",
        tier="dev",
        owner="kj",
        remove_by="2026-09-01",
    ),
    Flag(
        id="settings.devices",
        description=(
            "Settings ▸ Devices — QR device pairing + per-device tokens (ADR 0087). OFF by "
            "default: the 'Allow devices on my network' flow stopped the desktop app from "
            "starting four separate times (v0.104.1 killed loopback; then a token only the "
            "acting browser held; then a bind written with no token configured; then CORS "
            "preflight 401s once a token exists). Each fix was correct and each exposed the "
            "next layer. It stays behind this flag until the whole path is exercised in the "
            "desktop app itself, which is where every one of those failures actually landed."
        ),
        tier="off",
        owner="kj",
        remove_by="2026-10-01",
    ),
    Flag(
        id="secrets-panel",
        description=(
            "Settings ▸ Secrets — external secrets manager panel (ADR 0080). Dev channel "
            "only while the connect/test/sync flow stabilizes; graduate to `on` once it's "
            "exercised end to end (#2120)."
        ),
        tier="dev",
        owner="kj",
        remove_by="2026-10-01",
    ),
]


def _registry() -> dict[str, Flag]:
    """Flags keyed by id (built fresh so tests can monkeypatch ``FLAGS``)."""
    return {f.id: f for f in FLAGS}


def _env_key(flag_id: str) -> str:
    """``PROTOAGENT_FLAG_<ID>`` — the id upper-cased with every non-alphanumeric run → ``_``
    (``chat.new_dashboard`` → ``PROTOAGENT_FLAG_CHAT_NEW_DASHBOARD``)."""
    return "PROTOAGENT_FLAG_" + "".join(c if c.isalnum() else "_" for c in flag_id).upper()


def _env_override(flag_id: str) -> bool | None:
    """The forced state from ``PROTOAGENT_FLAG_<ID>`` (headless / CI / deployment escape
    hatch), or ``None`` when the var is unset."""
    raw = os.environ.get(_env_key(flag_id))
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "on", "yes")


def current_channel() -> Channel:
    """The runtime's openness. Explicit ``PROTOAGENT_CHANNEL`` wins; else the dev sandbox
    instance (``PROTOAGENT_INSTANCE=dev``, ADR 0065) is ``dev``; else the ``developer.channel``
    config field; else ``prod``.

    Read live (like plugin config) so a Settings save applies without a restart, and degrades
    to env/default when there's no graph state (ACP / headless / import-time)."""
    explicit = os.environ.get("PROTOAGENT_CHANNEL", "").strip().lower()
    if explicit in _CHANNEL_RANK:
        return explicit  # type: ignore[return-value]
    try:
        from infra.paths import instance_id

        if instance_id() == "dev":
            return "dev"
    except Exception:
        pass
    try:
        from graph.sdk import config

        configured = str(getattr(config(), "developer_channel", "") or "").strip().lower()
        if configured in _CHANNEL_RANK:
            return configured  # type: ignore[return-value]
    except Exception:
        pass
    return "prod"


def _tier_enabled(tier: str, channel: str) -> bool:
    return _CHANNEL_RANK.get(channel, 0) >= _TIER_REQUIRES.get(tier, 99)


def flag_enabled(flag_id: str, *, channel: Channel | None = None) -> bool:
    """Is ``flag_id`` enabled in the current process? Env override > tier-vs-channel > off.
    An unregistered id is always off (fail-closed). Pass ``channel`` to resolve against a
    specific channel instead of the live one (used by ``resolved_flags`` and tests)."""
    override = _env_override(flag_id)
    if override is not None:
        return override
    flag = _registry().get(flag_id)
    if flag is None:
        return False
    return _tier_enabled(flag.tier, channel or current_channel())


def resolved_flags(*, channel: Channel | None = None) -> dict:
    """Every registered flag with its metadata + resolved state, plus the active channel —
    the payload for ``GET /api/flags`` (slice 2) and the Developer panel (slice 4)."""
    resolved_channel = channel or current_channel()
    flags = []
    for f in FLAGS:
        override = _env_override(f.id)
        flags.append(
            {
                "id": f.id,
                "description": f.description,
                "tier": f.tier,
                "owner": f.owner,
                "remove_by": f.remove_by,
                "enabled": override if override is not None else _tier_enabled(f.tier, resolved_channel),
                "source": "env" if override is not None else "channel",
            }
        )
    return {"channel": resolved_channel, "flags": flags}
