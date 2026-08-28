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


# Capability group membership — which tool names imply which operating-model
# paragraphs. Derived from the binding gates in tools/lg_tools.py:get_all_tools.
_GOAL_TOOLS = frozenset({"set_goal", "update_goal_plan", "abandon_goal"})
_TASK_TOOLS = frozenset({"task_create", "task_update", "task_close"})
_SCHEDULE_TOOLS = frozenset({"schedule_task"})
_WATCH_TOOLS = frozenset({"create_watch"})
_WAIT_TOOLS = frozenset({"wait"})


def _build_operating_model(bound_tools: frozenset[str] | None = None) -> str:
    """Build the operating model section (ADR 0079, #3190), including only
    paragraphs whose capabilities are actually bound.

    When ``bound_tools`` is None (legacy callers), the full section is emitted
    unconditionally — the pre-#3190 behavior.
    """
    has_goal = bound_tools is None or bool(bound_tools & _GOAL_TOOLS)
    has_tasks = bound_tools is None or bool(bound_tools & _TASK_TOOLS)
    has_schedule = bound_tools is None or bool(bound_tools & _SCHEDULE_TOOLS)
    has_watch = bound_tools is None or bool(bound_tools & _WATCH_TOOLS)
    has_wait = bound_tools is None or bool(bound_tools & _WAIT_TOOLS)
    has_any = has_goal or has_tasks or has_schedule or has_watch

    if not has_any and not has_wait:
        return ""

    lines = ["# Operating model", ""]

    lines.append(
        "You operate as a long-horizon autonomous agent, not a one-shot responder. You hold objectives\n"
        "across turns and drive them to done — observing your own state, planning, acting, and correcting."
    )

    if has_any:
        ws_items = []
        if has_goal:
            ws_items.append("your **active goal + its plan**")
        if has_tasks:
            ws_items.append("your **open tasks**")
        if has_watch:
            ws_items.append("your **active watches**")
        if has_schedule:
            ws_items.append("your **pending schedules**")
        lines.append("")
        lines.append(
            "Your durable working-state is shown to you each turn in a `<working_state>` block:\n"
            + ", ".join(ws_items)
            + ". Read it first — it is what you are responsible for."
        )

        # OODA loop — adapt references to bound capabilities only
        lines.append("")
        lines.append("Run this loop:")
        lines.append("")

        observe_triggers = ["an operator asked"]
        if has_schedule:
            observe_triggers.append("a schedule fired")
        if has_watch:
            observe_triggers.append("a watch tripped")
        lines.append(
            "- **Observe** — read `<working_state>` and why you are awake ("
            + ", ".join(observe_triggers)
            + " — stated at the top of the turn). Take in what changed."
        )

        orient_parts = []
        if has_goal:
            orient_parts.append(
                "keep your **plan** current with `update_goal_plan` (it is your world-model:\n"
                "  what's done, what's next, what failed — persisted and fed back to you every turn)"
            )
        if has_tasks:
            orient_parts.append(
                "break the goal into concrete **tasks** with `task_create`; "
                "the task board is the goal's backlog"
                if has_goal
                else "break work into concrete **tasks** with `task_create`"
            )
        if orient_parts:
            lines.append("- **Orient** — " + ". ".join(orient_parts) + ".")
        lines.append(
            "- **Decide** — pick the next concrete step. Decide whether to do it now, or, if it depends on\n"
            "  something that isn't ready, to hand it off (below)."
        )
        act_note = " Update/close tasks as you go." if has_tasks else ""
        lines.append(
            "- **Act** — do the step (directly or by delegating with `task`)." + act_note
        )

        # Primitives block — only include bound groups
        primitives = []
        if has_goal:
            primitives.append(
                "- **Goal** (`set_goal`) — your standing objective. A deterministic verifier decides DONE, never\n"
                "  your own say-so; keep working until it passes, or call `abandon_goal` if it's truly out of scope."
            )
        if has_tasks:
            primitives.append(
                "- **Tasks** (`task_create`/`task_update`/`task_close`) — the goal's backlog. Decompose the goal\n"
                "  into tasks and drive them down; this is your board (NOT any built-in todo tool)."
                if has_goal
                else "- **Tasks** (`task_create`/`task_update`/`task_close`) — your work board. Decompose work\n"
                "  into tasks and drive them down."
            )
        if has_schedule:
            primitives.append(
                "- **Schedule** (`schedule_task`) — do something *later* or on a cadence (a follow-up, a recurring\n"
                "  sweep). The prompt you schedule must be self-contained."
            )
        if has_watch:
            primitives.append(
                "- **Watch** (`create_watch`) — supervise an external *condition* (a deploy, CI, a metric, a peer's\n"
                "  PR); when it trips you're brought back to react. Hold as many as you need, in parallel."
            )
        if primitives:
            lines.append("")
            count = len(primitives)
            lines.append(
                f"Compose your primitive{'s' if count > 1 else ''}"
                + (f" — {'they are' if count > 1 else 'it is'} one system"
                   + (", not " + str(count) if count > 1 else "") + ":"
                   if count > 1
                   else ":")
            )
            lines.append("")
            lines.extend(primitives)

    # Async-handoff rule — adapt to available primitives
    if has_watch or has_schedule or has_wait:
        handoff_options = []
        if has_watch:
            handoff_options.append("a **watch** on the condition")
        if has_schedule:
            handoff_options.append("a **schedule** for a known time")
        if has_wait:
            handoff_options.append("`wait` for a short countdown")
        lines.append("")
        if len(handoff_options) == 1:
            options_text = handoff_options[0]
        else:
            options_text = " (or ".join(handoff_options) + ")"
        lines.append(
            "**Do not spin waiting on async work.** When your next step depends on something in flight — a\n"
            "build, a delegated peer agent, CI, a review — do NOT burn turns polling it. Set "
            + options_text
            + " and END the\nturn. You'll be resumed with context when it's actually ready. "
            "Persisting means yielding and\ncoming back, not looping."
        )

    lines.append("")
    lines.append(
        "**Self-correct.** If your plan isn't producing progress, change the approach and say so"
        + (" in the\nplan" if has_goal else "")
        + ". If you're blocked, record what's blocking you. Don't repeat an action that already failed."
    )

    return "\n".join(lines).strip()


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
    bound_tool_names: frozenset[str] | None = None,
) -> str:
    """Build the complete system prompt for the lead agent.

    ``context`` is injected verbatim at the end of the prompt (before
    the response-format block) — ``KnowledgeMiddleware`` is the typical
    caller, passing in retrieved knowledge-store hits.

    ``projects`` (ADR 0007) — when the fenced filesystem toolset is enabled,
    the list of managed project workspaces ``[{name, path, write}]`` is named in
    the prompt so the agent knows the dirs it can operate on (and which are
    read-only). Inert when None.

    ``bound_tool_names`` (#3190, ADR 0108 D6) — the set of tool names actually
    bound on this graph build. When provided, capability-specific doctrine
    (operating model, guidelines) is generated only for bound capabilities.
    None emits everything unconditionally (legacy behavior).
    """
    return "\n\n".join(
        text
        for _label, text in build_system_prompt_parts(
            workspace=workspace,
            include_subagents=include_subagents,
            context=context,
            projects=projects,
            bound_tool_names=bound_tool_names,
        )
    )


