"""AgentState — LangGraph state schema for protoAgent.

Extends AgentState with optional session/context fields + a reducer
for captured message() tool content. Fork-specific state fields
(findings, verdicts, scope, etc.) belong in your own subclass.
"""

import operator
from typing import Annotated, NotRequired

from langgraph.prebuilt.chat_agent_executor import AgentState


class ProtoAgentState(AgentState):
    """Base state schema for the protoAgent LangGraph agent.

    Extends AgentState (which provides `messages` with add_messages reducer).
    Extend this class in your fork to add domain-specific state.
    """

    # Session tracking (A2A / chat session ID)
    session_id: NotRequired[str]

    # Knowledge context injected by KnowledgeMiddleware before LLM call
    context: NotRequired[str]

    # Captured message() tool content
    captured_messages: Annotated[list[str], operator.add]
