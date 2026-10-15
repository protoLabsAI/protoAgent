"""Stable prompt composition: persona, roles, projects, collaboration and rules.

Skills are loaded on demand; memory, the skills index and operational state ride
the ephemeral projection, not this cached prefix. The ``context`` parameter is a
legacy seam, unused by the runtime. Parts label the actual composed text for the
prompt viewer. See docs/explanation/prompt-contracts.md for ordering and budgets.
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

#: The capability map, public: group name → the tool names that switch its doctrine on.
#: ``tests/test_prompt_budgets.py`` iterates this to pin the honesty invariant (binding
#: one group never names another's tools); add a group here when you add a paragraph.
CAPABILITY_GROUPS: dict[str, frozenset[str]] = {
    "goal": _GOAL_TOOLS,
    "tasks": _TASK_TOOLS,
    "schedule": _SCHEDULE_TOOLS,
    "watch": _WATCH_TOOLS,
    "wait": _WAIT_TOOLS,
}


def _build_operating_model(bound_tools: frozenset[str] | None = None) -> str:
    """Capability-specific operating rules; None retains the legacy full set.

    Background progress/yield policy has one home in Guidelines. This section
    explains the durable primitives without repeating their invocation schemas.
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
    if has_any:
        lines.extend([
            "Drive standing objectives across turns, using your live commitments:",
            "- **Observe** — read `<working_state>` and why this turn started.",
            "- **Orient** — revise the plan when circumstances change.",
            "- **Decide** — choose the next concrete step and its dependencies.",
            "- **Act** — do the step.",
            "",
        ])
    def available(name: str) -> bool:
        return bound_tools is None or name in bound_tools

    if has_goal:
        actions = [text for name, text in (
            ("set_goal", "`set_goal` establishes an objective."),
            ("update_goal_plan", "`update_goal_plan` keeps its plan current."),
            ("abandon_goal", "Use `abandon_goal` when the objective is out of scope."),
        ) if available(name)]
        lines.append("- **Goals:** " + " ".join(actions) +
                     " Only the deterministic verifier decides DONE; continue until it passes or report a blocker.")
    if has_tasks:
        actions = [text for name, text in (
            ("task_create", "decompose work with `task_create`"),
            ("task_update", "record progress with `task_update`"),
            ("task_close", "finish with `task_close`"),
        ) if available(name)]
        lines.append("- **Tasks:** " + "; ".join(actions) + ".")
    if has_schedule:
        lines.append(
            "- **Schedules:** use `schedule_task` for a known future time or cadence. "
            "Its prompt must stand alone when you resume."
        )
    if has_watch:
        lines.append(
            "- **Watches:** use `create_watch` for an external condition. "
            "It resumes you when the condition trips."
        )
    if has_wait:
        lines.append(
            "- **Wait:** `wait(seconds, then=…)` ends this turn and resumes you after "
            "a short countdown with the supplied next step."
        )
    lines.append("\nIf progress stalls, change the approach and record the blocker; don't repeat a failed action unchanged.")
    return "\n".join(lines)


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

    ``context`` is a legacy static Context section before the operating model.
    Runtime memory/skills/state use request-time projection instead; callers
    must not put per-turn content into this cached prefix.

    ``projects`` (ADR 0007) — when the fenced filesystem toolset is enabled,
    the list of managed project workspaces ``[{name, path, write}]`` is named in
    the prompt so the agent knows the dirs it can operate on (and which are
    read-only). Inert when None.

    ``bound_tool_names`` (#3190, ADR 0108 D6) — the set of tool names actually
    bound on this graph build. When provided, capability-specific doctrine
    (operating model, guidelines) is generated only for bound capabilities.
    None retains potential-capability guidance with a runtime-availability qualifier.
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
    potential capabilities with an explicit runtime-availability qualification.
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
        from infra.paths import app_root

        soul = _read_file(app_root() / "config" / "SOUL.md")
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
    if include_subagents and (bound_tool_names is None or bound_tool_names & {"task", "task_batch"}):
        parts.append(("Subagents", _build_subagent_section(bound_tool_names)))

    # 2b. Managed project workspaces (ADR 0007 — fenced filesystem toolset).
    if projects:
        section = _build_projects_section(projects, bound_tool_names)
        if section:
            parts.append(("Managed projects", section))

    # 2c. Delegate collaboration (#3042) — only when delegates are configured. Teaches
    # the lead that when the operator asks two+ participants to WORK TOGETHER, moderating
    # the rounds is ITS job: subordinate delegates (acp / model) answer their caller and
    # cannot address each other, so someone has to run the back-and-forth, and that
    # someone is the orchestrator. A single `@x @y` from the operator is one round each,
    # not a discussion.
    collab = _build_collaboration_section() if bound_tool_names is None or "delegate_to" in bound_tool_names else ""
    if collab:
        parts.append(("Collaboration", collab))

    # 3. Legacy static context; runtime projection never uses this seam.
    if context:
        parts.append(("Context", f"\n# Context\n\n{context}"))

    # 3.5 Operating model — the autonomous doctrine (ADR 0079, #3190). Capability-derived:
    # only names tools that are actually bound. Empty when none of the autonomous primitives
    # (goal, tasks, schedule, watch, wait) are present — a stripped deployment gets a
    # shorter, honest prompt instead of instructions it can't follow.
    op_model = _build_operating_model(bound_tool_names)
    if op_model:
        parts.append(("Operating model", op_model))

    # 4. Universal response rules and the single background-work policy.
    guideline_lines = [
        "# Guidelines",
        "",
        "- Honor explicit requests to use a tool. Otherwise answer simple questions directly and use tools when they add needed information.",
        "- If a tool fails, read the error, retry once with corrected inputs, then report the concrete failure.",
        "- Answer naturally. Reasoning streams separately; don't narrate deliberation in the answer.",
    ]
    async_tools = {"task", "task_batch", "delegate_to", "wait", "schedule_task", "create_watch"}
    if bound_tool_names is None or bound_tool_names & async_tools:
        guideline_lines.append(
            "- **Do not spin on async work.** Continue independent work while background jobs run. "
            "When no independent step can progress, END your turn and resume when notified. "
            "If completion is not automatically delivered, arrange a supported wake-up before yielding. "
            "Do not poll, re-check or spawn duplicates to wait for a result. "
            "For fan-out, synthesize once all required results arrive."
        )
    if bound_tool_names is None:
        guideline_lines.append("- Capability guidance describes potential features; use only tools exposed by your runtime.")
    parts.append(("Guidelines", "\n".join(guideline_lines)))

    return parts


def _build_projects_section(projects, bound_tool_names: frozenset[str] | None = None) -> str:
    """Render roots and only the filesystem operations reachable by this lead."""
    ordered = ("list_projects", "read_file", "list_dir", "find_files", "search_files",
               "write_file", "edit_file", "delete_file")
    available = [name for name in ordered if bound_tool_names is None or name in bound_tool_names]
    if not available:
        return ""
    lines = [
        "# Managed projects", "",
        "Available filesystem tools: " + ", ".join(f"`{name}`" for name in available) + ". "
        "Paths are fenced to these roots. Project permissions still apply; read-only roots cannot be modified.",
    ]
    if "delete_file" in available:
        lines.append("`delete_file` always asks first and is unavailable in no-delete projects.")
    lines.append("")
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
        "When asked to have participants work together, moderate the discussion via `delegate_to`. "
        "ACP/model delegates are subordinates: they answer you and cannot address each other. "
        "Relay proposals and objections between them. Assign distinct roles (one drafts, one reviews) "
        "to avoid concurrent edits in a shared workspace. After a handful of rounds, report the "
        "outcome and any unresolved disagreement. Prefer `background=True` for long exchanges.\n"
        "A bare operator `@x @y <task>` is one message to each, not an iterative discussion. "
        "If an @addressed message reaches you because every direct address found its stopped "
        "fleet member unreachable, call `delegate_to` for each named target now; it can ask "
        "the operator to start the member and retry. Do not answer in the named participant's place."
    )


