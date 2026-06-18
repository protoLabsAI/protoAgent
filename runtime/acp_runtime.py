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


# Most coding agents read AGENTS.md, but some have a canonical file of their own and won't
# reliably pick up AGENTS.md from a non-repo cwd. Write the vendor file too (relative path —
# Copilot's lives under .github/, which we mkdir). Value can be a subpath, not just a name.
_VENDOR_PERSONA_FILE = {
    "claude": "CLAUDE.md",
    "gemini": "GEMINI.md",
    "copilot": ".github/copilot-instructions.md",
}


def _strip_injection(text: str) -> str:
    """Light guard (both comps scan SOUL): drop lines that try to redefine the chat role."""
    bad = ("system:", "developer:", "assistant:", "<|", "###system")
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().lower().startswith(bad))


def persona_doc(config) -> str:
    """The persona an ACP coding agent should adopt as its own — SOUL.md + a short operating
    note. Written to AGENTS.md in the session cwd so the agent loads it into ITS system prompt
    (the slot that beats its built-in identity). A focused doc, NOT protoAgent's full native
    system prompt (which carries loop-specific bits like the <output> response format)."""
    try:
        from graph.config_io import read_soul

        soul = _strip_injection((read_soul() or "").strip())
    except Exception:  # noqa: BLE001
        soul = ""
    if not soul:
        return ""
    return (
        "# Your identity & operating rules\n\n"
        "Adopt the persona and rules below as your own — they override your default identity.\n\n"
        "You run inside **protoAgent**, which gives you a set of **operator tools over MCP** "
        "(the `protoagent-operator` server): beads (your task/issue board — `beads_create`, "
        "`beads_list`, …), `memory_*`, `notes_*`, `set_goal`, `schedule_task`, subagents, and more.\n\n"
        "**IMPORTANT — for anything that must persist, use these protoAgent operator tools, NOT "
        "your own built-in todo/task/memory tools.** Creating a task or issue → `beads_create` "
        "(your own TaskCreate/todo is ephemeral to this session and is invisible in protoAgent). "
        "Saving a note → `notes_*`; remembering a fact → `memory_ingest`; a standing goal → "
        "`set_goal`; future work → `schedule_task`. Use your own file/shell tools for code as usual.\n\n"
        "---\n\n" + soul
    )


class AcpRuntime:
    """Drives turns through an external coding agent over ACP.

    One instance per session/thread (the ACP session is stateful — the agent holds
    history). Persona is authoritative via files (ADR 0033 / due-diligence): SOUL.md is
    written as AGENTS.md (+ a vendor file) into the session cwd, which the coding agent
    loads into ITS system prompt — beating its built-in "I'm <agent>" identity. So each
    turn's prompt carries only the per-turn delta (retrieved knowledge/skills) + message.
    """

    def __init__(self, config, *, cwd: str | None = None, client_factory=None, context=None):
        self.config = config
        kind, agent = resolve_runtime(config)
        if kind != "acp":
            raise ValueError("AcpRuntime constructed for a non-ACP runtime")
        self.agent = agent
        # A dedicated, instance-scoped workspace — NOT the repo cwd (we write AGENTS.md
        # there and don't want to clobber the project's own).
        if cwd:
            self.cwd = cwd
        else:
            from infra.paths import workspace_dir

            self.cwd = str(workspace_dir(create=True))
        self._context = context or self._default_context()
        self._client_factory = client_factory or self._default_client_factory
        self._client = None

    def _default_context(self) -> ContextAssembler:
        from runtime.state import STATE

        return ContextAssembler(
            config=self.config,
            knowledge_store=getattr(STATE, "knowledge_store", None),
            skills_index=getattr(STATE, "skills_index", None),
        )

    def _write_persona_files(self) -> None:
        """Write the persona where the coding agent will read it as its own identity:
        AGENTS.md (universal) + a vendor file for this agent. Best-effort."""
        doc = persona_doc(self.config)
        if not doc.strip():
            return
        try:
            base = Path(self.cwd)
            base.mkdir(parents=True, exist_ok=True)
            for name in {"AGENTS.md", _VENDOR_PERSONA_FILE.get(self.agent, "AGENTS.md")}:
                target = base / name
                target.parent.mkdir(parents=True, exist_ok=True)  # vendor file may be in a subdir (.github/)
                target.write_text(doc, encoding="utf-8")
        except Exception:  # noqa: BLE001 — persona is best-effort, never fail the turn
            log.warning("[acp-runtime] could not write persona files to %s", self.cwd, exc_info=True)

    def _default_client_factory(self):
        spec = adapter_for(self.agent, self.config)
        mcp = operator_mcp_server_spec(self.config)
        from plugins.coding_agent.acp_client import AcpClient

        return AcpClient(
            spec["command"],
            spec.get("args"),
            cwd=self.cwd,
            name=self.agent,
            mcp_servers=[mcp] if mcp else [],
        )

    def _ensure_client(self):
        if self._client is None:
            self._write_persona_files()  # before the session starts → agent loads it
            self._client = self._client_factory()
        return self._client

    async def run_turn(self, message: str, *, progress_callback=None, tool_callback=None, text_callback=None) -> str:
        """Run one turn: per-turn context delta + message → ACP → write back. Persona is
        carried by the AGENTS.md file, not the prompt. ``tool_callback`` receives the agent's
        structured tool start/end events (UI cards); ``text_callback`` receives answer-text
        deltas (token-ish streaming)."""
        client = self._ensure_client()
        ctx = self._context.assemble(query=message)
        prompt = "\n\n".join(p for p in (ctx.volatile_delta, message) if p)
        answer = await client.prompt(
            prompt,
            progress_callback=progress_callback,
            tool_callback=tool_callback,
            text_callback=text_callback,
        )
        self._context.after_turn(user=message, response=answer)
        return answer

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()
            self._client = None


