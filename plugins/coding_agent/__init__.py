"""CLI coding-agent plugin — spawn a coding agent over ACP (ADR 0024).

Contributes one tool, ``code_with(agent, task)``, that hands a coding job to a
configured CLI coding agent (protoCLI ``proto``, Claude Code, Codex, Gemini CLI)
and returns its result. The agent is driven over the Agent Client Protocol
(JSON-RPC 2.0 over the child's stdio) by ``acp_client.AcpClient``.

The plugin ships disabled with an empty agent list — each configured agent gets
file + shell access in its workdir, so it's a deliberate opt-in. Enable with
``plugins: { enabled: [coding_agent] }`` and add agents under the ``coding_agent``
config section. See docs/guides/coding-agents.md.

Per-agent safety controls (ADR 0024):
- ``permissions`` — by-kind permission policy the client applies to the coding
  agent's ``session/request_permission`` requests: ``auto`` (allow all, default),
  ``allowlist`` (allow all but deny ``execute``/``delete``), or ``readonly``
  (allow only read-like kinds). Overridable with ``allow_kinds`` / ``deny_kinds``.
- ``confirm`` — when true, ``code_with`` asks the operator (``ask_human``) to
  approve *before* each call to that agent (a per-call consent gate). Per-action
  live HITL is deferred — it would need to pause a blocking subprocess session,
  which LangGraph's resume model can't do mid-tool.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from langchain_core.tools import tool

from .acp_client import AcpClient, AcpError

log = logging.getLogger("protoagent.plugins.coding_agent")

# One client (subprocess + session) per agent, keyed by its launch + policy
# signature so a config change spins up a fresh client. Module-global so the
# session persists across graph builds / turns; a per-agent lock serializes turns
# (a session is a single conversation — ``task_batch`` must not interleave two
# prompts on one).
_CLIENTS: dict[tuple, AcpClient] = {}
_LOCKS: dict[str, asyncio.Lock] = {}

_VALID_POLICIES = {"auto", "allowlist", "readonly"}
# ACP tool-call kinds treated as read-only (safe under ``readonly``).
_READONLY_KINDS = {"read", "search", "fetch", "think", "glob", "grep", "list"}
# Risky kinds denied by ``allowlist`` unless explicitly allowed.
_DEFAULT_DENY = {"execute", "delete"}
# Resume values that count as approval for the ``confirm`` gate.
_APPROVALS = {"y", "yes", "approve", "approved", "ok", "okay", "allow", "proceed", "go"}


def _normalize_agents(raw) -> dict[str, dict]:
    """Validate the configured ``agents`` list → {name: spec}. Drops bad entries
    (logged) rather than raising, so one typo can't break the plugin."""
    agents: dict[str, dict] = {}
    for entry in raw or []:
        if not isinstance(entry, dict):
            log.warning("[coding_agent] ignoring non-mapping agent entry: %r", entry)
            continue
        name = str(entry.get("name", "")).strip()
        command = str(entry.get("command", "")).strip()
        workdir = str(entry.get("workdir", "")).strip()
        if not (name and command and workdir):
            log.warning("[coding_agent] agent entry needs name+command+workdir: %r", entry)
            continue
        if name in agents:
            log.warning("[coding_agent] duplicate agent name %r — keeping first", name)
            continue
        args = entry.get("args") or []
        if not isinstance(args, (list, tuple)):
            log.warning("[coding_agent] %s: args must be a list — ignoring", name)
            args = []
        env = entry.get("env") if isinstance(entry.get("env"), dict) else None
        policy = str(entry.get("permissions", "auto")).strip().lower() or "auto"
        if policy not in _VALID_POLICIES:
            log.warning("[coding_agent] %s: unknown permissions %r — using 'auto'", name, policy)
            policy = "auto"
        agents[name] = {
            "name": name,
            "command": command,
            "args": [str(a) for a in args],
            "workdir": workdir,
            "env": {str(k): str(v) for k, v in env.items()} if env else None,
            "timeout_s": entry.get("timeout_s"),
            "permissions": policy,
            "allow_kinds": [str(k).lower() for k in (entry.get("allow_kinds") or [])],
            "deny_kinds": [str(k).lower() for k in (entry.get("deny_kinds") or [])],
            "confirm": bool(entry.get("confirm", False)),
        }
    return agents


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


def _approved(decision) -> bool:
    return str(decision).strip().lower() in _APPROVALS


