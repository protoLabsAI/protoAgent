"""CodexResponsesInputMiddleware — the Codex backend's input-shape rules (ADR 0097).

The ChatGPT/Codex Responses backend rejects a ``system``-role input item
("System messages are not allowed") — the system prompt must ride the top-level
``instructions`` field instead. langchain-openai maps a ``SystemMessage`` to a
system-role input item, so for the ``openai-codex`` provider we intercept the call,
move the (possibly block-structured) system text into ``instructions`` via a bound
kwarg, and drop the system message from the input.

Added only for ``openai-codex`` and innermost (last word on the request, after
PromptCache / context injection), so it sees the final assembled system prompt.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware


def _system_text(content) -> str:
    """Flatten a system message's content (string or Responses/Anthropic block list)
    into plain instructions text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        return "\n\n".join(p for p in parts if p)
    return ""


class CodexResponsesInputMiddleware(AgentMiddleware):
    """Move the system prompt into the Responses ``instructions`` field for Codex."""

    def _transform(self, request):
        model = getattr(request, "model", None)
        sysmsg = getattr(request, "system_message", None)
        if model is None or sysmsg is None:
            return request
        text = _system_text(getattr(sysmsg, "content", None))
        if not text:
            return request
        # Bind instructions as a per-call kwarg (flows into the responses payload)
        # and clear the system message so no system-role input item is emitted.
        bound = model.bind(instructions=text)
        return request.override(model=bound, system_message=None)

    def wrap_model_call(self, request, handler):
        return handler(self._transform(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._transform(request))