# ── ACP-backed aux model ───────────────────────────────────────────────────────
# So an ACP-only setup (no gateway) still has a model for protoAgent's *auxiliary* calls
# (compaction, goal-verification, fact extraction). Text-only — no tool-calling needed.

_AUX_CLIENTS: dict[str, object] = {}  # one reused aux session per agent


def _gateway_configured(config) -> bool:
    """True when a real OpenAI-compatible gateway key is available (config or env)."""
    key = (getattr(config, "api_key", "") or "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    return bool(key)


async def _aux_prompt(agent: str, config, text: str) -> str:
    client = _AUX_CLIENTS.get(agent)
    if client is None:
        spec = adapter_for(agent, config)
        from plugins.coding_agent.acp_client import AcpClient

        client = AcpClient(spec["command"], spec.get("args"), cwd=os.getcwd(), name=f"{agent}-aux")
        _AUX_CLIENTS[agent] = client
    return await client.prompt(text)


def _messages_to_text(messages) -> str:
    parts = []
    for m in messages:
        content = getattr(m, "content", m)
        parts.append(content if isinstance(content, str) else str(content))
    return "\n\n".join(p for p in parts if p)


def make_acp_aux_model(config, agent: str | None = None):
    """A `BaseChatModel` backed by an ACP agent — for aux LLM calls (compaction, goal-eval,
    fact extraction, …). `agent` names which coding agent (e.g. "claude"); blank falls back
    to the main runtime's agent, then "proto". Used both by the ACP-only fallback (no
    gateway) and by an explicit per-slot override like `aux_model: acp:claude`. Lazy +
    import-guarded so langchain stays optional at import time."""
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    agent = (agent or "").strip() or resolve_runtime(config)[1] or "proto"

    class AcpChatModel(BaseChatModel):
        """Text-only chat model over the ACP coding agent (no tools)."""

        @property
        def _llm_type(self) -> str:
            return f"acp:{agent}"

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> "ChatResult":
            text = await _aux_prompt(agent, config, _messages_to_text(messages))
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

        def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> "ChatResult":
            # Sync path — run the async prompt on a private loop in a worker thread so it's
            # safe whether or not the caller is already inside an event loop.
            import asyncio
            import concurrent.futures

            def _run():
                return asyncio.run(_aux_prompt(agent, config, _messages_to_text(messages)))

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                text = ex.submit(_run).result()
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    return AcpChatModel()