def build_system_prompt_parts(
    workspace: str = "/sandbox",
    include_subagents: bool = True,
    context: str = "",
    projects=None,
    bound_tool_names: frozenset[str] | None = None,
) -> list[tuple[str, str]]:
    """The lead-agent system prompt as labeled ``(label, text)`` sections —
    the segmentation substrate for the prompt viewer's context-budget
    breakdown (#2243 P2). ``build_system_prompt`` is exactly these texts
    joined with a blank line (a test pins the equivalence), so the labels
    annotate the REAL prompt, never a reconstruction.

    ``bound_tool_names`` (#3190): when provided, capability-specific sections
    are generated only for tools that are actually bound. None = emit
    everything (legacy behavior, safe default for callers that don't thread it).
    """
    parts: list[tuple[str, str]] = []

    # 1. Identity — the instance's OWN persona. Prefer the canonical live SOUL:
    # ``instance_paths().soul_path`` = ``<instance_root>/config/SOUL.md`` — the path
    # ``config_io.read_soul``/``write_soul`` use and the persona drawer edits, and
    # PROTOAGENT_HOME-aware. This is what makes a FLEET MEMBER load ITS OWN persona:
    # a member is spawned directly by the supervisor (not via entrypoint.sh, which
    # only the primary runs to copy config/SOUL.md → /sandbox/SOUL.md), and it
    # inherits the hub's default ``workspace`` (/sandbox), so the legacy
    # ``{workspace}/SOUL.md`` read below resolved the HUB's file — a placeholder for
    # the member — leaving it with NO identity (it then collapses onto whatever the
    # injected team context foregrounds — the hub agent). Reading the instance's own
    # config/SOUL.md fixes members and also gives the primary its real persona
    # regardless of the entrypoint copy. Fall back to the legacy runtime copy
    # (``{workspace}/SOUL.md``), then the repo/bundle default.
    soul = ""
    try:
        from infra.paths import instance_paths

        soul = _read_file(instance_paths().soul_path)
    except Exception:  # noqa: BLE001 — path resolution must never break prompt building
        soul = ""
    if not soul:
        soul = _read_file(f"{workspace}/SOUL.md")
    if not soul:
        soul = _read_file(Path(__file__).parent.parent / "config" / "SOUL.md")
    if soul:
        parts.append(("SOUL", soul))
    else:
        parts.append(
            (
                "SOUL",
                "# Agent\n\n"
                "You are a protoAgent — an A2A-compliant LangGraph agent. "
                "Replace this placeholder by writing an SOUL.md in the workspace "
                "with your agent's identity, role, and personality.",
            )
        )

    # 2. Subagent instructions
    if include_subagents:
        parts.append(("Subagents", _build_subagent_section()))

    # 2b. Managed project workspaces (ADR 0007 — fenced filesystem toolset).
    if projects:
        section = _build_projects_section(projects)
        if section:
            parts.append(("Managed projects", section))

    # 2c. Delegate collaboration (#3042) — only when delegates are configured. Teaches
    # the lead that when the operator asks two+ participants to WORK TOGETHER, moderating
    # the rounds is ITS job: subordinate delegates (acp / model) answer their caller and
    # cannot address each other, so someone has to run the back-and-forth, and that
    # someone is the orchestrator. A single `@x @y` from the operator is one round each,
    # not a discussion.
    collab = _build_collaboration_section()
    if collab:
        parts.append(("Collaboration", collab))

    # 3. Dynamic context (typically from KnowledgeMiddleware)
    if context:
        parts.append(("Context", f"\n# Context\n\n{context}"))

    # 3.5 Operating model — the autonomous doctrine (ADR 0079, #3190). Capability-derived:
    # only names tools that are actually bound. Empty when none of the autonomous primitives
    # (goal, tasks, schedule, watch, wait) are present — a stripped deployment gets a
    # shorter, honest prompt instead of instructions it can't follow.
    op_model = _build_operating_model(bound_tool_names)
    if op_model:
        parts.append(("Operating model", op_model))

    # 4. Operator guidelines — OVERRIDE THIS in your fork
    _bt = bound_tool_names
    guideline_lines = [
        "# Guidelines",
        "",
        "- Prefer direct answers for simple requests; use tools when they add",
        "  information the user asked for.",
    ]
    if _bt is None or "task" in _bt:
        guideline_lines.extend([
            "- Delegate to subagents via the `task` tool only for genuinely parallel",
            "  or specialized work.",
        ])
    guideline_lines.extend([
        "- If a tool fails, read the error, try once with corrected inputs, then",
        "  surface the failure to the user with the concrete error string.",
    ])
    if _bt is None or "wait" in _bt:
        guideline_lines.extend([
            "- When you're waiting for something to finish — a ship/build/job to",
            "  complete, a cooldown, an ETA a tool just reported (\"arriving in 37s\") —",
            "  do NOT call a status tool in a loop to wait it out; that burns the whole",
            "  turn. Call `wait(seconds, then=…)` to yield and be re-triggered when it's",
            "  ready (it ends the turn and resumes you with `then`).",
        ])
    guideline_lines.extend([
        "- Answer directly and naturally. Your reasoning is streamed separately (native",
        "  reasoning), so think freely — don't narrate your deliberation in the answer.",
    ])
    parts.append(("Guidelines", "\n".join(guideline_lines)))

    return parts


