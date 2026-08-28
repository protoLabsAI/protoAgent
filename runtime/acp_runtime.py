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
import re
import sys
from pathlib import Path

from runtime.acp_agents import ACP_AGENT_CATALOG, DEPRECATED_ACP_AGENTS
from runtime.context import ContextAssembler

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]  # repo root (where the `server` pkg lives)

# Best-effort launch commands per agent, derived from the canonical catalog
# (runtime/acp_agents.py) — ACP servers drift, so these are *defaults* the operator can
# override in config (``acp.agents.<name>: {command, args}``).
_ACP_ADAPTERS: dict[str, dict] = {
    # Deprecated agents are included: they are no longer OFFERED, but an install already
    # on one must still resolve a launch command or it stops booting (#2633).
    a["id"]: {"command": a["command"], "args": list(a["args"])}
    for a in (*ACP_AGENT_CATALOG, *DEPRECATED_ACP_AGENTS)
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


def operator_mcp_server_spec(config) -> dict:
    """The ``mcpServers`` entry mounting slice 1's operator MCP server.

    Under an ACP runtime the coding agent IS the brain, so it gets protoAgent's FULL
    toolset by default — parity with the native runtime, where the gateway model has
    every tool. There is no "enable tools for ACP" step: ``operator_mcp.tools`` is an
    optional *restriction* (a named allowlist), not a requirement. Empty/unset ⇒ ``"*"``
    (everything, minus the redundant code-exec tool the coding agent already has — see
    ``runtime.operator_mcp_tools._STAR_EXCLUDE``)."""
    configured = list(getattr(config, "operator_mcp_tools", None) or [])
    allow = configured or ["*"]  # empty ⇒ full toolset, parity with the native runtime
    # ACP's stdio MCP-server schema wants env as an array of {name, value} (not a dict).
    # The agent spawns this command in its OWN cwd, so put the repo on PYTHONPATH — else
    # `-m server.operator_mcp` can't import (unless protoagent is pip-installed).
    repo_root = str(_REPO_ROOT)
    pythonpath = repo_root + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    env: list[dict] = [{"name": "PYTHONPATH", "value": pythonpath}]
    # Pin the child to THIS instance's resolved root. The ACP agent spawns the MCP
    # server with the spec env REPLACING the environment, so anything not listed here
    # is lost — forwarding only PROTOAGENT_INSTANCE let a PROTOAGENT_HOME-scoped
    # instance's child boot the DEFAULT stores and write another instance's data
    # (found live: a smoke agent's task_create landed in the operator's prod board).
    # The resolved instance_root wins over any env/auto-scope derivation in the child.
    try:
        from infra.paths import instance_paths

        env.append({"name": "PROTOAGENT_HOME", "value": str(instance_paths().instance_root)})
    except Exception:  # noqa: BLE001 — path resolution must never block the mount
        log.warning("[acp-runtime] could not resolve instance root for the operator MCP env", exc_info=True)
    box = os.environ.get("PROTOAGENT_BOX_ROOT")
    if box:
        env.append({"name": "PROTOAGENT_BOX_ROOT", "value": box})  # host-layer fidelity in the child
    inst = os.environ.get("PROTOAGENT_INSTANCE")
    if inst:
        env.append({"name": "PROTOAGENT_INSTANCE", "value": inst})  # share this instance's data
    # Pass the resolved allowlist to the child explicitly — the spawned server otherwise
    # reads operator_mcp.tools from YAML, which wouldn't carry the "*" default.
    env.append({"name": "OPERATOR_MCP_TOOLS", "value": ",".join(allow)})
    # Frozen desktop sidecar: sys.executable IS the server entrypoint and rejects
    # `-m server.operator_mcp` argv (#1603's class) — use the dispatch verb instead.
    if getattr(sys, "frozen", False):
        return {"name": "protoagent-operator", "command": sys.executable, "args": ["operator-mcp"], "env": env}
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
        "(the `protoagent-operator` server): tasks (your task/issue board — `task_create`, "
        "`task_list`, …), `memory_*`, `notes_*`, `set_goal`, `schedule_task`, subagents, and more.\n\n"
        "**IMPORTANT — for anything that must persist, use these protoAgent operator tools, NOT "
        "your own built-in todo/task/memory tools.** Creating a task or issue → `task_create` "
        "(your own TaskCreate/todo is ephemeral to this session and is invisible in protoAgent). "
        "Saving a note → `notes_*`; remembering a fact → `memory_ingest`; a standing goal → "
        "`set_goal`; future work → `schedule_task`. Use your own file/shell tools for code as usual.\n\n"
        "---\n\n" + soul
    )


