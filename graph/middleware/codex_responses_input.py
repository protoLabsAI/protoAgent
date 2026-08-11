"""CodexResponsesInputMiddleware — the Codex backend's input-shape rules (ADR 0097).

The ChatGPT/Codex Responses backend rejects a ``system``-role input item
("System messages are not allowed") — the system prompt must ride the top-level
``instructions`` field instead. langchain-openai maps a ``SystemMessage`` to a
system-role input item, so for the ``openai-codex`` provider we intercept the call,
move the (possibly block-structured) system text into ``instructions`` via
``model_settings`` (which the agent factory spreads into every bind), and drop the
system message from the input.

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
        # Deliver instructions via model_settings, NEVER a model binding (#2519): the
        # agent factory re-binds tools from the RAW model (RunnableBinding.__getattr__
        # resolves bind_tools on .bound), which silently discarded a bound kwarg — so
        # every tool-bearing Codex call went out with NO system prompt at all, while
        # PromptCapture (upstream of this transform) still showed the full prompt.
        # model_settings is spread into every factory bind/bind_tools branch, and the
        # Responses payload builder passes `instructions` through as the top-level field.
        return request.override(
            system_message=None,
            model_settings={**(getattr(request, "model_settings", None) or {}), "instructions": text},
        )

    def wrap_model_call(self, request, handler):
        return handler(self._transform(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._transform(request))
