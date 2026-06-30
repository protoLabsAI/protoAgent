"""Main LangGraph agent for protoAgent.

Builds the agent graph with middleware, tools, and subagent support.
Uses langchain's create_agent() with AgentMiddleware for the DeerFlow pattern.
"""

from typing import Annotated, Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt import InjectedState

from graph.config import LangGraphConfig
from graph.llm import create_llm
from graph.prompts import build_system_prompt, build_subagent_prompt
from graph.middleware.audit import AuditMiddleware
from graph.middleware.knowledge import KnowledgeMiddleware
from graph.middleware.memory import SessionSummaryMiddleware
from graph.middleware.message_capture import MessageCaptureMiddleware
from graph.state import ProtoAgentState
from graph.subagents.config import SUBAGENT_REGISTRY
from tools.lg_tools import HITL_TOOL_NAMES, _session_id_from, get_all_tools


def _build_middleware(config: LangGraphConfig, knowledge_store=None, skills_index=None, extra_middleware=None):
    middleware = []

    # Self-heal a thread left with a dangling tool_call (a tool that hung while
    # the user sent the next message, an interrupted turn, …) before anything
    # else touches the history — otherwise the provider 400s every later turn
    # ("insufficient tool messages following tool_calls"). No-op on a healthy
    # history, so it never affects a normal turn.
    from graph.middleware.tool_call_repair import ToolCallRepairMiddleware

    middleware.append(ToolCallRepairMiddleware())

    # End the turn after the `wait` tool runs (yield-and-resume instead of
    # busy-polling). No-op on any turn that didn't call `wait`.
    from graph.middleware.wait_yield import WaitYieldMiddleware

    middleware.append(WaitYieldMiddleware())

    # Break a no-progress tool loop (#1446) — the same tool + args returning the
    # same result over and over. Nudge once, then end the turn instead of spinning
    # to the recursion limit. No-op on any healthy/varied history.
    from graph.middleware.stall_guard import StallGuardMiddleware

    middleware.append(StallGuardMiddleware())

    # Mid-turn user steering (spike) — fold queued user input into the running
    # turn at the next model call, so a user can redirect ongoing work without
    # stopping the stream. No-op when nothing was injected this turn.
    from graph.middleware.steering import SteeringMiddleware

    middleware.append(SteeringMiddleware())

    # Per-turn model override (per chat tab). Outermost wrap_model_call so the
    # PromptCache below sees the ACTUAL model when deciding caching. No-op unless
    # the turn carries state["model"].
    from graph.middleware.model_override import ModelOverrideMiddleware

    middleware.append(ModelOverrideMiddleware(config))

    # Prompt caching + knowledge-context delivery (wrap_model_call). Added
    # first/outermost so the cache breakpoint lands on the stable system
    # prefix; KnowledgeMiddleware's context is delivered just after it.
    from graph.middleware.prompt_cache import PromptCacheMiddleware

    middleware.append(
        PromptCacheMiddleware(
            enabled=config.prompt_cache_enabled,
            ttl=config.prompt_cache_ttl,
            force=config.prompt_cache_force,
        )
    )

    # Enforcement gate first (outermost) so disallowed/rate-limited tool
    # calls are blocked before any execution. Opt-in via config.
    if config.enforcement_enabled and (config.enforcement_disallowed_tools or config.enforcement_rate_limits):
        from graph.middleware.enforcement import EnforcementMiddleware

        middleware.append(
            EnforcementMiddleware(
                disallowed_tools=config.enforcement_disallowed_tools,
                rate_limits=config.enforcement_rate_limits,
            )
        )

    # KnowledgeMiddleware also carries the always-on skill index (the
    # <available_skills> injection, ADR 0060). Build it when knowledge OR skills
    # is active, so skills work even on a KB-less agent (the store is None-tolerant).
    _skills_index = skills_index if config.skills_enabled else None
    if (config.knowledge_middleware and knowledge_store) or _skills_index is not None:
        middleware.append(
            KnowledgeMiddleware(
                knowledge_store if config.knowledge_middleware else None,
                top_k=config.knowledge_top_k,
                skills_index=_skills_index,
                skills_top_k=config.skills_top_k,
            )
        )

    # Deferred-tool disclosure (ADR 0005 #3) — trims the per-call tool set to
    # base + agent-loaded. Opt-in; the search_tools meta-tool is added to the
    # tool list in create_agent_graph when this is on.
    if config.tools_deferred_enabled:
        from graph.middleware.tool_deferral import ToolDeferralMiddleware
        from tools.lg_tools import resolve_deferred_keep

        middleware.append(ToolDeferralMiddleware(resolve_deferred_keep(config.tools_deferred_keep)))

    if config.audit_middleware:
        middleware.append(AuditMiddleware())

    if config.memory_middleware:
        middleware.append(SessionSummaryMiddleware(knowledge_store))

    # Context compaction — summarize old history near the context limit.
    # CountingSummarizationMiddleware adds a Prometheus compaction counter on top
    # of langchain's SummarizationMiddleware (ADR 0006 — proves the lever fires).
    if config.compaction_enabled:
        from graph.middleware.compaction import CountingSummarizationMiddleware

        summ_model = create_llm(config, model_name=_resolve_aux_model(config, config.compaction_model))
        keep = ("messages", config.compaction_keep_messages)
        try:
            mw = CountingSummarizationMiddleware(
                model=summ_model,
                trigger=_parse_compaction_trigger(config.compaction_trigger),
                keep=keep,
            )
        except ValueError:
            # `fraction:`/`tokens:` triggers need the model's context-window
            # profile, which custom gateway aliases don't expose — langchain
            # raises here. Fall back to a message-count trigger so compaction
            # still runs instead of taking down the whole graph at load.
            import logging

            fallback = max(config.compaction_keep_messages * 3, 60)
            logging.getLogger(__name__).warning(
                "[compaction] trigger %r needs a model profile that %r lacks; falling back to messages:%d",
                config.compaction_trigger,
                config.model_name,
                fallback,
            )
            mw = CountingSummarizationMiddleware(model=summ_model, trigger=("messages", fallback), keep=keep)
        middleware.append(mw)

    # Model routing / failover — retry on fallback models (same gateway).
    if config.routing_fallback_models:
        from langchain.agents.middleware import ModelFallbackMiddleware

        fallbacks = [create_llm(config, model_name=m) for m in config.routing_fallback_models]
        middleware.append(ModelFallbackMiddleware(*fallbacks))

    # Plugin-contributed middleware (ADR 0032) — appended after the core chain but
    # before MessageCapture, so their before/after-model + tool hooks run and the
    # turn is still captured. Each is already an instance (factories resolved in
    # agent_init); skip falsy entries (a factory may opt out by returning None).
    for mw in extra_middleware or []:
        if mw is not None:
            middleware.append(mw)

    middleware.append(MessageCaptureMiddleware())

    return middleware