def _build_subagent_section(bound_tools: frozenset[str] | None = None) -> str:
    """Lead-visible roles; detailed arguments live in task/task_batch schemas."""
    has_task = bound_tools is None or "task" in bound_tools
    has_batch = bound_tools is None or "task_batch" in bound_tools
    invocation = "`task`" if has_task else "`task_batch`"
    lines = [
        "# Subagent Delegation",
        "",
        f"Use {invocation} for specialized or long-running work; answer simple requests directly. "
        "Choose the best matching role and use its name verbatim as `subagent_type`:",
        "",
    ]
    for name, config in SUBAGENT_REGISTRY.items():
        if config.lead_visible:
            lines.append(f"- **{name}**: {config.description}")
    lines.extend([
        "",
        ("Run independent delegations concurrently with `task_batch`. " if has_batch else
         "Run independent delegations with parallel `task` calls. ") +
        "Subagents cannot spawn further subagents.",
        "Use `run_in_background=true` for long independent work; results arrive automatically "
        "on a later turn. Use foreground when you need the result to finish this reply. "
        "Without a background manager, requests run in the foreground. "
        "Follow the async-work rule in Guidelines once a job is confirmed running in the background.",
    ])
    return "\n".join(lines)


def build_subagent_prompt(agent_name: str, workspace: str = "/sandbox") -> str:
    """Build system prompt for a specific subagent.

    Subagents answer naturally (no `<scratch_pad>`/`<output>` protocol); their final
    message content is the result the lead agent reads. Reasoning streams natively.
    """
    config = SUBAGENT_REGISTRY.get(agent_name)
    base = config.system_prompt if config else "You are a subagent. Complete the delegated task efficiently."
    return base
