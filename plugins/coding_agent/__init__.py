"""ACP coding-agent client library — the shared plumbing behind ``delegate_to``.

Drives a CLI coding agent (protoCLI ``proto``, Claude Code, Codex, Gemini CLI) over
the Agent Client Protocol (JSON-RPC 2.0 over the child's stdio) via
``acp_client.AcpClient``.

This module is **no longer a plugin** — the ``code_with`` tool it used to contribute
was retired in favour of ``delegate_to`` with an ``acp`` delegate (ADR 0025), which
does the same job over one tool alongside a2a/openai delegates and a console panel.
What remains is the ACP client library that the ``delegates`` plugin and the ACP
runtime (ADR 0033) import:

- ``_client_for(spec)`` — get-or-create a cached ``AcpClient`` for a launch+policy
  signature (the cache key includes ``workdir``).
- ``evict_client(spec)`` — pop that cached client AND terminate its subprocess.
- ``_make_permission(spec)`` — the by-kind permission resolver (ADR 0024).

The ``spec`` dict is supplied by the caller; ``permissions`` is the by-kind policy
the client applies to the coding agent's ``session/request_permission`` requests:
``auto`` (allow all), ``allowlist`` (allow all but deny ``execute``/``delete``), or
``readonly`` (allow only read-like kinds) — overridable with ``allow_kinds`` /
``deny_kinds``.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable

from .acp_client import AcpClient

log = logging.getLogger("protoagent.plugins.coding_agent")

# One client (subprocess + session) per agent, keyed by its launch + policy
# signature so a config change spins up a fresh client. Module-global so the
# session persists across graph builds / turns.
_CLIENTS: dict[tuple, AcpClient] = {}

# ACP tool-call kinds treated as read-only (safe under ``readonly``).
_READONLY_KINDS = {"read", "search", "fetch", "think", "glob", "grep", "list"}
# Risky kinds denied by ``allowlist`` unless explicitly allowed.
_DEFAULT_DENY = {"execute", "delete"}


def _make_permission(spec: dict) -> Callable[[dict], str | None]:
    """Build the ACP permission resolver for an agent: given a request's params,
    return the optionId to select (or None to cancel/deny). Decides per the
    agent's ``permissions`` policy, using the request's ``toolCall.kind``."""
    policy = spec["permissions"]
    allow_set = set(spec["allow_kinds"])
    deny_set = set(spec["deny_kinds"])

    def _allowed(kind: str) -> bool:
        if policy == "readonly":
            return kind in (allow_set or _READONLY_KINDS)
        if policy == "allowlist":
            if kind in (deny_set or _DEFAULT_DENY):
                return False
            return kind in allow_set if allow_set else True
        return True  # auto

    def resolver(params: dict) -> str | None:
        options = params.get("options") or []
        kind = str(((params.get("toolCall") or {}).get("kind") or "")).lower()
        allow = _allowed(kind)
        prefix = "allow" if allow else "reject"
        for opt in options:
            if str(opt.get("kind", "")).startswith(prefix):
                return opt.get("optionId")
        # No option of the desired kind: allow ⇒ fall back to the first option;
        # deny ⇒ cancel (None).
        if allow:
            return options[0].get("optionId") if options else None
        log.info("[coding_agent/%s] denied %r action (policy=%s)", spec["name"], kind or "?", policy)
        return None

    return resolver


def _cache_key(spec: dict) -> tuple:
    return (
        spec["name"], spec["command"], tuple(spec["args"]), spec["workdir"],
        spec["permissions"], tuple(sorted(spec["allow_kinds"])), tuple(sorted(spec["deny_kinds"])),
    )


def _session_id_path(spec: dict) -> Path:
    """Where this agent's ACP session id is persisted, so a restart can
    ``session/load`` the same thread instead of starting fresh (#970). Keyed by a
    digest of the full launch+policy signature (the same tuple as the client cache),
    and ``scope_leaf``'d per instance like every other store so co-located hubs stay
    isolated. Imported lazily to keep this library host-free for its unit tests."""
    from infra.paths import data_home, scope_leaf

    digest = hashlib.sha256(repr(_cache_key(spec)).encode()).hexdigest()[:16]
    return scope_leaf(data_home() / "acp_sessions" / f"{digest}.json")


def _client_for(spec: dict) -> AcpClient:
    """Get-or-create the cached client for an agent spec."""
    key = _cache_key(spec)
    client = _CLIENTS.get(key)
    if client is None:
        client = AcpClient(
            spec["command"],
            spec["args"],
            cwd=spec["workdir"],
            env=spec["env"],
            name=spec["name"],
            permission=_make_permission(spec),
            session_id_path=_session_id_path(spec),
        )
        _CLIENTS[key] = client
    return client


async def evict_client(spec: dict) -> bool:
    """Drop the cached client for ``spec`` AND terminate its subprocess.

    The dispatch/relaunch paths ``_CLIENTS.pop(...)`` on an ``AcpError`` only
    *forget* the handle, leaving the child to be reaped by GC. A caller that
    dispatches into a short-lived, per-call ``workdir`` (e.g. a disposable git
    worktree) needs a *deterministic* reap — otherwise each scoped ``workdir``
    leaves its own ``AcpClient`` subprocess behind (the cache key includes
    ``workdir``). This pops the cached client and ``await``s ``client.close()`` so
    the process actually dies. Returns True if a live client was closed; idempotent.
    """
    client = _CLIENTS.pop(_cache_key(spec), None)
    if client is None:
        return False
    try:
        await client.close()
    except Exception:  # noqa: BLE001 — teardown is best-effort
        log.warning("[coding_agent/%s] close during evict failed", spec.get("name"), exc_info=True)
    return True