def _resolve_aux_model(config, specific: str = "") -> str | None:
    """Pick the model for an auxiliary call: a specific override, else the
    shared ``routing.aux_model`` fast alias, else None (→ the main model)."""
    for candidate in (specific, getattr(config, "aux_model", "")):
        cleaned = (candidate or "").strip()
        if cleaned:
            return cleaned
    return None


def _auto_background_seconds() -> float:
    """Time budget (seconds) after which a *foreground* ``task`` delegation transparently
    detaches to the background (ADR 0051). ``BACKGROUND_AUTO_S`` env; 0 (default) = off."""
    import os

    try:
        return max(0.0, float(os.environ.get("BACKGROUND_AUTO_S", "0")))
    except ValueError:
        return 0.0


def _parse_compaction_trigger(spec: str):
    """Parse 'fraction:0.8' / 'tokens:120000' / 'messages:80' → langchain trigger tuple."""
    try:
        kind, _, val = spec.partition(":")
        kind = kind.strip().lower()
        if kind == "fraction":
            return ("fraction", float(val))
        if kind in ("tokens", "messages"):
            return (kind, int(val))
    except (ValueError, AttributeError):
        pass
    return ("fraction", 0.8)


class SubagentError(RuntimeError):
    """A subagent delegation failed hard (the subagent itself raised). The ``task``
    tool converts this into a ``status="error"`` ToolMessage so the console renders
    the delegation card as a failure (X) — not a green "done" wrapping an ``Error:``
    string (which read as success). ``task_batch`` reports it inline and continues."""


