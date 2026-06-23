"""System prompt composer for protoAgent.

Composes the system prompt from, in order:

1. Agent identity (``SOUL.md`` in the workspace, falls back to a
   template-generic placeholder).
2. Skill methodology (``skills/<slug>/SKILL.md`` — loaded per skill
   if the consumer passes a ``skill`` hint; the template ships no
   skill docs by default).
3. Subagent delegation rules (built from ``SUBAGENT_REGISTRY``).
4. Dynamic context injected by ``KnowledgeMiddleware`` when the agent
   ships a knowledge store.
5. Operator guidelines (the template ships neutral defaults — override
   in your fork to encode domain behavior like "verify, don't trust"
   or "always end with a PASS/WARN/FAIL verdict").

The model answers naturally; its reasoning streams natively (the gateway's
``reasoning_content``), so there is no ``<scratch_pad>``/``<output>`` text protocol.

When forking, the main thing to edit is the operator guidelines block
— that's where you encode how the agent behaves in its specific
domain.
"""

from pathlib import Path

from graph.subagents.config import SUBAGENT_REGISTRY


def _read_file(path: str | Path) -> str:
    """Read a file if it exists, return empty string otherwise."""
    p = Path(path)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def build_system_prompt(
    workspace: str = "/sandbox",
    include_subagents: bool = True,
    context: str = "",
    projects=None,
) -> str:
    """Build the complete system prompt for the lead agent.

    ``context`` is injected verbatim at the end of the prompt (before
    the response-format block) — ``KnowledgeMiddleware`` is the typical
    caller, passing in retrieved knowledge-store hits.

    ``projects`` (ADR 0007) — when the fenced filesystem toolset is enabled,
    the list of managed project workspaces ``[{name, path, write}]`` is named in
    the prompt so the agent knows the dirs it can operate on (and which are
    read-only). Inert when None.
    """
    parts = []

    # 1. Identity — prefer the runtime workspace (entrypoint.sh copies
    # config/SOUL.md to /sandbox/SOUL.md at container start). Fall back
    # to the repo source so local `python -m server` runs without a
    # /sandbox mount still pick up persona edits made via the drawer.
    soul = _read_file(f"{workspace}/SOUL.md")
    if not soul:
        soul = _read_file(Path(__file__).parent.parent / "config" / "SOUL.md")
    if soul:
        parts.append(soul)
    else:
        parts.append(
            "# Agent\n\n"
            "You are a protoAgent — an A2A-compliant LangGraph agent. "
            "Replace this placeholder by writing an SOUL.md in the workspace "
            "with your agent's identity, role, and personality."
        )

    # 2. Subagent instructions
    if include_subagents:
        parts.append(_build_subagent_section())

    # 2b. Managed project workspaces (ADR 0007 — fenced filesystem toolset).
    if projects:
        section = _build_projects_section(projects)
        if section:
            parts.append(section)

    # 3. Dynamic context (typically from KnowledgeMiddleware)
    if context:
        parts.append(f"\n# Context\n\n{context}")

    # 4. Operator guidelines — OVERRIDE THIS in your fork
    parts.append("""
# Guidelines

- Prefer direct answers for simple requests; use tools when they add
  information the user asked for.
- Delegate to subagents via the `task` tool only for genuinely parallel
  or specialized work.
- If a tool fails, read the error, try once with corrected inputs, then
  surface the failure to the user with the concrete error string.
- When you're waiting for something to finish — a ship/build/job to
  complete, a cooldown, an ETA a tool just reported ("arriving in 37s") —
  do NOT call a status tool in a loop to wait it out; that burns the whole
  turn. Call `wait(seconds, then=…)` to yield and be re-triggered when it's
  ready (it ends the turn and resumes you with `then`).
- Answer directly and naturally. Your reasoning is streamed separately (native
  reasoning), so think freely — don't narrate your deliberation in the answer.
""")

    return "\n\n".join(parts)


def _build_projects_section(projects) -> str:
    """Render the managed-project workspaces the fs tools are fenced to."""
    lines = [
        "# Managed projects",
        "",
        "You operate on these project workspaces via the filesystem tools "
        "(`list_projects`, `read_file`, `list_dir`, `find_files`, `search_files`, "
        "and — in read-write projects — `write_file`/`edit_file`). All paths are "
        "fenced to these roots; you cannot read or write outside them.",
        "",
    ]
    rendered = 0
    for p in projects:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        path = str(p.get("path") or "").strip()
        if not name or not path:
            continue
        mode = "read-write" if p.get("write") else "read-only"
        lines.append(f"- **{name}** ({mode}) — `{path}`")
        rendered += 1
    return "\n".join(lines) if rendered else ""


def _build_subagent_section() -> str:
    """Build the subagent delegation instructions.

    The background-delegation guidance is unconditional (not gated on a runtime
    flag) so this prompt stays a turn-stable cache prefix shared by the live graph,
    the cache warmer, and the native loop. The ``task`` tool always accepts
    ``run_in_background``; it degrades to synchronous execution if the background
    manager is disabled (ADR 0050)."""
    lines = [
        "# Subagent Delegation",
        "",
        "You can delegate to specialized subagents with the `task` tool. Each has focused",
        "tools and a domain-specific prompt. **Match the work to the subagent whose",
        "description fits, and prefer delegating specialized or long-running work to it over",
        "grinding it out inline in your own turn.** The roster (use the names verbatim as",
        "`subagent_type`):",
        "",
    ]

    for name, config in SUBAGENT_REGISTRY.items():
        lines.append(f"- **{name}**: {config.description}")
        if config.tools:
            lines.append(f"  Tools: {', '.join(config.tools)}")
        lines.append("")

    lines.extend(
        [
            "**Rules:**",
            "- Pick the most specialized subagent whose description matches the task. Don't do",
            "  domain work a subagent is purpose-built for (deep research, strategy/planning,",
            "  multi-step gathering) inline — delegate it.",
            "- For simple, quick requests, answer directly without delegation.",
            "- Run independent delegations concurrently — one `task` call each, or `task_batch`",
            "  (bounded automatically by the configured concurrency cap).",
            "- Subagents cannot spawn further subagents.",
            "",
            "**Background delegation (`run_in_background=true` on `task`):** default to this for",
            "any long, independent, or tool/quota-heavy delegation — deep research, a strategic",
            "audit, anything that will take many turns or lots of web/tool calls. It returns",
            "immediately with a job id and the result is delivered back to you automatically on a",
            "later turn, so the conversation stays live instead of freezing on a multi-minute",
            "delegation. Use foreground (the default) only when you need the result to finish your",
            "current reply. Once you background a task, do NOT poll it or spawn a duplicate — you",
            "will be notified when it completes.",
        ]
    )

    return "\n".join(lines)


def build_subagent_prompt(agent_name: str, workspace: str = "/sandbox") -> str:
    """Build system prompt for a specific subagent.

    Subagents answer naturally (no `<scratch_pad>`/`<output>` protocol); their final
    message content is the result the lead agent reads. Reasoning streams natively.
    """
    config = SUBAGENT_REGISTRY.get(agent_name)
    base = config.system_prompt if config else "You are a subagent. Complete the delegated task efficiently."
    return base
