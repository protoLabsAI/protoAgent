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
    ``PROTOAGENT_FLAG_<ID>`` env override  >  a stored override  >  the flag's tier vs. the
    runtime channel  >  off

The stored override is what the Developer panel's toggle writes (``PUT /api/flags/<id>``):
one small JSON file per instance, so a flag that gates SERVER behaviour can be switched on
from the console and stays on across restarts. Before it existed the toggle only changed
the console, and a server-gated flag could be enabled by environment variable alone.
(The ``?flag:`` query-param layer stays frontend-only.)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

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
        id="secrets-panel",
        description=(
            "Settings ▸ Secrets — external secrets manager panel (ADR 0080). Dev channel "
            "only while the connect/test/sync flow stabilizes; graduate to `on` once it's "
            "exercised end to end (#2120)."
        ),
        tier="dev",
        owner="kj",
        remove_by="2027-02-01",
    ),
    Flag(
        id="home_conversation",
        description=(
            "Home conversation (ADR 0135 D9) — one pinned chat per instance where job "
            "results and owner notices are shown. Off while the slices land: with it off, "
            "`/api/home` answers 404 (#5545)."
        ),
        tier="off",
        owner="kj",
        remove_by="2027-03-01",
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


# ── stored overrides (the Developer panel's toggle) ──────────────────────────────────────
_OVERRIDES_FILE = "developer-flags.json"
_overrides_lock = threading.Lock()
# (path, mtime_ns) the cached map was read at; flag_enabled is called on request paths.
_overrides_cache: list = [None, None, {}]


class FlagOverrideError(ValueError):
    """The override cannot be changed; the message is safe to show the operator."""


def _overrides_path() -> Path:
    from infra.paths import instance_paths

    return instance_paths().instance_root / _OVERRIDES_FILE


def stored_overrides() -> dict[str, bool]:
    """This instance's stored overrides for registered flags. An unreadable or malformed
    file reads as no overrides: a bad file must never turn a pre-release path on."""
    try:
        path = _overrides_path()
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    with _overrides_lock:
        if _overrides_cache[0] == str(path) and _overrides_cache[1] == stamp:
            return dict(_overrides_cache[2])
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            log.warning("[flags] %s is unreadable; ignoring stored overrides", path)
            raw = {}
        known = _registry()
        clean = {k: v for k, v in raw.items() if k in known and isinstance(v, bool)} if isinstance(raw, dict) else {}
        _overrides_cache[:] = [str(path), stamp, clean]
        return dict(clean)


def _write_overrides(overrides: dict[str, bool]) -> None:
    path = _overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(overrides, sort_keys=True, indent=2) + "\n", "utf-8")
    os.replace(tmp, path)
    with _overrides_lock:
        _overrides_cache[:] = [None, None, {}]


def set_override(flag_id: str, enabled: bool | None) -> None:
    """Store (``True``/``False``) or clear (``None``) this instance's override for a
    registered flag. Refuses an unknown flag, and one pinned by its environment variable,
    where a stored value would be ignored and the panel would appear to do nothing."""
    if flag_id not in _registry():
        raise FlagOverrideError(f"no developer flag named {flag_id!r}")
    if enabled is not None and _env_override(flag_id) is not None:
        raise FlagOverrideError(
            f"{flag_id} is set by the {_env_key(flag_id)} environment variable; unset it to change the flag here"
        )
    overrides = stored_overrides()
    if enabled is None:
        if flag_id not in overrides:
            return
        overrides.pop(flag_id)
    else:
        overrides[flag_id] = bool(enabled)
    _write_overrides(overrides)


def clear_overrides() -> None:
    """Remove every stored override (the panel's "Reset all")."""
    if stored_overrides():
        _write_overrides({})


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
    """Is ``flag_id`` enabled in the current process? Env override > stored override >
    tier-vs-channel > off. An unregistered id is always off (fail-closed). Pass ``channel``
    to resolve against a specific channel instead of the live one (used by
    ``resolved_flags`` and tests)."""
    override = _env_override(flag_id)
    if override is not None:
        return override
    flag = _registry().get(flag_id)
    if flag is None:
        return False
    stored = stored_overrides().get(flag_id)
    if stored is not None:
        return stored
    return _tier_enabled(flag.tier, channel or current_channel())


def resolved_flags(*, channel: Channel | None = None) -> dict:
    """Every registered flag with its metadata + resolved state, plus the active channel —
    the payload for ``GET /api/flags`` (slice 2) and the Developer panel (slice 4)."""
    resolved_channel = channel or current_channel()
    stored = stored_overrides()
    flags = []
    for f in FLAGS:
        env = _env_override(f.id)
        default = _tier_enabled(f.tier, resolved_channel)
        if env is not None:
            enabled, source = env, "env"
        elif f.id in stored:
            enabled, source = stored[f.id], "override"
        else:
            enabled, source = default, "channel"
        flags.append(
            {
                "id": f.id,
                "description": f.description,
                "tier": f.tier,
                "owner": f.owner,
                "remove_by": f.remove_by,
                "enabled": enabled,
                # "env": pinned by PROTOAGENT_FLAG_<ID> (the panel cannot change it);
                # "override": stored for this instance by the Developer panel; else the channel.
                "source": source,
                "default": default,
            }
        )
    return {"channel": resolved_channel, "flags": flags}
