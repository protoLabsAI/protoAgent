"""AgentState — LangGraph state schema for protoAgent.

Extends AgentState with optional session/context fields + a reducer
for captured message() tool content. Fork-specific state fields
(findings, verdicts, scope, etc.) belong in your own subclass.
"""

import operator
from typing import Annotated, NotRequired

# langchain's create_agent state base (messages + jump_to + structured_response).
# NOT langgraph's chat_agent_executor.AgentState — that carries a managed
# `remaining_steps` channel which create_agent rejects in the input schema.
from langchain.agents import AgentState


class ProtoAgentState(AgentState):
    """Base state schema for the protoAgent LangGraph agent.

    Extends create_agent's AgentState (which provides `messages` with the
    add_messages reducer). Passed as ``state_schema`` to create_agent so these
    fields are real channels readable by tools via InjectedState. Extend this
    class in your fork to add domain-specific state.
    """

    # Session tracking (A2A / chat session ID)
    session_id: NotRequired[str]

    # Incognito thread (ADR 0069 D3b): no session-memory persistence, no memory
    # injection for this thread. Set per turn by the chat entry paths (A2A
    # message metadata / POST /api/chat); read by SessionSummaryMiddleware and
    # KnowledgeMiddleware from state (current_session_id() is empty in tool
    # bodies — state is the reliable carrier).
    incognito: NotRequired[bool]

    # Per-turn model override (per chat tab) — read by ModelOverrideMiddleware to
    # swap the lead model for this turn; unset → the configured default.
    model: NotRequired[str]

    # Per-turn subagent tool fence (#1639): a detached background job running a
    # registry subagent stamps the subagent's resolved tool allowlist here (fire
    # metadata → request metadata → state); SubagentFenceMiddleware blocks any
    # tool call outside it. Unset → no fence (ordinary turns).
    subagent_fence: NotRequired[list[str]]

    # Knowledge context injected by KnowledgeMiddleware before LLM call
    context: NotRequired[str]

    # Captured message() tool content
    captured_messages: Annotated[list[str], operator.add]
