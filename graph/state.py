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

    # Per-turn model override (per chat tab) — read by ModelOverrideMiddleware to
    # swap the lead model for this turn; unset → the configured default.
    model: NotRequired[str]

    # Knowledge context injected by KnowledgeMiddleware before LLM call
    context: NotRequired[str]

    # Captured message() tool content
    captured_messages: Annotated[list[str], operator.add]