def _subagent_tools(sub_config, tool_map: dict) -> list:
    """Resolve a subagent's bound tools from its allowlist, hard-denying the lead-only HITL
    interrupt tools (``HITL_TOOL_NAMES``) even if the config lists them. A subagent runs on a
    checkpointer-less graph, so ``ask_human`` / ``request_user_input`` would fail opaquely
    mid-delegation — this is the enforced backstop to the convention that no subagent allowlist
    names them, so a fork can't enable one by editing a ``SubagentConfig.tools`` list."""
    blocked = [n for n in sub_config.tools if n in HITL_TOOL_NAMES]
    if blocked:
        import logging

        logging.getLogger(__name__).warning(
            "[subagent] '%s' lists HITL tool(s) %s — dropping them; a subagent can't resume a "
            "LangGraph interrupt (no checkpointer). Remove them from its tools list.",
            sub_config.name,
            blocked,
        )
    return [tool_map[name] for name in sub_config.tools if name in tool_map and name not in HITL_TOOL_NAMES]


async def _run_subagent(
    *,
    config,
    tool_map: dict,
    available_subagents: str,
    description: str,
    prompt: str,
    subagent_type: str,
    truncate: int | None = None,
    parent_task_id: str | None = None,
) -> str:
    """Run a single subagent delegation and return its output text.

    Shared by the single ``task`` tool and the concurrent ``task_batch`` tool.
    ``truncate`` (chars) bounds the returned body so a wide fan-out can't blow
    the parent context; ``None`` means unbounded (single-task path).
    ``parent_task_id`` is the delegating ``task``/``task_batch`` tool-call id; when
    set, every event the subagent emits is tagged with it so the console can nest
    the subagent's own tool cards under the delegation card.
    """
    sub_config = SUBAGENT_REGISTRY.get(subagent_type)
    if not sub_config:
        return f"Error: Unknown subagent '{subagent_type}'. Available: {available_subagents}"

    sub_tools = _subagent_tools(sub_config, tool_map)
    if not sub_tools:
        return f"Error: No tools available for subagent '{subagent_type}'."

    # Subagent model: per-subagent override → routing.aux_model → main model.
    sub_llm = create_llm(config, model_name=_resolve_aux_model(config, getattr(sub_config, "model", "")))

    # Subagents do real work (tool calls), so the enforcement rail (ADR 0003) should
    # cover them too — not just the lead agent. Mirror the lead's gate so a disallowed/
    # rate-limited tool is blocked inside a delegation. (Per-instance limiter: each
    # subagent run gets its own window — a per-delegation cap, not a shared budget.)
    sub_middleware = [AuditMiddleware()]
    if getattr(config, "enforcement_enabled", False) and (
        config.enforcement_disallowed_tools or config.enforcement_rate_limits
    ):
        from graph.middleware.enforcement import EnforcementMiddleware

        sub_middleware.insert(
            0,
            EnforcementMiddleware(
                disallowed_tools=config.enforcement_disallowed_tools,
                rate_limits=config.enforcement_rate_limits,
            ),
        )

    subagent = create_agent(
        model=sub_llm,
        tools=sub_tools,
        middleware=sub_middleware,
        system_prompt=build_subagent_prompt(subagent_type),
    )

    # Tag every event the subagent emits with the parent delegation's tool-call id.
    # LangChain propagates config metadata to all child runs, so the subagent's own
    # tool frames carry `parent_task_id` — letting the console nest them under the
    # `task` card BY ID rather than by frame ordering (the delegation runs detached
    # via ensure_future, so its on_tool_end races AHEAD of these child frames).
    sub_run_config: dict[str, Any] = {"recursion_limit": sub_config.max_turns}
    if parent_task_id:
        sub_run_config["metadata"] = {"parent_task_id": parent_task_id}

    try:
        result = await subagent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=sub_run_config,
        )

        messages = result.get("messages", [])

        # The delegation's answer is the subagent's last AIMessage with content —
        # not "any message with content" (which could surface a raw tool dump) and
        # not gated on a fragile startswith("Error") text sniff (which discarded a
        # legitimate answer that opened with "Error"). Hard failures already raise
        # SubagentError below, so they never reach here.
        body = None
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                body = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

        if body is None:
            return f"[{subagent_type} completed: {description}] -- no output produced."

        if truncate is not None and len(body) > truncate:
            body = body[:truncate] + f"\n\n…[truncated to {truncate} chars]"

        return f"[{subagent_type} completed: {description}]\n\n{body}"
    except Exception as e:
        # Surface a hard subagent failure as a tool ERROR (X) rather than a green
        # "done" wrapping an "Error:" string. Callers (``task``/``task_batch``)
        # turn this into a status="error" ToolMessage or an inline batch error line.
        raise SubagentError(f"Subagent '{subagent_type}' failed: {e}") from e


