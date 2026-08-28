"""ACP coding-agent client library — the shared plumbing behind ``delegate_to``.

Drives a CLI coding agent (protoCLI ``proto``, Claude Code, Codex, Gemini CLI) over
the Agent Client Protocol (JSON-RPC 2.0 over the child's stdio) via
``acp_client.AcpClient``.

This module is **no longer a plugin** — the ``code_with`` tool it used to contribute
was retired in favour of ``delegate_to`` with an ``acp`` delegate (ADR 0025), which
does the same job over one tool alongside a2a/openai delegates and a console panel.
What remains is the ACP client library that the ``delegates`` plugin and the ACP
runtime (ADR 0033) import:

- ``dispatch_tapped(delegate, prompt, …)`` — the PUBLIC one-shot tapped dispatch seam
  (#3235): fresh session, by-kind permissions, live tool/thought/text callbacks,
  cancel-kills-child, teardown on every exit, returning a ``TappedResult``.
- ``_client_for(spec)`` — get-or-create a cached ``AcpClient`` for a launch+policy
  signature (the cache key includes ``workdir``).
- ``evict_client(spec)`` — pop one exact cached client AND terminate its subprocess.
- ``evict_clients(spec)`` — terminate every permission/conversation variant for one
  configured delegate while leaving unrelated delegates untouched.
- ``_make_permission(spec)`` — the by-kind permission resolver (ADR 0024).

The ``spec`` dict is supplied by the caller; ``permissions`` is the by-kind policy
the client applies to the coding agent's ``session/request_permission`` requests:
``auto`` (allow all), ``allowlist`` (allow all but deny ``execute``/``delete``), or
``readonly`` (allow only read-like kinds) — overridable with ``allow_kinds`` /
``deny_kinds``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from .acp_client import AcpClient, ProgressCallback, TappedResult, ToolCallback

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
            allowed = kind in (allow_set or _READONLY_KINDS)
        elif policy == "allowlist":
            if kind in (deny_set or _DEFAULT_DENY):
                return False
            allowed = kind in allow_set if allow_set else True
        else:
            allowed = True  # auto
        if spec.get("permissions_ceiling") == "readonly":
            allowed = allowed and kind in _READONLY_KINDS
        return allowed

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
    base = (
        spec["name"],
        spec["command"],
        tuple(spec["args"]),
        spec["workdir"],
        spec["permissions"],
        tuple(sorted(spec["allow_kinds"])),
        tuple(sorted(spec["deny_kinds"])),
        # Env shapes MUST key the pool: two specs differing only in env/env_remove
        # would otherwise share a client and the first caller's environment sticks
        # for everyone (QA panel on #2145; env itself had the same latent gap).
        tuple(sorted((spec.get("env") or {}).items())),
        tuple(sorted(spec.get("env_remove") or ())),  # sorted: order-insensitive identity
    )
    ceiling = str(spec.get("permissions_ceiling") or "")
    conversation = str(spec.get("conversation_key") or "")
    if not ceiling and not conversation:
        # Preserve the historical tuple exactly so upgrades keep finding every
        # existing persisted ACP session file (#970).
        return base
    # A per-invocation ceiling changes the mutable ACP permission resolver. Key it
    # before the conversation discriminator so concurrent fenced and unrestricted
    # turns can never share a client and overwrite each other.
    return (*base, ceiling, conversation)


def _session_id_path(spec: dict) -> Path:
    """Where this agent's ACP session id is persisted, so a restart can
    ``session/load`` the same thread instead of starting fresh (#970). Keyed by a
    digest of the full launch+policy signature (the same tuple as the client cache),
    a file under the per-instance ``instance_root/acp_sessions`` store so co-located
    hubs stay isolated. Imported lazily to keep this library host-free for its unit tests."""
    from infra.paths import instance_paths

    digest = hashlib.sha256(repr(_cache_key(spec)).encode()).hexdigest()[:16]
    return instance_paths().store("acp_sessions") / f"{digest}.json"


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
            # Subtractive env seam (#2117): strip host identity/credential vars from the
            # spawned coder WITHOUT mutating os.environ. `.get` so a spec that predates the
            # field (or an ad-hoc caller) is byte-identical — no removal, full inheritance.
            env_remove=spec.get("env_remove"),
            name=spec["name"],
            permission=_make_permission(spec),
            session_id_path=_session_id_path(spec),
        )
        _CLIENTS[key] = client
    return client


def _drop_client(spec: dict) -> AcpClient | None:
    """Synchronously pop the cached client for ``spec`` (no await) and return it, so
    a cancellation handler can ``kill_now()`` it and remove it from the pool without
    risking that an awaited teardown is itself cancelled. Returns None if none cached."""
    return _CLIENTS.pop(_cache_key(spec), None)


async def close_all() -> bool:
    """Reap EVERY cached ACP client + its subprocess tree — the shutdown hook so a
    server stop doesn't strand pooled ``delegate_to`` agents as init-reparented
    orphans (the leak that piled up to ~20 GB). Idempotent; returns True if any were
    closed."""
    clients = list(_CLIENTS.values())
    _CLIENTS.clear()
    closed = False
    for client in clients:
        try:
            await client.close()
            closed = True
        except Exception:  # noqa: BLE001 — shutdown reap is best-effort
            log.warning("[coding_agent] close during close_all failed", exc_info=True)
    return closed


async def evict_client(spec: dict) -> bool:
    """Drop the exact cached client for ``spec`` AND terminate its subprocess.

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


async def evict_clients(spec: dict) -> bool:
    """Terminate every cached conversation variant for one configured delegate.

    A delegate removal has the base configuration, not each per-call
    ``conversation_key``. Match the stable launch/policy prefix and leave every
    unrelated delegate untouched. Idempotent and best-effort.
    """
    base = _cache_key({**spec, "permissions_ceiling": "", "conversation_key": ""})
    clients = []
    for key in list(_CLIENTS):
        if key == base or (len(key) == len(base) + 2 and key[: len(base)] == base):
            client = _CLIENTS.pop(key, None)
            if client is not None:
                clients.append(client)
    for client in clients:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            log.warning("[coding_agent/%s] close during multi-evict failed", spec.get("name"), exc_info=True)
    return bool(clients)


async def forget_session(spec: dict) -> bool:
    """Forget the persisted ACP session for ``spec`` — evict the live client AND
    delete its saved session id — so the NEXT dispatch starts a fresh ``session/new``
    instead of ``session/load``-resuming the old thread.

    The persisted session (``#970``) lets a dispatch *reattach* a prior thread,
    which is right when the same ``workdir`` keeps its contents across calls. But a
    caller that **recreates the workdir fresh per attempt** (the project_board loop's
    disposable git worktree) wants the opposite: a resumed thread would carry memory
    of a diff the wiped tree no longer has, so the coder thinks it's already done
    (→ no diff) or edits against stale assumptions. Calling this first keeps the
    coder's memory in step with the (empty) tree. Returns True if anything was
    cleared; idempotent.
    """
    evicted = await evict_client(spec)
    removed = False
    try:
        _session_id_path(spec).unlink()
        removed = True
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("[coding_agent/%s] could not delete persisted session", spec.get("name"), exc_info=True)
    return evicted or removed


# ── the public tapped-dispatch seam (#3235) ───────────────────────────────────

# Fallback turn budget when neither the call nor the delegate names one — matches
# ``AcpClient.prompt``'s own default. A configured delegate normally carries its own
# ``timeout_s`` (the delegates plugin defaults it to 1800s).
_DEFAULT_TAPPED_TIMEOUT_S = 600.0


def _delegate_spec(delegate) -> dict:
    """Normalize ``delegate`` into the spec dict the client pool keys on.

    Accepts either a spec mapping (the ``_client_for`` shape) or a delegate-shaped
    object such as the delegates plugin's ``Delegate`` dataclass — duck-typed, so this
    library never imports that plugin (the dependency already points the other way).
    The object branch mirrors ``AcpAdapter._spec`` field-for-field: a tapped dispatch
    and a ``delegate_to`` dispatch of the same delegate must resolve the SAME cache
    key, so eviction/teardown from either side finds the client the other created.
    """
    if isinstance(delegate, Mapping):
        spec = dict(delegate)
    else:
        spec = {
            "name": str(getattr(delegate, "name", "") or ""),
            "command": str(getattr(delegate, "command", "") or ""),
            "args": [str(a) for a in (getattr(delegate, "args", None) or [])],
            "workdir": str(getattr(delegate, "workdir", "") or ""),
            "env": getattr(delegate, "env", None) or None,
            "env_remove": list(getattr(delegate, "env_remove", None) or []),
            "permissions": str(getattr(delegate, "permissions", "") or "auto"),
            "allow_kinds": list(getattr(delegate, "allow_kinds", None) or []),
            "deny_kinds": list(getattr(delegate, "deny_kinds", None) or []),
            "conversation_key": str(getattr(delegate, "conversation_key", "") or ""),
            "permissions_ceiling": str(getattr(delegate, "permissions_ceiling", "") or ""),
            "timeout_s": getattr(delegate, "timeout_s", None),
        }
    # A mapping caller may pass only the launch essentials; fill the keys the pool,
    # the permission resolver, and the cache key index into directly.
    spec.setdefault("name", "acp")
    spec.setdefault("args", [])
    spec.setdefault("env", None)
    spec.setdefault("env_remove", [])
    spec.setdefault("permissions", "auto")
    spec.setdefault("allow_kinds", [])
    spec.setdefault("deny_kinds", [])
    if not (spec.get("command") and spec.get("workdir")):
        from .acp_client import AcpError

        raise AcpError("dispatch_tapped needs a delegate with command + workdir")
    return spec


async def dispatch_tapped(
    delegate,
    prompt: str,
    *,
    on_tool: ToolCallback | None = None,
    on_thought: ProgressCallback | None = None,
    on_text: ProgressCallback | None = None,
    timeout: float | None = None,
) -> TappedResult:
    """Run ONE fully-tapped coder turn against ``delegate`` and return a `TappedResult`.

    The public seam for orchestrators that need more than ``delegate_to``'s prose reply
    — live callbacks while the coder works, and the wire signals (usage, plan, stop
    reason, dead end) when it stops. It exists so callers (the project board's build
    loop) stop reaching into this package's private client pool (#3235). One call owns
    the whole lifecycle:

    * **fresh-session forgetting** — any pooled client for this delegate signature is
      evicted and its persisted ACP session id deleted *before* dispatch, so the turn
      opens a fresh ``session/new`` instead of ``session/load``-resuming a thread whose
      workdir contents may no longer exist (the disposable-worktree caller).
    * **permission policy** — the delegate's by-kind resolver (ADR 0024) is rebuilt
      from the spec on every dispatch, honoring ``permissions`` / ``allow_kinds`` /
      ``deny_kinds`` / ``permissions_ceiling``.
    * **callback forwarding** — ``on_tool`` receives the structured tool start/end
      event dicts, ``on_thought`` the coder's reasoning deltas, ``on_text`` the
      answer-text deltas, exactly as ``AcpClient.prompt`` streams them. All optional
      and best-effort: a raising callback never breaks the turn.
    * **cancel kills the child** — ``asyncio.CancelledError`` pops the pooled handle
      and synchronously SIGKILLs the coder's whole process tree before re-raising, so
      stopping the caller stops the coder (no awaits on the cancellation path).
    * **teardown on every exit** — success or failure, the subprocess is evicted from
      the pool and reaped; a tapped dispatch never leaves a child behind.

    Args:
        delegate: the dispatch target — a spec mapping (``command`` + ``workdir``
            required; ``name``/``permissions``/``env``/… optional) or a delegate-shaped
            object such as the delegates plugin's ``Delegate`` dataclass.
        prompt: the user turn to send.
        on_tool: async callback for structured tool start/end event dicts.
        on_thought: async callback for the coder's reasoning-text deltas.
        on_text: async callback for answer-text deltas.
        timeout: seconds to await the turn; defaults to the delegate's ``timeout_s``
            (else 600).

    Raises `AcpError` on any transport/protocol failure — with the child already torn
    down either way.
    """
    spec = _delegate_spec(delegate)
    await forget_session(spec)
    client = _client_for(spec)
    client._permission = _make_permission(spec)
    if timeout is None:
        try:
            timeout = float(spec.get("timeout_s") or 0) or _DEFAULT_TAPPED_TIMEOUT_S
        except (TypeError, ValueError):
            timeout = _DEFAULT_TAPPED_TIMEOUT_S
    try:
        reply = await client.prompt(
            prompt,
            tool_callback=on_tool,
            thought_callback=on_thought,
            text_callback=on_text,
            timeout=timeout,
        )
        # Snapshot BEFORE teardown: the signals are per-client state and the client is
        # about to be evicted.
        result = client.tapped_result(reply)
    except asyncio.CancelledError:
        # Mid-cancellation: an awaited teardown would itself be cancelled before the
        # tree died. Forget the handle and SIGKILL the whole tree synchronously.
        _drop_client(spec)
        client.kill_now()
        raise
    except BaseException:
        await evict_client(spec)
        raise
    await evict_client(spec)
    return result
