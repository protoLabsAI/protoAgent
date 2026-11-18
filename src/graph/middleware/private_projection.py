"""View-only private-history boundary for every shared-audience model call."""

from typing import Annotated, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from graph import private_sessions
from graph.fence_scope import in_private_segment
from runtime.state import STATE


def _placeholder():
    # A dedicated tag survives KnowledgeMiddleware's stale-context-frame cleanup.
    return HumanMessage(
        content="[an earlier private exchange was omitted]",
        additional_kwargs={"protoagent_private_omission": True},
    )


class PrivateProjectionState(AgentState):
    # Checkpoint the ID so an interrupted turn resumes with the same boundary.
    # PrivateStateAttr prevents callers from supplying it as graph input.
    protoagent_private_projection_start: NotRequired[Annotated[str | None, PrivateStateAttr]]


class PrivateProjectionMiddleware(AgentMiddleware):
    """Private turns see the checkpoint; all other turns see only its public view."""

    state_schema = PrivateProjectionState

    def before_agent(self, state, runtime):  # type: ignore[override]
        # Capture before SteeringMiddleware.before_model appends interjections.
        # Reset on every new invocation, including turns with the flag disabled.
        start = next((m.id for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
        return {"protoagent_private_projection_start": start}

    async def abefore_agent(self, state, runtime):  # type: ignore[override]
        return self.before_agent(state, runtime)

    @staticmethod
    def _project(request):
        if not getattr(STATE.graph_config, "private_steering_enabled", False):
            return request
        if in_private_segment() or request.state.get("segment_audience") == "private":
            return request

        messages = request.messages
        latest = next(
            (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)), len(messages)
        )
        start = request.state.get("protoagent_private_projection_start")
        if start is not None:
            # Resolve by ID, not position: compaction can replace earlier history.
            # Old checkpoints/direct wrapper callers may have no saved boundary.
            latest = next((i for i, message in enumerate(messages) if message.id == start), latest)
        try:
            public = {id(m) for m in private_sessions.public_part(messages, request.state.get("session_id"))}
        except (private_sessions.OwnershipUnknown, private_sessions.UnlabelledPrivateTurns):
            systems = [m for m in messages[:latest] if isinstance(m, SystemMessage)]
            return request.override(messages=[*systems, _placeholder(), *messages[latest:]])

        keep = [i >= latest or id(m) in public for i, m in enumerate(messages)]
        if all(keep):
            return request
        # Fully labelled histories may split a tool round across stamps. Remove the
        # entire historical round if any member is withheld, never synthesize a result.
        for i, message in enumerate(messages[:latest]):
            if isinstance(message, AIMessage) and message.tool_calls:
                calls = {call["id"] for call in message.tool_calls}
                results = [
                    j
                    for j in range(i + 1, latest)
                    if isinstance(messages[j], ToolMessage) and messages[j].tool_call_id in calls
                ]
                if (
                    not keep[i]
                    or any(not keep[j] for j in results)
                    or calls != {messages[j].tool_call_id for j in results}
                ):
                    for j in [i, *results]:
                        keep[j] = False
        retained_calls = {
            call["id"] for i, m in enumerate(messages) if keep[i] and isinstance(m, AIMessage) for call in m.tool_calls
        }
        for i, message in enumerate(messages[:latest]):
            if isinstance(message, ToolMessage) and message.tool_call_id not in retained_calls:
                keep[i] = False

        projected = []
        omitted = False
        for message, retained in zip(messages, keep):
            if retained:
                projected.append(message)
            elif not omitted:
                projected.append(_placeholder())
            omitted = not retained
        return request.override(messages=projected)

    def wrap_model_call(self, request, handler):  # type: ignore[override]
        return handler(self._project(request))

    async def awrap_model_call(self, request, handler):  # type: ignore[override]
        return await handler(self._project(request))
