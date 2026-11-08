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


def _take_last(_current: bool, incoming: bool) -> bool:
    """Reducer for ``operator_chat_turn``: the latest write wins, so every turn (and
    resume) overwrites the previous one exactly as a plain LastValue channel would.

    The reducer exists only so that two *identical* writes arriving in one superstep —
    a resume that drains several parallel HITL interrupts feeds the ``Command.update``
    stamp into each pending branch — fold instead of raising ``InvalidUpdateError`` the
    way a bare LastValue channel does on more-than-one-value-per-step. Authority is
    never widened: the drivers derive the single bool from one authenticated origin, so
    the folded writes carry the same value.
    """
    return incoming


class ProtoAgentState(AgentState):
    """Base state schema for the protoAgent LangGraph agent.

    Extends create_agent's AgentState (which provides `messages` with the
    add_messages reducer). Passed as ``state_schema`` to create_agent so these
    fields are real channels readable by tools via InjectedState. Extend this
    class in your fork to add domain-specific state.
    """

    # Session tracking (A2A / chat session ID)
    session_id: NotRequired[str]

    # Server-stamped on every chat turn, including resumes; absent in subagents.
    # Carries authenticated operator origin into tool execution (ADR 0127 D6). The
    # ``_take_last`` reducer keeps it a last-write-wins flag while tolerating the
    # duplicate concurrent writes a resume draining parallel HITL interrupts produces
    # (a bare LastValue channel raises InvalidUpdateError on those).
    operator_chat_turn: Annotated[bool, _take_last]

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

    # Whether this turn's client can display rendered UI — artifacts, components (#4144).
    # Stamped every turn by the chat entry paths from the turn's origin: False for a plain
    # API client (``/v1``, ``/api/chat``), True for the console / A2A stream. Read by tools
    # from injected state (``current_session_id()``-style contextvars are empty in tool
    # bodies); absent → True, the behaviour before the flag existed.
    renders_ui: NotRequired[bool]

    # Captured message() tool content
    captured_messages: Annotated[list[str], operator.add]
