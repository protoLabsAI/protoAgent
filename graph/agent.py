"""Main LangGraph agent for protoAgent.

Builds the agent graph with middleware, tools, and subagent support.
Uses langchain's create_agent() with AgentMiddleware for the DeerFlow pattern.
"""

from langchain.agents import create_agent
from langchain_core.tools import BaseTool

from graph.config import LangGraphConfig
from graph.llm import create_llm
from graph.prompts import build_system_prompt, build_subagent_prompt
from graph.middleware.audit import AuditMiddleware
from graph.middleware.knowledge import KnowledgeMiddleware
from graph.middleware.memory import MemoryMiddleware
from graph.middleware.message_capture import MessageCaptureMiddleware
from graph.subagents.config import SUBAGENT_REGISTRY
from tools.lg_tools import get_all_tools


def _build_middleware(config: LangGraphConfig, knowledge_store=None, skills_index=None, extra_middleware=None):
    middleware = []

    # Prompt caching + knowledge-context delivery (wrap_model_call). Added
    # first/outermost so the cache breakpoint lands on the stable system
    # prefix; KnowledgeMiddleware's context is delivered just after it.
    from graph.middleware.prompt_cache import PromptCacheMiddleware
    middleware.append(PromptCacheMiddleware(
        enabled=config.prompt_cache_enabled,
        ttl=config.prompt_cache_ttl,
        force=config.prompt_cache_force,
    ))

    # Enforcement gate first (outermost) so disallowed/rate-limited tool
    # calls are blocked before any execution. Opt-in via config.
    if config.enforcement_enabled and (
        config.enforcement_disallowed_tools or config.enforcement_rate_limits
    ):
        from graph.middleware.enforcement import EnforcementMiddleware
        middleware.append(EnforcementMiddleware(
            disallowed_tools=config.enforcement_disallowed_tools,
            rate_limits=config.enforcement_rate_limits,
        ))

    # KnowledgeMiddleware also carries human-authored skill retrieval (the
    # <learned_skills> injection). Build it when knowledge OR skills is active,
    # so skills work even on a KB-less agent (the store is None-tolerant).
    _skills_index = skills_index if config.skills_enabled else None
    if (config.knowledge_middleware and knowledge_store) or _skills_index is not None:
        middleware.append(KnowledgeMiddleware(
            knowledge_store if config.knowledge_middleware else None,
            top_k=config.knowledge_top_k,
            skills_index=_skills_index,
            skills_top_k=config.skills_top_k,
        ))

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
        middleware.append(MemoryMiddleware(knowledge_store))

    if config.ingest_enabled and knowledge_store is not None:
        from graph.middleware.knowledge_ingest import KnowledgeIngestMiddleware
        middleware.append(KnowledgeIngestMiddleware(
            knowledge_store, ingest_tools=config.ingest_tools or None,
        ))

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
                "[compaction] trigger %r needs a model profile that %r lacks; "
                "falling back to messages:%d",
                config.compaction_trigger, config.model_name, fallback,
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
    for mw in (extra_middleware or []):
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