# ── empty-reply detection (#2991) ──────────────────────────────────────────────
# ACP coding delegates (sonnet, claude-code, …) occasionally "reply" without doing any
# work: zero tool calls, no file edits, just a boilerplate preamble echoing intent ("Let
# me read the relevant files first"). The board plugin classifies these as their own
# failure class (#198/#222); this is the host-side backstop in the runtime's own delegate
# path — detect the empty reply and retry the same delegate once before returning it.

# Detection is deliberately two-tier so it never drops a genuine short answer that merely
# OPENS with a conversational lead-in (the #2991 false-positive: a bare-prefix match on
# "Sure,"/"OK,"/"First," classified "Sure, the fix is: change line 42." as boilerplate and
# silently retried it away). A reply is empty only when EVERY clause is either a stripped
# lead-in or a bare announcement of work-to-do — the moment any clause carries real content
# ("the fix is …", "the bug is …"), the reply is substantive and passes through untouched.

# Tier 1 — conversational lead-ins. On their own they mean nothing; they can equally
# precede a real answer, so they are STRIPPED and what remains decides. Never boilerplate
# by themselves.
_FILLER_LEADINS: tuple[str, ...] = (
    "sure", "ok", "okay", "alright", "all right", "of course", "certainly", "absolutely",
    "sure thing", "no problem", "gotcha", "understood", "got it", "sounds good", "will do",
    "you got it", "yes", "yep", "yeah", "yup", "great", "perfect", "right", "first",
    "now", "so", "well", "cool", "then", "also", "just", "please", "actually", "hey",
    "hi", "hello", "go ahead and",
)

# Tier 2 — an actual announcement of work that produced no output: a lead ("let me", "i'll",
# …) followed by an EXPLORATION verb ("read", "look", "check", …). Only verbs that deliver
# nothing even when performed live here — action/answer verbs ("fix", "change", "explain")
# are intentionally excluded, since a clause like "the fix is …" carries the answer itself.
_INTENT_LEADS: tuple[str, ...] = (
    "let me", "let's", "let us", "i'll", "i will", "i'm going to", "i am going to",
    "i'm gonna", "i am gonna", "i'm about to", "i am about to", "going to", "gonna",
)
_WORK_VERBS: tuple[str, ...] = (
    "read", "look", "take a look", "have a look", "check", "examine", "inspect", "review",
    "start", "begin", "investigate", "dig", "explore", "open", "search", "gather",
    "go through", "pull up", "analyze", "scan", "trace", "study", "get started",
    "get back to", "dive", "work on", "familiarize", "spin up",
)
# Announce phrases with no lead+verb shape ("looking into it", "one moment", "on it").
_STANDALONE_INTENT: tuple[str, ...] = (
    "looking into", "taking a look", "having a look", "one moment", "one sec",
    "one second", "give me a moment", "give me a sec", "give me a second", "on it",
    "working on it", "getting started", "hang on", "hold on", "here goes", "here we go",
)

# With zero tool calls, a reply at or under this many characters is a candidate for
# "boilerplate only"; more text than this is substantive by length alone. Sized to hold a
# couple of sentences of preamble but well under a real one-paragraph answer.
_EMPTY_REPLY_TEXT_CEILING = 400

# Chars that end one clause and begin another. Splitting on these means a real thought
# tacked onto a preamble ("let me look — the answer is 42") is seen as its own clause and
# keeps the reply substantive.
_CLAUSE_SPLIT = re.compile(r"[,.!?;:—–\n]+")
# A phrase only matches at a word boundary, so "so" never eats into "something".
_WORD_BOUNDARY = " ,.!?:;—–-…\t'"


def _startswith_phrase(text: str, phrase: str) -> bool:
    """True if *text* begins with *phrase* as a whole word/phrase (not mid-word)."""
    if text == phrase:
        return True
    return text.startswith(phrase) and text[len(phrase)] in _WORD_BOUNDARY