def _build_code_with(agents: dict[str, dict], default_timeout_s: float):
    """Build the ``code_with`` tool, closing over the configured agents.

    Not wrapped in ``with_fallback``: the ``confirm`` gate calls ``interrupt()``,
    whose control-flow exception that wrapper would swallow. Expected failures
    return error strings; the I/O is guarded locally as the equivalent net.
    """
    listing = ", ".join(
        f"`{name}` (in `{spec['workdir']}`)" for name, spec in agents.items()
    )

    @tool
    async def code_with(agent: str, task: str) -> str:
        """Delegate a coding task to a CLI coding agent and return its result.

        Deprecated: prefer `delegate_to(target, query)` with an `acp` delegate
        (the unified delegate registry, ADR 0025) — it does the same over one tool
        alongside a2a/openai delegates, with a console panel. This tool stays for
        back-compat and will be removed in a future release.

        Use this to hand a real, repo-scoped coding job — read/edit/run code,
        fix a failing test, add an endpoint — to a purpose-built coding agent
        that has its own file access, shell, and edit/verify loop. Prefer this
        over doing multi-file code edits inline.

        Args:
            agent: which configured coding agent to use (see the available list
                in this tool's description).
            task: the full, self-contained instruction (the coding agent does
                not see this conversation — restate the goal, the relevant files
                if known, and the definition of done, e.g. "run the tests").

        Each agent works in its own pre-configured directory; you cannot point it
        elsewhere. The call blocks until the agent finishes the turn (coding is
        slow) and returns its final message. Follow-up calls to the same agent
        continue the same session, so you can iterate ("now also …").
        """
        spec = agents.get(agent)
        if spec is None:
            return (
                f"Error: unknown coding agent {agent!r}. "
                f"Configured agents: {', '.join(agents) or '(none)'}."
            )
        if not str(task).strip():
            return "Error: `task` is empty — give the coding agent a concrete instruction."

        # Per-call consent gate (before any side effect, so re-execution on resume
        # is idempotent). interrupt() parks the turn as input-required.
        if spec["confirm"]:
            from langgraph.types import interrupt

            decision = interrupt({
                "question": (
                    f"Allow coding agent '{agent}' to work in {spec['workdir']}?\n\n"
                    f"Task: {task}\n\nReply 'yes' to proceed, anything else to decline."
                )
            })
            if not _approved(decision):
                return f"Declined: the operator did not approve running '{agent}' on this task."

        lock = _LOCKS.setdefault(agent, asyncio.Lock())
        timeout = float(spec.get("timeout_s") or default_timeout_s)
        client = _client_for(spec)

        async def _narrate(title: str) -> None:
            # Log narration. A later PR streams these onto A2A working frames.
            log.info("[coding_agent/%s] %s", agent, title)

        try:
            async with lock:
                answer = await client.prompt(task, progress_callback=_narrate, timeout=timeout)
        except AcpError as exc:
            _CLIENTS.pop(_cache_key(spec), None)  # drop so the next call relaunches
            return f"Error: {agent} (coding agent) failed: {exc}"
        except Exception as exc:  # noqa: BLE001 — local safety net (with_fallback is dropped)
            log.warning("[coding_agent/%s] unexpected failure: %s", agent, exc)
            return f"Error (partial result): {agent} could not complete: {type(exc).__name__}: {exc}"
        return answer or f"{agent} finished but returned no text."

    # The configured agent names belong in the LLM-facing description so the model
    # knows what it can pass as `agent` (the docstring can't interpolate them).
    code_with.description = f"{code_with.description}\n\nAvailable agents: {listing}."
    return code_with


def register(registry) -> None:
    """Entry point — called once at load with a PluginRegistry."""
    cfg = registry.config or {}
    agents = _normalize_agents(cfg.get("agents"))
    if not agents:
        log.warning(
            "[coding_agent] enabled but no agents configured — add entries under "
            "`coding_agent.agents` (see docs/guides/coding-agents.md). No tool registered."
        )
        return
    try:
        default_timeout_s = float(cfg.get("default_timeout_s") or 600)
    except (TypeError, ValueError):
        default_timeout_s = 600.0
    registry.register_tool(_build_code_with(agents, default_timeout_s))
    log.info("[coding_agent] registered code_with for %d agent(s): %s",
             len(agents), ", ".join(agents))