async def _run_subagent(
    *,
    config,
    tool_map: dict,
    available_subagents: str,
    description: str,
    prompt: str,
    subagent_type: str,
    emit_skill: bool,
    truncate: int | None = None,
    skills_index=None,
) -> str:
    """Run a single subagent delegation and return its output text.

    Shared by the single ``task`` tool and the concurrent ``task_batch`` tool.
    ``truncate`` (chars) bounds the returned body so a wide fan-out can't blow
    the parent context; ``None`` means unbounded (single-task path).
    """
    from datetime import datetime, timezone

    import tracing
    from graph.extensions.skills import SkillV1Artifact, emit_skill_artifact

    sub_config = SUBAGENT_REGISTRY.get(subagent_type)
    if not sub_config:
        return f"Error: Unknown subagent '{subagent_type}'. Available: {available_subagents}"

    sub_tools = [tool_map[name] for name in sub_config.tools if name in tool_map]
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
        sub_middleware.insert(0, EnforcementMiddleware(
            disallowed_tools=config.enforcement_disallowed_tools,
            rate_limits=config.enforcement_rate_limits,
        ))

    subagent = create_agent(
        model=sub_llm,
        tools=sub_tools,
        middleware=sub_middleware,
        system_prompt=build_subagent_prompt(subagent_type),
    )

    try:
        result = await subagent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config={"recursion_limit": sub_config.max_turns},
        )

        messages = result.get("messages", [])

        # Extract tools actually invoked during the subagent run.
        tools_used: list[str] = []
        for msg in messages:
            # AIMessage tool_calls: [{"name": ..., "args": ..., "id": ...}]
            for tc in getattr(msg, "tool_calls", []) or []:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                if name and name not in tools_used:
                    tools_used.append(name)

        body = None
        for msg in reversed(messages):
            if hasattr(msg, "content") and msg.content:
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if content and not content.startswith("Error"):
                    body = content
                    break

        if body is None:
            return f"[{subagent_type} completed: {description}] -- no output produced."

        if truncate is not None and len(body) > truncate:
            body = body[:truncate] + f"\n\n…[truncated to {truncate} chars]"

        output_text = f"[{subagent_type} completed: {description}]\n\n{body}"

        # Emit skill-v1 artifact when opted in and config permits.
        if emit_skill and sub_config.allow_skill_emission:
            if not tools_used:
                import logging
                logging.getLogger(__name__).warning(
                    "[skill] emit_skill=True but no tool usage metadata "
                    "captured for subagent '%s'; skipping skill emission.",
                    subagent_type,
                )
            else:
                try:
                    artifact = SkillV1Artifact(
                        name=description,
                        description=f"Captured workflow: {description}",
                        prompt_template=prompt,
                        tools_used=tools_used,
                        created_at=datetime.now(timezone.utc),
                        source_session_id=tracing.current_session_id(),
                    )
                    emit_skill_artifact(artifact)
                    # Persist to the skill index here — same async context as
                    # emission, so task_batch (which fans out into child tasks)
                    # doesn't lose artifacts the way a ContextVar drain would.
                    if skills_index is not None:
                        try:
                            skills_index.add_emitted_skill(artifact)
                        except Exception as exc:
                            import logging
                            logging.getLogger(__name__).error(
                                "[skill] persisting emitted skill failed: %s", exc
                            )
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).error(
                        "[skill] skill-v1 artifact construction failed: %s; "
                        "skipping emission.",
                        exc,
                    )

        return output_text
    except Exception as e:
        return f"Error: Subagent '{subagent_type}' failed: {e}"


async def run_manual_subagent(
    config: LangGraphConfig,
    knowledge_store=None,
    scheduler=None,
    *,
    description: str,
    prompt: str,
    subagent_type: str = "researcher",
    emit_skill: bool = False,
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
    all_tools = get_all_tools(knowledge_store, scheduler=scheduler)
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
        emit_skill=emit_skill,
        truncate=truncate,
    )