def _strip_leadins(text: str) -> str:
    """Drop markdown bullets/quotes and any leading conversational fillers, iteratively —
    "- Sure, first, let me …" → "let me …". Returns lowercased remainder ("" if the text
    was nothing but fillers). A filler only counts at a word boundary, never mid-word."""
    text = text.lstrip("#*->`•·–—+ \t").strip().lower()
    changed = True
    while changed and text:
        changed = False
        for f in _FILLER_LEADINS:
            if text == f:
                return ""
            if text.startswith(f) and text[len(f)] in _WORD_BOUNDARY:
                text = text[len(f):].lstrip(_WORD_BOUNDARY)
                changed = True
                break
    return text


def _is_intent_clause(clause: str) -> bool:
    """True if *clause* (already lead-in-stripped) is a bare announcement of work with no
    delivered content — a standalone announce phrase, or a lead + exploration verb."""
    if not clause:
        return True
    if any(_startswith_phrase(clause, p) for p in _STANDALONE_INTENT):
        return True
    for lead in _INTENT_LEADS:
        if _startswith_phrase(clause, lead):
            rest = _strip_leadins(clause[len(lead):])
            if any(_startswith_phrase(rest, v) for v in _WORK_VERBS):
                return True
    return False


def is_empty_delegate_reply(text: str, tool_calls: int) -> bool:
    """True when an ACP delegate returned nothing meaningful (#2991).

    "Meaningful" = at least one tool call (a file edit is a tool call), OR substantive
    text beyond a boilerplate preamble. A reply with zero tool calls and either no text or
    only a short announcement of work never done ("Let me read the relevant files first")
    is empty and worth a retry. A normal reply — any tool activity, or real prose — is
    never flagged, so the common path is untouched.

    Detection favours the safe direction: a genuine short answer that opens with a
    conversational lead-in ("Sure, the fix is: change line 42.") is NOT empty, because the
    lead-in is stripped and the surviving clause carries content. Missing a truly-empty
    reply merely returns it as-is (the board plugin's own classification is the backstop);
    dropping a real one would lose the answer, so the heuristic errs toward "substantive".
    """
    if tool_calls > 0:
        return False
    stripped = (text or "").strip()
    if not stripped:
        return True
    if len(stripped) > _EMPTY_REPLY_TEXT_CEILING:
        return False
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    return all(_is_boilerplate_line(ln) for ln in lines)


