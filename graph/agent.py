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


def _build_middleware(config: LangGraphConfig, knowledge_store=None, skills_index=None):
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
    if config.compaction_enabled:
        from langchain.agents.middleware import SummarizationMiddleware
        summ_model = create_llm(config, model_name=config.compaction_model or None)
        middleware.append(SummarizationMiddleware(
            model=summ_model,
            trigger=_parse_compaction_trigger(config.compaction_trigger),
            keep=("messages", config.compaction_keep_messages),
        ))

    # Model routing / failover — retry on fallback models (same gateway).
    if config.routing_fallback_models:
        from langchain.agents.middleware import ModelFallbackMiddleware
        fallbacks = [create_llm(config, model_name=m) for m in config.routing_fallback_models]
        middleware.append(ModelFallbackMiddleware(*fallbacks))

    middleware.append(MessageCaptureMiddleware())

    return middleware


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
    llm,
    tool_map: dict,
    available_subagents: str,
    description: str,
    prompt: str,
    subagent_type: str,
    emit_skill: bool,
    truncate: int | None = None,
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

    subagent = create_agent(
        model=llm,
        tools=sub_tools,
        middleware=[AuditMiddleware()],
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
) -> str:
    """Run a subagent outside the lead agent's ``task`` tool.

    The React operator console uses this to let a human explicitly fan out
    work. It intentionally uses the same private runner as ``task`` so audit,
    prompt, max-turn, and one-level delegation behavior stay aligned.
    """
    llm = create_llm(config)
    all_tools = get_all_tools(knowledge_store, scheduler=scheduler)
    tool_map = {t.name: t for t in all_tools}
    available_subagents = ", ".join(SUBAGENT_REGISTRY.keys()) or "(none configured)"

    return await _run_subagent(
        llm=llm,
        tool_map=tool_map,
        available_subagents=available_subagents,
        description=description,
        prompt=prompt,
        subagent_type=subagent_type,
        emit_skill=emit_skill,
        truncate=truncate,
    )


async def run_manual_subagent_batch(
    config: LangGraphConfig,
    knowledge_store=None,
    scheduler=None,
    *,
    tasks: list[dict],
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
            )

    results = await asyncio.gather(*(_one(s) for s in tasks), return_exceptions=True)

    parts = []
    for i, res in enumerate(results, start=1):
        if isinstance(res, Exception):
            res = f"Error: task #{i} raised {type(res).__name__}: {res}"
        parts.append(f"=== Task {i}/{len(results)} ===\n{res}")
    return "\n\n".join(parts)


def _build_task_tools(config: LangGraphConfig, all_tools: list[BaseTool]):
    """Build the subagent-delegation tools: single ``task`` and concurrent ``task_batch``.

    Subagents share AuditMiddleware so their tool calls land alongside the
    parent agent's. The session_id contextvar set by trace_session
    propagates because subagents run in the same async context. Subagents are
    given only their allowlisted tools (which never include ``task``/
    ``task_batch``), so delegation depth is naturally bounded to one level.
    """
    import asyncio

    from langchain_core.tools import tool

    llm = create_llm(config)
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
            llm=llm,
            tool_map=tool_map,
            available_subagents=available_subagents,
            description=description,
            prompt=prompt,
            subagent_type=subagent_type,
            emit_skill=emit_skill,
            truncate=None,
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
                    llm=llm,
                    tool_map=tool_map,
                    available_subagents=available_subagents,
                    description=desc,
                    prompt=prm,
                    subagent_type=spec.get("subagent_type", "researcher"),
                    emit_skill=bool(spec.get("emit_skill", False)),
                    truncate=truncate,
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

    return [task, task_batch]


def create_agent_graph(
    config: LangGraphConfig,
    knowledge_store=None,
    scheduler=None,
    skills_index=None,
    include_subagents: bool = True,
):
    """Create the protoAgent LangGraph agent.

    Returns a compiled graph that can be invoked with:
        graph.ainvoke({"messages": [HumanMessage(content="...")]})
    """
    llm = create_llm(config)

    all_tools = get_all_tools(knowledge_store, scheduler=scheduler)

    if include_subagents:
        all_tools.extend(_build_task_tools(config, all_tools))

    # Programmatic tool calling — opt-in. Built last so it can wrap every
    # other tool (including task/task_batch) but never itself.
    if config.execute_code_enabled:
        from tools.execute_code import build_execute_code_tool
        all_tools.append(build_execute_code_tool(all_tools, config=config))

    middleware = _build_middleware(config, knowledge_store, skills_index=skills_index)

    system_prompt = build_system_prompt(
        include_subagents=include_subagents,
    )

    agent = create_agent(
        model=llm,
        tools=all_tools,
        middleware=middleware,
        system_prompt=system_prompt,
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