async def run_manual_subagent(
    config: LangGraphConfig,
    knowledge_store=None,
    scheduler=None,
    *,
    description: str,
    prompt: str,
    subagent_type: str = "researcher",
    truncate: int | None = None,
    extra_tools=None,
) -> str:
    """Run a subagent outside the lead agent's ``task`` tool.

    The React operator console uses this to let a human explicitly fan out
    work. It intentionally uses the same private runner as ``task`` so audit,
    prompt, max-turn, and one-level delegation behavior stay aligned.

    ``extra_tools`` are tools beyond the core ``get_all_tools`` set — plugin and
    MCP tools — that the lead graph exposes via ``extra_tools`` but which this
    out-of-graph runner would otherwise miss. Without them a subagent whose
    allowlist names a plugin tool (e.g. a finance ``backtest_strategy``) sees
    "not a valid tool" and silently degrades. Mirrors the lead graph's tool set.
    """
    # Mirror the lead graph's tool set so a subagent run OUTSIDE the lead's
    # `task` tool (slash `/distill`, scheduled `/dream`, the console fan-out) sees
    # the same tools — otherwise allowlisted names like `task_create` (distill's
    # propose path) silently degrade. inbox/tasks come from STATE (not threaded
    # through every caller); goal mode from config.
    from runtime.state import STATE

    all_tools = get_all_tools(
        knowledge_store,
        scheduler=scheduler,
        inbox_store=STATE.inbox_store,
        tasks_store=STATE.tasks_store,
        goal_enabled=getattr(config, "goal_enabled", False),
        graph_config=config,
    )
    if extra_tools:
        all_tools = all_tools + list(extra_tools)
    tool_map = {t.name: t for t in all_tools}
    available_subagents = ", ".join(SUBAGENT_REGISTRY.keys()) or "(none configured)"

    return await _run_subagent(
        config=config,
        tool_map=tool_map,
        available_subagents=available_subagents,
        description=description,
        prompt=prompt,
        subagent_type=subagent_type,
        truncate=truncate,
    )


async def run_manual_subagent_batch(
    config: LangGraphConfig,
    knowledge_store=None,
    scheduler=None,
    *,
    tasks: list[dict],
    extra_tools=None,
) -> str:
    """Run independent manual subagent jobs concurrently.

    Mirrors the lead-agent ``task_batch`` tool, including stable output order
    and per-task failure isolation, but is callable from the operator API.
    """
    import asyncio

    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks must be a non-empty list")

    max_concurrency = max(1, config.subagent_max_concurrency)
    truncate = config.subagent_output_truncate
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(spec: dict) -> str:
        if not isinstance(spec, dict):
            return f"Error: each task must be an object, got {type(spec).__name__}."
        desc = spec.get("description") or "(no description)"
        prm = spec.get("prompt")
        if not prm:
            return f"Error: task '{desc}' is missing 'prompt'."
        async with sem:
            return await run_manual_subagent(
                config,
                knowledge_store=knowledge_store,
                scheduler=scheduler,
                description=desc,
                prompt=prm,
                subagent_type=spec.get("subagent_type") or spec.get("type", "researcher"),
                truncate=truncate,
                extra_tools=extra_tools,
            )

    results = await asyncio.gather(*(_one(s) for s in tasks), return_exceptions=True)

    parts = []
    for i, res in enumerate(results, start=1):
        if isinstance(res, Exception):
            res = f"Error: task #{i} raised {type(res).__name__}: {res}"
        parts.append(f"=== Task {i}/{len(results)} ===\n{res}")
    return "\n\n".join(parts)