def _is_boilerplate_line(line: str) -> bool:
    remainder = _strip_leadins(line)
    if not remainder:
        return True  # the line was nothing but conversational filler ("Sure.", "OK!")
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(remainder) if c.strip()]
    if not clauses:
        return True
    return all(_is_intent_clause(_strip_leadins(c)) for c in clauses)


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

        # Mirror graph/agent.py: `middleware.knowledge: false` means no memory injection on
        # ANY runtime — the store is withheld from the composer (the skill index still injects).
        knowledge_on = bool(getattr(self.config, "knowledge_middleware", True))
        # This runtime holds no thread/session id for a turn, so the assembler has none and
        # never records injection rows (ADR 0069 D6 rows must be attributable to a turn).
        return ContextAssembler(
            config=self.config,
            knowledge_store=getattr(STATE, "knowledge_store", None) if knowledge_on else None,
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
            # This client drives the agent's OWN chat turn, not a coder dispatch. On the
            # streaming/A2A path that turn is already booked under the same `acp:<agent>`
            # label (`server.chat._acp_drive_turn` yields the usage frame the executor's
            # terminal hook records), so a row from here would double it in the very
            # rollup #3015 exists to make trustworthy. On the non-streaming driver the
            # turn is booked nowhere — a gap #3015 neither opens nor closes, because a
            # chat turn recorded HERE would file under a `coder:` key and count as coder
            # work. See `AcpClient.record_runs` for why that is left to #3000.
            record_runs=False,
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
        deltas (token-ish streaming).

        Empty-reply guard (#2991): if the delegate returns with no tool calls and only a
        boilerplate preamble, the same delegate is retried ONCE. The first attempt's frames
        are buffered and dropped, so the caller sees only the retry — never the empty first
        attempt. A second empty reply is returned as-is (no loop). A normal reply makes
        exactly one attempt and streams unchanged.
        """
        client = self._ensure_client()
        ctx = self._context.assemble(query=message)
        prompt = "\n\n".join(p for p in (ctx.volatile_delta, message) if p)

        answer, tool_calls = await self._prompt_attempt(
            client,
            prompt,
            progress_callback=progress_callback,
            tool_callback=tool_callback,
            text_callback=text_callback,
        )
        if is_empty_delegate_reply(answer, tool_calls):
            log.warning(
                "[acp-runtime] empty reply from delegate %s (output_len=%d, tool_calls=%d) — retrying once",
                self.agent,
                len(answer or ""),
                tool_calls,
            )
            answer, tool_calls = await self._prompt_attempt(
                client,
                prompt,
                progress_callback=progress_callback,
                tool_callback=tool_callback,
                text_callback=text_callback,
            )
            if is_empty_delegate_reply(answer, tool_calls):
                # One retry only — a second empty reply is returned normally (no loop).
                log.warning(
                    "[acp-runtime] retry of delegate %s also returned empty "
                    "(output_len=%d, tool_calls=%d) — returning it",
                    self.agent,
                    len(answer or ""),
                    tool_calls,
                )
        self._context.after_turn(user=message, response=answer)
        return answer

    async def _prompt_attempt(
        self, client, prompt: str, *, progress_callback, tool_callback, text_callback
    ) -> tuple[str, int]:
        """One ACP prompt attempt → ``(answer, tool_call_count)``.

        The caller's tool/text callbacks are held behind a buffer until the attempt proves
        substantive — the first tool call, or text past the boilerplate ceiling — then the
        buffer is flushed and every later frame passes through live. So a real reply streams
        essentially unchanged (only a short leading preamble is briefly buffered, released
        the instant real work appears), while an attempt that stays empty is dropped whole:
        its buffered boilerplate frames are never forwarded, letting a retry replace it with
        the caller none the wiser (#2991). ``progress_callback`` is passed straight through —
        progress narration is ephemeral and safe to leak from a discarded attempt.
        """
        tool_calls = 0
        text_len = 0
        flushed = False
        buffer: list[tuple[str, object]] = []

        async def _flush() -> None:
            nonlocal flushed
            if flushed:
                return
            flushed = True
            for kind, payload in buffer:
                if kind == "text":
                    if text_callback is not None:
                        await text_callback(payload)
                elif tool_callback is not None:
                    await tool_callback(payload)
            buffer.clear()

        async def _on_tool(ev) -> None:
            nonlocal tool_calls
            if isinstance(ev, dict) and ev.get("phase") == "start":
                tool_calls += 1
            if flushed:
                if tool_callback is not None:
                    await tool_callback(ev)
                return
            buffer.append(("tool", ev))
            await _flush()  # any tool activity proves the attempt did real work

        async def _on_text(delta) -> None:
            nonlocal text_len
            text_len += len(delta or "")
            if flushed:
                if text_callback is not None:
                    await text_callback(delta)
                return
            buffer.append(("text", delta))
            if text_len > _EMPTY_REPLY_TEXT_CEILING:
                await _flush()  # too much text to be a boilerplate-only preamble

        answer = await client.prompt(
            prompt,
            progress_callback=progress_callback,
            tool_callback=_on_tool,
            text_callback=_on_text,
        )
        # Attempt over. If it never flushed but IS a real (if short) reply, deliver its
        # buffered frames now; if it's empty, leave them unsent so a retry can replace it.
        if not flushed and not is_empty_delegate_reply(answer, tool_calls):
            await _flush()
        return answer, tool_calls

    def last_usage(self) -> dict | None:
        """Latest ACP-native context pressure ({used, size} tokens) the agent reported
        via ``usage_update`` — None when the agent hasn't sent one (most coding agents
        don't; hermes-acp does after each response).

        A live-state accessor for a caller that wants to sample it (the coding-agent
        client exposes the same attribute for the project board's monitor). Nothing on
        the chat path reads it: the turn's usage frame deliberately carries no
        context_* fields, because no consumer was ever built for them (#3006).
        """
        return getattr(self._client, "last_usage", None)

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

        # Not a coder run: this is the auxiliary model (compaction, goal verification,
        # fact extraction) for an ACP-only setup with no gateway. Recording it under a
        # `coder:` key would put an internal housekeeping call in the coder-run count
        # (#3015). It is unmetered either way — a separate row shape, if anyone wants it.
        client = AcpClient(spec["command"], spec.get("args"), cwd=os.getcwd(), name=f"{agent}-aux", record_runs=False)
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
