"""ACP agent runtime (ADR 0033, slice 3).

When ``agent_runtime: acp:<agent>``, an external coding agent (proto / codex / claude /
copilot / opencode) drives the turn over ACP instead of the native LangGraph loop. This
ties the two foundations together:

- **Tool plane** (slice 1): the operator MCP server is mounted into the ACP session via
  ``session/new`` ``mcpServers`` — the coding agent gets protoAgent's allowlisted tools.
- **Context plane** (slice 2): the prompt is built from the runtime context contract — a
  cacheable persona prefix sent ONCE at session start, then per-turn deltas (ADR 0033 D5:
  ACP sessions are stateful, so don't resend the world).

protoAgent stays the shell (A2A, scheduling, goals, console, memory). Opt-in: default is the
native runtime, so this is inert unless configured.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from runtime.context import ContextAssembler

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]  # repo root (where the `server` pkg lives)

# Best-effort launch commands per agent — ACP servers drift, so these are *defaults*
# the operator can override in config (``acp.agents.<name>: {command, args}``).
_ACP_ADAPTERS: dict[str, dict] = {
    "proto": {"command": "proto", "args": ["--acp"]},
    "codex": {"command": "npx", "args": ["-y", "@zed-industries/codex-acp"]},
    "claude": {"command": "npx", "args": ["-y", "@agentclientprotocol/claude-agent-acp"]},
    "gemini": {"command": "gemini", "args": ["--experimental-acp"]},
    "opencode": {"command": "opencode", "args": ["acp"]},
    "copilot": {"command": "copilot", "args": ["--acp"]},
}


def resolve_runtime(config) -> tuple[str, str]:
    """``("native", "")`` or ``("acp", "<agent>")`` from ``agent_runtime``."""
    raw = (getattr(config, "agent_runtime", "native") or "native").strip()
    if raw == "native" or not raw:
        return ("native", "")
    if raw.startswith("acp:"):
        return ("acp", raw.split(":", 1)[1].strip() or "")
    if raw == "acp":  # bare "acp" with no agent → invalid, treat as native + warn
        log.warning("[acp-runtime] agent_runtime 'acp' needs an agent, e.g. 'acp:codex' — using native")
        return ("native", "")
    log.warning("[acp-runtime] unknown agent_runtime %r — using native", raw)
    return ("native", "")


def is_acp_runtime(config) -> bool:
    return resolve_runtime(config)[0] == "acp"


def adapter_for(agent: str, config=None) -> dict:
    """Launch spec ({command, args}) for *agent* — config override beats the default."""
    overrides = (getattr(config, "acp_agents", None) or {}) if config else {}
    if agent in overrides and overrides[agent].get("command"):
        o = overrides[agent]
        return {"command": o["command"], "args": list(o.get("args", []) or [])}
    if agent in _ACP_ADAPTERS:
        d = _ACP_ADAPTERS[agent]
        return {"command": d["command"], "args": list(d["args"])}
    raise ValueError(f"no ACP adapter for {agent!r} — add acp.agents.{agent}.command in config")


def operator_mcp_server_spec(config) -> dict | None:
    """The ``mcpServers`` entry mounting slice 1's operator MCP server, or None when no
    tools are allowlisted (nothing to expose)."""
    if not (getattr(config, "operator_mcp_tools", None) or []):
        return None
    # ACP's stdio MCP-server schema wants env as an array of {name, value} (not a dict).
    # The agent spawns this command in its OWN cwd, so put the repo on PYTHONPATH — else
    # `-m server.operator_mcp` can't import (unless protoagent is pip-installed).
    repo_root = str(_REPO_ROOT)
    pythonpath = repo_root + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    env: list[dict] = [{"name": "PYTHONPATH", "value": pythonpath}]
    inst = os.environ.get("PROTOAGENT_INSTANCE")
    if inst:
        env.append({"name": "PROTOAGENT_INSTANCE", "value": inst})  # share this instance's data
    # Pass the runtime's allowlist to the child explicitly — the spawned server otherwise
    # reads operator_mcp.tools from YAML, which may not match this runtime's intent.
    tools = ",".join(getattr(config, "operator_mcp_tools", []) or [])
    env.append({"name": "OPERATOR_MCP_TOOLS", "value": tools})
    return {
        "name": "protoagent-operator",
        "command": sys.executable,
        "args": ["-m", "server.operator_mcp"],
        "env": env,
    }


class AcpRuntime:
    """Drives turns through an external coding agent over ACP.

    One instance per session/thread (the ACP session is stateful — the agent holds
    history, so we send the cacheable prefix once then per-turn deltas).
    """

    def __init__(self, config, *, cwd: str | None = None, client_factory=None, context=None):
        self.config = config
        kind, agent = resolve_runtime(config)
        if kind != "acp":
            raise ValueError("AcpRuntime constructed for a non-ACP runtime")
        self.agent = agent
        self.cwd = cwd or os.getcwd()
        self._context = context or self._default_context()
        self._client_factory = client_factory or self._default_client_factory
        self._client = None
        self._prefix_sent = False

    def _default_context(self) -> ContextAssembler:
        from runtime.state import STATE
        return ContextAssembler(
            config=self.config,
            knowledge_store=getattr(STATE, "knowledge_store", None),
            skills_index=getattr(STATE, "skills_index", None),
        )

    def _default_client_factory(self):
        spec = adapter_for(self.agent, self.config)
        mcp = operator_mcp_server_spec(self.config)
        from plugins.coding_agent.acp_client import AcpClient
        return AcpClient(
            spec["command"], spec.get("args"), cwd=self.cwd, name=self.agent,
            mcp_servers=[mcp] if mcp else [],
        )

    def _ensure_client(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    async def run_turn(self, message: str, *, progress_callback=None) -> str:
        """Run one turn: build the prompt (prefix once, then deltas) → ACP → write back."""
        client = self._ensure_client()
        ctx = self._context.assemble(query=message)
        if not self._prefix_sent:
            prompt = ctx.as_prompt(message)            # persona prefix + volatile + message
            self._prefix_sent = True                   # session is stateful — don't resend the prefix
        else:
            prompt = "\n\n".join(p for p in (ctx.volatile_delta, message) if p)
        answer = await client.prompt(prompt, progress_callback=progress_callback)
        self._context.after_turn(user=message, response=answer)
        return answer

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()
            self._client = None
