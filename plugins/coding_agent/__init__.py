"""CLI coding-agent plugin — spawn a coding agent over ACP (ADR 0024).

Contributes one tool, ``code_with(agent, task)``, that hands a coding job to a
configured CLI coding agent (protoCLI ``proto``, Claude Code, Codex, Gemini CLI)
and returns its result. The agent is driven over the Agent Client Protocol
(JSON-RPC 2.0 over the child's stdio) by ``acp_client.AcpClient``.

The plugin ships disabled with an empty agent list — each configured agent gets
file + shell access in its workdir (auto-allowed, confined to that dir), so it's
a deliberate opt-in. Enable with ``plugins: { enabled: [coding_agent] }`` and add
agents under the ``coding_agent`` config section. See docs/guides/coding-agents.md.
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.tools import tool

from tools.fallbacks import with_fallback

from .acp_client import AcpClient, AcpError

log = logging.getLogger("protoagent.plugins.coding_agent")

# One client (subprocess + session) per agent, keyed by its launch signature so a
# config change spins up a fresh client. Module-global so the session persists
# across graph builds / turns; a per-agent lock serializes turns (a session is a
# single conversation — ``task_batch`` must not interleave two prompts on one).
_CLIENTS: dict[tuple, AcpClient] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


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
        agents[name] = {
            "name": name,
            "command": command,
            "args": [str(a) for a in args],
            "workdir": workdir,
            "env": {str(k): str(v) for k, v in env.items()} if env else None,
            "timeout_s": entry.get("timeout_s"),
        }
    return agents


def _client_for(spec: dict) -> AcpClient:
    """Get-or-create the cached client for an agent spec."""
    key = (spec["name"], spec["command"], tuple(spec["args"]), spec["workdir"])
    client = _CLIENTS.get(key)
    if client is None:
        client = AcpClient(
            spec["command"],
            spec["args"],
            cwd=spec["workdir"],
            env=spec["env"],
            name=spec["name"],
        )
        _CLIENTS[key] = client
    return client


def _build_code_with(agents: dict[str, dict], default_timeout_s: float):
    """Build the ``code_with`` tool, closing over the configured agents."""
    listing = ", ".join(
        f"`{name}` (in `{spec['workdir']}`)" for name, spec in agents.items()
    )

    @tool
    @with_fallback("The coding agent did not return a result.")
    async def code_with(agent: str, task: str) -> str:
        """Delegate a coding task to a CLI coding agent and return its result.

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

        lock = _LOCKS.setdefault(agent, asyncio.Lock())
        timeout = float(spec.get("timeout_s") or default_timeout_s)
        client = _client_for(spec)

        async def _narrate(title: str) -> None:
            # PR1: log narration. A later PR streams these onto A2A working frames.
            log.info("[coding_agent/%s] %s", agent, title)

        async with lock:
            try:
                answer = await client.prompt(
                    task, progress_callback=_narrate, timeout=timeout
                )
            except AcpError as exc:
                # Drop the cached client so the next call relaunches cleanly.
                _CLIENTS.pop((spec["name"], spec["command"], tuple(spec["args"]), spec["workdir"]), None)
                return f"Error: {agent} (coding agent) failed: {exc}"
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