def _build_task_tools(config: LangGraphConfig, all_tools: list[BaseTool], background_mgr=None):
    """Build the subagent-delegation tools: single ``task`` and concurrent ``task_batch``.

    Subagents share AuditMiddleware so their tool calls land alongside the
    parent agent's. The session_id contextvar set by trace_session
    propagates because subagents run in the same async context. Subagents are
    given only their allowlisted tools (which never include ``task``/
    ``task_batch``), so delegation depth is naturally bounded to one level.

    ``background_mgr`` (ADR 0050) enables ``run_in_background`` on the ``task``
    tool — a delegation the agent fires and keeps working past, surfaced back via
    a completion notification on the spawning session's next turn. ``None``
    disables it (the param falls back to synchronous execution).
    """
    import asyncio
    from typing import Literal

    from langchain_core.tools import InjectedToolCallId, tool

    tool_map = {t.name: t for t in all_tools}
    subagent_names = list(SUBAGENT_REGISTRY.keys())
    available_subagents = ", ".join(subagent_names) or "(none configured)"
    max_concurrency = max(1, config.subagent_max_concurrency)
    truncate = config.subagent_output_truncate

    # Constrain subagent_type to the live registry (plugin-contributed subagents
    # included — this runs after they're registered) so the model can't pass a name
    # that doesn't exist. A dynamic Literal renders as a JSON-schema ``enum`` the model
    # sees in the tool schema; evaluated at def time → captures the current roster,
    # rebuilt on every graph reload. ``or [...]`` keeps it valid if the registry is bare.
    _SubagentType = Literal[tuple(subagent_names or ["researcher"])]

    @tool
    async def task(
        description: str,
        prompt: str,
        subagent_type: _SubagentType = "researcher",
        run_in_background: bool = False,
        state: Annotated[Any, InjectedState] = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """Delegate a single task to a specialized subagent.

        Use this for one focused delegation. To run several independent
        delegations at once, use ``task_batch`` instead — it runs them
        concurrently rather than one after another.

        Args:
            description: Short description of what this task will accomplish
            prompt: Detailed instructions for the subagent
            subagent_type: Which subagent to use — one of the registered roster
                shown in the system prompt (e.g. researcher, strategist, …)
            run_in_background: Set True for long-running, independent work (deep
                research, multi-step gathering) you don't need to block on. The
                task runs detached as its own turn and returns IMMEDIATELY with a
                job id; you will be notified of the result automatically on a
                later turn. When you set this, do NOT poll, re-check, or spawn a
                duplicate — just continue with other work. Leave False (the
                default) when you need the result to finish the current turn.
        """

        async def _spawn_bg() -> str:
            # Resolve the originating session from injected graph state, not the
            # tracing contextvar — the contextvar reads empty in a tool body, so
            # the completion could never drain back to the spawning chat (ADR 0050).
            job_id = await background_mgr.spawn(
                origin_session=_session_id_from(state),
                subagent_type=subagent_type,
                description=description,
                prompt=prompt,
            )
            return job_id

        if run_in_background and background_mgr is not None:
            if subagent_type not in SUBAGENT_REGISTRY:
                return f"Error: Unknown subagent '{subagent_type}'. Available: {available_subagents}"
            job_id = await _spawn_bg()
            return (
                f"Background agent started: {job_id} ({subagent_type}: {description}). "
                "It is running detached; you will be notified of the result automatically "
                "on a later turn. Do NOT poll, re-check, or spawn a duplicate for this — "
                "continue with other work in the meantime."
            )

        # Auto-background (ADR 0051): a foreground delegation that overruns the budget
        # transparently detaches so it can't freeze the turn. Off unless BACKGROUND_AUTO_S>0.
        auto_s = _auto_background_seconds()
        if auto_s > 0 and background_mgr is not None and subagent_type in SUBAGENT_REGISTRY:
            inline = asyncio.ensure_future(
                _run_subagent(
                    config=config,
                    tool_map=tool_map,
                    available_subagents=available_subagents,
                    description=description,
                    prompt=prompt,
                    subagent_type=subagent_type,
                    truncate=None,
                    parent_task_id=tool_call_id,
                )
            )
            done, _pending = await asyncio.wait({inline}, timeout=auto_s)
            if inline in done:
                try:
                    return inline.result()
                except SubagentError as e:
                    return ToolMessage(
                        content=f"[{e}. Continue without its result.]",
                        tool_call_id=tool_call_id,
                        status="error",
                    )
            inline.cancel()
            try:
                await inline
            except BaseException:  # noqa: BLE001 — discard the abandoned inline run
                pass
            job_id = await _spawn_bg()
            return (
                f"This '{subagent_type}' delegation ran past the {auto_s:.0f}s inline budget, "
                f"so I moved it to the background as {job_id}. You'll be notified when it "
                "finishes — continue with other work; do NOT re-spawn it."
            )

        # Foreground delegation: a blocking `await` that would freeze the turn until
        # the subagent finishes. Wrap it in a cancellable task registered under THIS
        # tool call's id (the one the console sees on the running `task` card) so the
        # user can ABORT just this delegation (Tier 2). On a user cancel the lead
        # CONTINUES with a "cancelled" result; a parent turn-level cancel (the Stop
        # button → A2A CancelTask) still propagates and kills the whole turn.
        from graph import delegations

        session_id = _session_id_from(state)
        deleg = asyncio.ensure_future(
            _run_subagent(
                config=config,
                tool_map=tool_map,
                available_subagents=available_subagents,
                description=description,
                prompt=prompt,
                subagent_type=subagent_type,
                truncate=None,
                parent_task_id=tool_call_id,
            )
        )
        delegations.register(session_id, tool_call_id, deleg, label=description)
        try:
            return await deleg
        except asyncio.CancelledError:
            # User-initiated delegation cancel → swallow and let the lead keep going;
            # a turn-level cancel (flag unset) → re-raise so the whole turn unwinds.
            if delegations.was_cancelled(session_id, tool_call_id):
                return ToolMessage(
                    content=(
                        f"[delegation cancelled by the user before it finished: "
                        f"{subagent_type} — {description}. Continue without its result.]"
                    ),
                    tool_call_id=tool_call_id,
                    status="error",  # cancelled → the card closes as an X, not green "done"
                )
            raise
        except SubagentError as e:
            # Subagent crashed → close the delegation card as a failure (X) while still
            # handing the lead a readable result so it can continue without it.
            return ToolMessage(
                content=f"[{e}. Continue without its result.]",
                tool_call_id=tool_call_id,
                status="error",
            )
        finally:
            delegations.unregister(session_id, tool_call_id)

    @tool
    async def task_batch(
        tasks: list[dict],
        run_in_background: bool = False,
        state: Annotated[Any, InjectedState] = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """Delegate several independent tasks to subagents concurrently.

        Prefer this over multiple sequential ``task`` calls whenever the
        delegations don't depend on each other (e.g. research three topics,
        check several sources) — they run in parallel, bounded by the
        configured concurrency cap, so total latency is roughly the slowest
        task rather than the sum. Use plain ``task`` for a single delegation
        or when one task's output feeds the next.

        Args:
            tasks: A list of task specs. Each item is an object with:
                - ``description`` (str, required): short summary of the task
                - ``prompt`` (str, required): detailed instructions
                - ``subagent_type`` (str, optional): defaults to "researcher"
            run_in_background: Set True to fan the whole batch out DETACHED — every
                task spawns as its own background agent and this returns IMMEDIATELY
                with their job ids, instead of blocking until they finish. Use it for a
                wide fan-out of long / independent / quota-heavy work (research several
                topics at once) you don't need to wait on; you'll be notified as each
                finishes. When you set this, do NOT poll, re-check, or re-spawn — just
                continue. Leave False (the default) when you need the results in this
                turn. Concurrency is capped either way.

        Returns the results concatenated in the same order as ``tasks``, each
        prefixed with its 1-based index. Individual failures are reported
        inline and do not abort the batch. With ``run_in_background`` the return is
        instead the list of started job ids (results arrive later as notifications).
        """
        if not tasks:
            return "Error: task_batch called with an empty task list."
        if not isinstance(tasks, list):
            return "Error: 'tasks' must be a list of task objects."

        # Background fan-out (ADR 0050): spawn every spec detached and return immediately
        # with the job ids — the multi-task analog of task(run_in_background=True), through
        # the same BackgroundManager.spawn. Each completion drains back into this session
        # independently (one task-notification per job); the manager's concurrency cap bounds
        # how many run at once. Degrades to the foreground batch below when no manager exists.
        if run_in_background and background_mgr is not None:
            session_id = _session_id_from(state)
            lines: list[str] = []
            started = 0
            for i, spec in enumerate(tasks, start=1):
                if not isinstance(spec, dict):
                    lines.append(f"Task {i}: skipped — each task must be an object.")
                    continue
                desc = spec.get("description") or "(no description)"
                prm = spec.get("prompt")
                st = spec.get("subagent_type", "researcher")
                if not prm:
                    lines.append(f"Task {i} ({desc}): skipped — missing 'prompt'.")
                    continue
                if st not in SUBAGENT_REGISTRY:
                    lines.append(f"Task {i} ({desc}): skipped — unknown subagent '{st}'.")
                    continue
                job_id = await background_mgr.spawn(
                    origin_session=session_id,
                    subagent_type=st,
                    description=desc,
                    prompt=prm,
                )
                started += 1
                lines.append(f"Task {i}: {job_id} ({st}: {desc})")
            if not started:
                return "No background tasks started:\n" + "\n".join(lines)
            return (
                f"Started {started} background agent(s), running detached. You will be "
                "notified automatically as each finishes — do NOT poll, re-check, or "
                "re-spawn; continue with other work.\n" + "\n".join(lines)
            )

        sem = asyncio.Semaphore(max_concurrency)

        async def _one(spec: dict) -> str:
            if not isinstance(spec, dict):
                return f"Error: each task must be an object, got {type(spec).__name__}."
            desc = spec.get("description") or "(no description)"
            prm = spec.get("prompt")
            if not prm:
                return f"Error: task '{desc}' is missing 'prompt'."
            async with sem:
                try:
                    return await _run_subagent(
                        config=config,
                        tool_map=tool_map,
                        available_subagents=available_subagents,
                        description=desc,
                        prompt=prm,
                        subagent_type=spec.get("subagent_type", "researcher"),
                        truncate=truncate,
                        parent_task_id=tool_call_id,
                    )
                except SubagentError as e:
                    # One failed delegation is reported inline; the batch goes on.
                    return f"Error: {e}"

        results = await asyncio.gather(*(_one(s) for s in tasks), return_exceptions=True)

        parts = []
        for i, res in enumerate(results, start=1):
            if isinstance(res, Exception):
                res = f"Error: task #{i} raised {type(res).__name__}: {res}"
            parts.append(f"=== Task {i}/{len(results)} ===\n{res}")
        return "\n\n".join(parts)

    # Background-job control tools (ADR 0051) — only when a background manager exists
    # (so a no-background build doesn't advertise dead controls).
    bg_tools: list[BaseTool] = []
    if background_mgr is not None:

        @tool
        async def task_output(job_id: str, block: bool = True, timeout: float = 30.0) -> str:
            """Check a background job's status and result (the ``bg-…`` id from
            ``task(run_in_background=True)``).

            You normally do NOT need this — you're notified automatically when a job
            finishes. Use it only when you deliberately want to wait for or inspect a
            specific job now.

            Args:
                job_id: the ``bg-…`` job id.
                block: if True (default), wait until the job finishes or ``timeout``
                    elapses; if False, return the current state immediately.
                timeout: max seconds to wait when ``block`` is True (capped at 600).
            """
            job = background_mgr.store.get(job_id)
            if job is None:
                return f"No background job {job_id}."
            if block and job.status == "running":
                cap = max(1.0, min(float(timeout or 0), 600.0))
                waited = 0.0
                while job.status == "running" and waited < cap:
                    await asyncio.sleep(1.0)
                    waited += 1.0
                    job = background_mgr.store.get(job_id) or job
            head = f"Job {job_id} ({job.subagent_type}: {job.description}) — {job.status}"
            if job.status == "running":
                return head + " (still running)."
            return f"{head}.\n\n{job.result or '(no output)'}"

        @tool
        async def stop_task(job_id: str) -> str:
            """Stop a running background job (the ``bg-…`` id from
            ``task(run_in_background=True)``) — cancels its detached turn. Use this to
            kill a job that's stuck, runaway, or no longer needed."""
            res = await background_mgr.cancel(job_id)
            return res.get("detail", "Done.")

        bg_tools = [task_output, stop_task]

    # Declarative multi-step workflows (ADR 0002) are now an opt-in plugin
    # (plugins/workflows) — its run_workflow/save_workflow tools come in via the
    # plugin tool path, not here. Core no longer ships the workflow engine.
    tools = [task, task_batch, *bg_tools]
    return tools


def create_agent_graph(
    config: LangGraphConfig,
    knowledge_store=None,
    scheduler=None,
    skills_index=None,
    extra_tools=None,
    extra_middleware=None,
    late_tool_factories=None,
    include_subagents: bool = True,
    checkpointer=None,
    inbox_store=None,
    tasks_store=None,
    background_mgr=None,
):
    """Create the protoAgent LangGraph agent.

    ``extra_tools`` are additional LangChain tools to expose to the lead agent
    (e.g. MCP-server tools discovered at startup). Appended before subagent /
    middleware assembly so they're in the tool map and visible to the model.

    ``checkpointer`` persists conversation state per ``thread_id``: pass one so
    multi-turn chats keep their history (the agent sees prior turns instead of
    starting fresh each message). Compaction middleware summarizes the old part
    of that history near the context limit. A checkpointer set only in the
    invoke ``config`` is ignored by LangGraph — it must be bound at compile time.

    Returns a compiled graph that can be invoked with:
        graph.ainvoke({"messages": [HumanMessage(content="...")]},
                      config={"configurable": {"thread_id": "..."}})
    """
    llm = create_llm(config)

    all_tools = get_all_tools(
        knowledge_store,
        scheduler=scheduler,
        inbox_store=inbox_store,
        tasks_store=tasks_store,
        # Thread the goal flag so the agent-facing set_goal tool (ADR 0028) is
        # actually BOUND, not just advertised. Without this it defaults False and
        # set_goal silently never reaches the model (it stayed in /api/tools,
        # which passes goal_enabled explicitly — a registry-vs-binding split).
        # Subagent builds deliberately omit it: subagents are bounded by
        # max_turns and must not self-set goals.
        goal_enabled=config.goal_enabled,
        # Lets knowledge_ingest build the gateway STT/vision fns for audio/video/image.
        graph_config=config,
    )

    if extra_tools:
        all_tools.extend(extra_tools)

    if include_subagents:
        all_tools.extend(
            _build_task_tools(
                config,
                all_tools,
                background_mgr=background_mgr,
            )
        )

    # Fenced multi-project filesystem toolset (ADR 0007 — operator primitives).
    # Opt-in; inert unless filesystem.enabled + a non-empty projects registry.
    # Added before the late-tools seam / deferred so they're wrappable + discoverable.
    if config.filesystem_enabled:
        from tools.fs_tools import build_fs_tools

        all_tools.extend(build_fs_tools(config))

    # Plugin-contributed late tools (the late-tools seam) — factories that need the
    # FULLY assembled toolset (core + subagent + plugin + MCP tools). Built
    # here, before the deferred meta-tool, so a late tool can wrap or proxy any other
    # tool (but never itself) and is still surfaced by search_tools.
    # factory(all_tools, config) -> tool | list[tool] | None; a raiser is skipped.
    for _late_factory in late_tool_factories or ():
        try:
            _produced = _late_factory(all_tools, config)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("[plugins] late tool factory failed — skipped")
            continue
        if _produced:
            all_tools.extend(_produced if isinstance(_produced, list) else [_produced])

    # Deferred tools (ADR 0005 #3) — opt-in progressive disclosure. The
    # search_tools meta-tool is built over the full set (so it can surface any
    # of them) and ToolDeferralMiddleware trims the per-call schemas to base +
    # loaded. Every tool stays callable; only the model's view is trimmed.
    if config.tools_deferred_enabled:
        from tools.lg_tools import build_search_tools_tool, resolve_deferred_keep

        keep = resolve_deferred_keep(config.tools_deferred_keep)
        all_tools.append(build_search_tools_tool(all_tools, keep))

    middleware = _build_middleware(
        config, knowledge_store, skills_index=skills_index, extra_middleware=extra_middleware
    )

    system_prompt = build_system_prompt(
        include_subagents=include_subagents,
        projects=(config.effective_filesystem_projects() if config.filesystem_enabled else None),
    )

    agent = create_agent(
        model=llm,
        tools=all_tools,
        middleware=middleware,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
        # Wire the declared state schema so session_id (stamped into every turn's
        # graph input by the chat/A2A layer) is a real channel the tools can read
        # via InjectedState. Without this, create_agent runs on the default
        # messages-only state, session_id is silently dropped, and tool bodies
        # can't recover it (the tracing contextvar is invisible in a tool body) —
        # which broke wait's same-session resume (ADR 0053) and set_goal.
        state_schema=ProtoAgentState,
    )

    # Single source of truth for "what tools the model has". Stamp the final
    # assembled set on the compiled graph so the Tools tab (/api/tools) and any
    # other consumer read exactly what's BOUND, instead of re-deriving the list
    # and drifting from it (set_goal advertised-but-unbound bd-2aa; task /
    # filesystem / execute_code under-reported bd-67j).
    agent.bound_tools = list(all_tools)
    return agent


def create_simple_agent(config: LangGraphConfig, knowledge_store=None, scheduler=None):
    """Create a simple agent without subagents (for debugging/testing)."""
    from langgraph.prebuilt import create_react_agent

    llm = create_llm(config)
    all_tools = get_all_tools(knowledge_store, scheduler=scheduler, graph_config=config)

    system_prompt = build_system_prompt(include_subagents=False)

    return create_react_agent(
        model=llm,
        tools=all_tools,
        prompt=system_prompt,
    )
