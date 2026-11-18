"""View-only private-history boundary for every shared-audience model call."""

from langchain.agents.middleware import AgentMiddleware
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


class PrivateProjectionMiddleware(AgentMiddleware):
    """Private turns see the checkpoint; all other turns see only its public view."""

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