async def run_manual_workflow(
    config: LangGraphConfig,
    registry,
    *,
    knowledge_store=None,
    scheduler=None,
    name: str,
    inputs: dict | None = None,
    on_step=None,
    extra_tools=None,
) -> dict:
    """Run a saved workflow recipe outside the lead agent's tool (operator UI).

    Returns the engine result ``{"output", "steps", "failed"}``. Raises
    ``ValueError`` for an unknown / invalid recipe or missing required inputs.

    ``on_step`` (optional async callback) is invoked with
    ``{"phase": "start"|"end", "step_id", "subagent", "output"?}`` around each
    step so a caller can stream per-step progress (e.g. the chat slash command's
    tool cards). Errors in the callback never interrupt the run.
    """
    from graph.workflows.engine import execute_workflow, resolve_inputs, validate_recipe

    if registry is None:
        raise ValueError("workflows are not enabled")
    recipe = registry.get(name)
    if recipe is None:
        raise ValueError(f"no workflow named {name!r}")
    errs = validate_recipe(recipe, known_subagents=set(SUBAGENT_REGISTRY))
    if errs:
        raise ValueError("invalid workflow: " + "; ".join(errs))
    resolved, missing = resolve_inputs(recipe, inputs or {})
    if missing:
        raise ValueError(f"missing required input(s): {', '.join(missing)}")

    async def _emit(event: dict) -> None:
        if on_step is None:
            return
        try:
            await on_step(event)
        except Exception:  # noqa: BLE001 — progress is best-effort, never fatal
            pass

    async def _run_step(subagent_type: str, prompt: str, step_id: str) -> str:
        await _emit({"phase": "start", "step_id": step_id, "subagent": subagent_type})
        out = await run_manual_subagent(
            config,
            knowledge_store=knowledge_store,
            scheduler=scheduler,
            description=f"workflow {name}:{step_id}",
            prompt=prompt,
            subagent_type=subagent_type,
            extra_tools=extra_tools,
        )
        await _emit({"phase": "end", "step_id": step_id, "subagent": subagent_type, "output": out})
        return out

    return await execute_workflow(
        recipe, resolved, run_step=_run_step, max_concurrency=config.subagent_max_concurrency,
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
                emit_skill=bool(spec.get("emit_skill", False)),
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


def _build_task_tools(config: LangGraphConfig, all_tools: list[BaseTool], skills_index=None, workflow_registry=None):
    """Build the subagent-delegation tools: single ``task`` and concurrent ``task_batch``.

    Subagents share AuditMiddleware so their tool calls land alongside the
    parent agent's. The session_id contextvar set by trace_session
    propagates because subagents run in the same async context. Subagents are
    given only their allowlisted tools (which never include ``task``/
    ``task_batch``), so delegation depth is naturally bounded to one level.
    """
    import asyncio

    from langchain_core.tools import tool

    tool_map = {t.name: t for t in all_tools}
    available_subagents = ", ".join(SUBAGENT_REGISTRY.keys()) or "(none configured)"
    max_concurrency = max(1, config.subagent_max_concurrency)
    truncate = config.subagent_output_truncate

    @tool
    async def task(
        description: str,
        prompt: str,
        subagent_type: str = "researcher",
        emit_skill: bool = False,
    ) -> str:
        """Delegate a single task to a specialized subagent.

        Use this for one focused delegation. To run several independent
        delegations at once, use ``task_batch`` instead — it runs them
        concurrently rather than one after another.

        Args:
            description: Short description of what this task will accomplish
            prompt: Detailed instructions for the subagent
            subagent_type: Which subagent to use (see SUBAGENT_REGISTRY)
            emit_skill: When True and the subagent config permits it, capture
                the workflow as a skill-v1 artifact on successful completion.
                Defaults to False (opt-in). No artifact is emitted on failure
                or when the subagent config has allow_skill_emission=False.
        """
        return await _run_subagent(
            config=config,
            tool_map=tool_map,
            available_subagents=available_subagents,
            description=description,
            prompt=prompt,
            subagent_type=subagent_type,
            emit_skill=emit_skill,
            truncate=None,
            skills_index=skills_index,
        )

    @tool
    async def task_batch(tasks: list[dict]) -> str:
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
                - ``emit_skill`` (bool, optional): defaults to False

        Returns the results concatenated in the same order as ``tasks``, each
        prefixed with its 1-based index. Individual failures are reported
        inline and do not abort the batch.
        """
        if not tasks:
            return "Error: task_batch called with an empty task list."
        if not isinstance(tasks, list):
            return "Error: 'tasks' must be a list of task objects."

        sem = asyncio.Semaphore(max_concurrency)

        async def _one(spec: dict) -> str:
            if not isinstance(spec, dict):
                return f"Error: each task must be an object, got {type(spec).__name__}."
            desc = spec.get("description") or "(no description)"
            prm = spec.get("prompt")
            if not prm:
                return f"Error: task '{desc}' is missing 'prompt'."
            async with sem:
                return await _run_subagent(
                    config=config,
                    tool_map=tool_map,
                    available_subagents=available_subagents,
                    description=desc,
                    prompt=prm,
                    subagent_type=spec.get("subagent_type", "researcher"),
                    emit_skill=bool(spec.get("emit_skill", False)),
                    truncate=truncate,
                    skills_index=skills_index,
                )

        results = await asyncio.gather(
            *(_one(s) for s in tasks), return_exceptions=True
        )

        parts = []
        for i, res in enumerate(results, start=1):
            if isinstance(res, Exception):
                res = f"Error: task #{i} raised {type(res).__name__}: {res}"
            parts.append(f"=== Task {i}/{len(results)} ===\n{res}")
        return "\n\n".join(parts)

    tools = [task, task_batch]

    # Reusable declarative workflows (ADR 0002) — run a saved multi-step recipe
    # over subagents. Only the lead agent gets this; subagents don't, so
    # workflows can't recurse.
    if getattr(config, "workflows_enabled", False) and workflow_registry is not None:
        from graph.workflows.engine import execute_workflow, resolve_inputs, validate_recipe

        @tool
        async def run_workflow(name: str = "", inputs: dict | None = None) -> str:
            """Run a saved multi-step workflow recipe over subagents.

            Workflows chain subagent steps (some in parallel), threading each
            step's output into the next — for repeatable jobs like
            research→synthesize→write. Pass an empty ``name`` to list the
            available workflows and their inputs.

            Args:
                name: The workflow name (see the list with an empty name).
                inputs: Mapping of the workflow's declared inputs to values.
            """
            if not name.strip():
                summaries = workflow_registry.list()
                if not summaries:
                    return "No workflows are available."
                lines = ["Available workflows:"]
                for s in summaries:
                    req = [i["name"] for i in s["inputs"] if i["required"]]
                    lines.append(f"- {s['name']}: {s['description']} (inputs: {', '.join(req) or 'none required'})")
                return "\n".join(lines)
            recipe = workflow_registry.get(name)
            if recipe is None:
                return f"No workflow named {name!r}. Available: {', '.join(workflow_registry.names()) or '(none)'}."
            errs = validate_recipe(recipe, known_subagents=set(SUBAGENT_REGISTRY))
            if errs:
                return f"Workflow {name!r} is invalid: " + "; ".join(errs)
            resolved, missing = resolve_inputs(recipe, inputs or {})
            if missing:
                return f"Workflow {name!r} needs input(s): {', '.join(missing)}."

            async def _run_step(subagent_type: str, prompt: str, step_id: str) -> str:
                return await _run_subagent(
                    config=config,
                    tool_map=tool_map,
                    available_subagents=available_subagents,
                    description=f"workflow {name}:{step_id}",
                    prompt=prompt,
                    subagent_type=subagent_type,
                    emit_skill=False,
                    truncate=truncate,
                    skills_index=skills_index,
                )

            result = await execute_workflow(recipe, resolved, run_step=_run_step, max_concurrency=max_concurrency)
            return result["output"]

        tools.append(run_workflow)

        @tool
        async def save_workflow(
            name: str,
            description: str,
            steps: list[dict],
            inputs: list[dict] | None = None,
            output: str = "",
        ) -> str:
            """Save a reusable multi-step workflow so it can be re-run later with
            run_workflow — capture a multi-step subagent process you just worked
            out (the closed loop). Saving overwrites any existing workflow of the
            same name.

            Args:
                name: Unique slug for the workflow.
                description: One-line summary of what it does.
                steps: Ordered list of step objects, each with ``id`` (str),
                    ``subagent`` (a configured subagent type), ``prompt`` (str,
                    may reference {{inputs.x}} and {{steps.<id>.output}}), and
                    optional ``depends_on`` (list of earlier step ids that run
                    first — independent steps run in parallel).
                inputs: Optional list of {name, required?, default?} the workflow
                    accepts (referenced as {{inputs.name}} in prompts).
                output: Optional final-output template (default = last step's output).
            """
            recipe: dict = {"name": name, "description": description, "version": 1, "steps": steps}
            if inputs:
                recipe["inputs"] = inputs
            if output:
                recipe["output"] = output
            errs = validate_recipe(recipe, known_subagents=set(SUBAGENT_REGISTRY))
            if errs:
                return "Cannot save — the workflow is invalid: " + "; ".join(errs)
            try:
                path = workflow_registry.save(recipe)
            except Exception as exc:  # noqa: BLE001 — readable tool error
                return f"Error saving workflow: {exc}"
            return (
                f"Saved workflow {name!r} ({len(steps)} step(s)) to {path}. "
                f"Run it with run_workflow({name!r}, ...)."
            )

        tools.append(save_workflow)

    return tools


def create_agent_graph(
    config: LangGraphConfig,
    knowledge_store=None,
    scheduler=None,
    skills_index=None,
    extra_tools=None,
    extra_middleware=None,
    include_subagents: bool = True,
    checkpointer=None,
    workflow_registry=None,
    inbox_store=None,
    beads_store=None,
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
        knowledge_store, scheduler=scheduler, inbox_store=inbox_store, beads_store=beads_store,
    )

    if extra_tools:
        all_tools.extend(extra_tools)

    if include_subagents:
        all_tools.extend(_build_task_tools(
            config, all_tools, skills_index=skills_index, workflow_registry=workflow_registry,
        ))

    # Fenced multi-project filesystem toolset (ADR 0007 — operator primitives).
    # Opt-in; inert unless filesystem.enabled + a non-empty projects registry.
    # Added before execute_code/deferred so they're wrappable + discoverable.
    if config.filesystem_enabled:
        from tools.fs_tools import build_fs_tools
        all_tools.extend(build_fs_tools(config))

    # Programmatic tool calling — opt-in. Built last so it can wrap every
    # other tool (including task/task_batch) but never itself.
    if config.execute_code_enabled:
        from tools.execute_code import build_execute_code_tool
        all_tools.append(build_execute_code_tool(all_tools, config=config))

    # Deferred tools (ADR 0005 #3) — opt-in progressive disclosure. The
    # search_tools meta-tool is built over the full set (so it can surface any
    # of them) and ToolDeferralMiddleware trims the per-call schemas to base +
    # loaded. Every tool stays callable; only the model's view is trimmed.
    if config.tools_deferred_enabled:
        from tools.lg_tools import build_search_tools_tool, resolve_deferred_keep
        keep = resolve_deferred_keep(config.tools_deferred_keep)
        all_tools.append(build_search_tools_tool(all_tools, keep))

    middleware = _build_middleware(config, knowledge_store, skills_index=skills_index, extra_middleware=extra_middleware)

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
    )

    return agent


def create_simple_agent(config: LangGraphConfig, knowledge_store=None, scheduler=None):
    """Create a simple agent without subagents (for debugging/testing)."""
    from langgraph.prebuilt import create_react_agent

    llm = create_llm(config)
    all_tools = get_all_tools(knowledge_store, scheduler=scheduler)

    system_prompt = build_system_prompt(include_subagents=False)

    return create_react_agent(
        model=llm,
        tools=all_tools,
        prompt=system_prompt,
    )
