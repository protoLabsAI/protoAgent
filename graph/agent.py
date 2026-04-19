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


def _build_middleware(config: LangGraphConfig, knowledge_store=None):
    middleware = []

    if config.knowledge_middleware and knowledge_store:
        middleware.append(KnowledgeMiddleware(
            knowledge_store, top_k=config.knowledge_top_k,
        ))

    if config.audit_middleware:
        middleware.append(AuditMiddleware())

    if config.memory_middleware and knowledge_store:
        middleware.append(MemoryMiddleware(knowledge_store))

    middleware.append(MessageCaptureMiddleware())

    return middleware


def _build_task_tool(config: LangGraphConfig, all_tools: list[BaseTool]):
    """Build the task tool for subagent delegation.

    Subagents share AuditMiddleware so their tool calls land alongside the
    parent agent's. The session_id contextvar set by trace_session
    propagates because subagents run in the same async context.
    """
    from langchain_core.tools import tool

    llm = create_llm(config)
    tool_map = {t.name: t for t in all_tools}
    available_subagents = ", ".join(SUBAGENT_REGISTRY.keys()) or "(none configured)"

    @tool
    async def task(
        description: str,
        prompt: str,
        subagent_type: str = "worker",
        emit_skill: bool = False,
    ) -> str:
        """Delegate a task to a specialized subagent.

        Args:
            description: Short description of what this task will accomplish
            prompt: Detailed instructions for the subagent
            subagent_type: Which subagent to use (see SUBAGENT_REGISTRY)
            emit_skill: When True and the subagent config permits it, capture
                the workflow as a skill-v1 artifact on successful completion.
                Defaults to False (opt-in). No artifact is emitted on failure
                or when the subagent config has allow_skill_emission=False.
        """
        from datetime import datetime, timezone

        import tracing
        from graph.extensions.skills import SkillV1Artifact, emit_skill_artifact

        sub_config = SUBAGENT_REGISTRY.get(subagent_type)
        if not sub_config:
            return f"Error: Unknown subagent '{subagent_type}'. Available: {available_subagents}"

        sub_tools = [
            tool_map[name] for name in sub_config.tools
            if name in tool_map
        ]

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

            output_text = None
            for msg in reversed(messages):
                if hasattr(msg, "content") and msg.content:
                    content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    if content and not content.startswith("Error"):
                        output_text = f"[{subagent_type} completed: {description}]\n\n{content}"
                        break

            if output_text is None:
                output_text = f"[{subagent_type} completed: {description}] -- no output produced."

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

    return task


def create_agent_graph(
    config: LangGraphConfig,
    knowledge_store=None,
    include_subagents: bool = True,
):
    """Create the protoAgent LangGraph agent.

    Returns a compiled graph that can be invoked with:
        graph.ainvoke({"messages": [HumanMessage(content="...")]})
    """
    llm = create_llm(config)

    all_tools = get_all_tools(knowledge_store)

    if include_subagents:
        task_tool = _build_task_tool(config, all_tools)
        all_tools.append(task_tool)

    middleware = _build_middleware(config, knowledge_store)

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


def create_simple_agent(config: LangGraphConfig, knowledge_store=None):
    """Create a simple agent without subagents (for debugging/testing)."""
    from langgraph.prebuilt import create_react_agent

    llm = create_llm(config)
    all_tools = get_all_tools(knowledge_store)

    system_prompt = build_system_prompt(include_subagents=False)

    return create_react_agent(
        model=llm,
        tools=all_tools,
        prompt=system_prompt,
    )