def _build_projects_section(projects) -> str:
    """Render the managed-project workspaces the fs tools are fenced to."""
    lines = [
        "# Managed projects",
        "",
        "You operate on these project workspaces via the filesystem tools "
        "(`list_projects`, `read_file`, `list_dir`, `find_files`, `search_files`, "
        "and — in read-write projects — `write_file`/`edit_file`, plus `delete_file`, "
        "which always asks first and is off in no-delete projects). All paths are "
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



def _build_collaboration_section() -> str:
    """Moderation doctrine for multi-agent collaboration (#3042) — empty unless delegates
    are configured, so an instance with none carries none of it.

    The load-bearing idea: ACP and model delegates are SUBORDINATES — they answer their
    caller and cannot route to each other. So "have X and Y work together" cannot happen
    among them on its own; the lead must run the rounds. This is the same reason
    reply-text agent-to-agent chaining was removed (a subordinate must not route onward):
    coordination is the orchestrator's job, done with `delegate_to`, not a capability
    handed to the delegates.
    """
    try:
        from runtime.state import STATE

        reg = getattr(STATE, "delegate_registry", None)
        if reg is None or not reg.names():
            return ""
    except Exception:  # noqa: BLE001 — prompt building must never break on registry state
        return ""
    return (
        "# Working with delegates\n\n"
        "When the operator asks two or more participants to **work together** / collaborate "
        "/ reach agreement on something (e.g. \"have proto and reviewer sort out X\"), YOU "
        "moderate it — that is orchestration, and it is your job:\n"
        "- Delegates of type `acp` and `model` are subordinates: they answer you and cannot "
        "address each other. So run the discussion yourself — `delegate_to` one, relay its "
        "points to the next (`delegate_to` again with that context), carry objections back, "
        "and continue round by round until they converge or clearly won't.\n"
        "- Give each participant a ROLE when the task is code or artifacts (one drafts, one "
        "reviews) rather than having both edit in parallel — two coders in one workspace "
        "collide.\n"
        "- Decide when it's settled and say so, with the outcome and who held which "
        "position. Don't loop indefinitely — a handful of rounds, then report even a "
        "disagreement.\n"
        "- Prefer `background=True` for anything that will take more than a couple of quick "
        "exchanges, and synthesize when the replies land.\n"
        "- A user message that still opens with one or more configured `@name` targets reached "
        "you only because every direct address found that stopped local fleet member "
        "unreachable. Honor the direct address: call `delegate_to` for each named target now. "
        "That tool can ask the operator to start the member and retry. Do not answer in the "
        "named participant's place.\n"
        "A bare `@x @y <task>` typed by the OPERATOR is one message to each, in turn — a "
        "quick parallel consult, not a discussion. If they need to iterate, that's the "
        "moderation loop above, and the operator hands it to you as an ordinary request."
    )

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
            "current reply. This is a general discipline for ANY background delegation (the",
            "`task` subagent tool AND, e.g., a fleet `delegate_to`): once you background the",
            "work, END your turn — do NOT try to wait/poll for it or spawn a duplicate. Each",
            "result is delivered back to you automatically on a later turn; synthesize the",
            "replies when they arrive (on a fan-out, wait for ALL of them first).",
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
