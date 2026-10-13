"""WirePromptCaptureMiddleware — observe what a model call ACTUALLY carries (#2527).

``PromptCaptureMiddleware`` records the composed system prompt, but it sits
outside the provider-shape transforms (``ClaudeCodeIdentityMiddleware`` prepends
the OAuth identity line; ``CodexResponsesInputMiddleware`` moves the whole
prompt into the Responses ``instructions`` model setting) — so "View prompt"
could show a prompt the wire never carried. That is exactly how #2519 (every
Codex turn sent with NO system prompt) stayed invisible for a full release.

This middleware sits INNERMOST — appended after every system-touching transform
— and stashes the request's effective wire text in a context-local slot;
``PromptCaptureMiddleware`` (outer; it captures after the handler returns, i.e.
after this ran) pops it and records it beside the composed prompt when the two
differ.

Deliberately IGNORED: kwargs bound onto the model object (``model.bind(...)``).
Bindings do not survive the agent factory's tool re-bind (#2519's root cause),
so text found only there is precisely the failure this observer exists to
expose — counting it as delivered would reproduce the original lie.

Read-only: never mutates the request; a stash failure never touches the turn.
"""

from __future__ import annotations

import contextvars

from langchain.agents.middleware import AgentMiddleware

# Task-local handoff to PromptCaptureMiddleware: the inner wrap sets it before
# the model call, the outer capture pops it after the handler returns — same
# call stack, so concurrent calls in other tasks can't cross wires.
_WIRE_SYSTEM: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "protoagent_wire_system", default=None
)


def pop_wire_system() -> str | None:
    """Read-and-clear the stashed wire text (PromptCapture's side of the handoff).

    ``None`` means no wire observer ran under this call (e.g. the subagent
    stack, where PromptCapture itself is already innermost)."""
    value = _WIRE_SYSTEM.get()
    _WIRE_SYSTEM.set(None)
    return value


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _wire_text(request) -> str:
    """The system-side text this request delivers: the Responses
    ``instructions`` model setting when a transform moved it there
    (openai-codex), else the (possibly transformed) system message. Empty means
    the call carries no system text at all — the #2519 alarm case."""
    settings = getattr(request, "model_settings", None) or {}
    instructions = settings.get("instructions")
    if isinstance(instructions, str) and instructions:
        return instructions
    sysmsg = getattr(request, "system_message", None)
    return _flatten(getattr(sysmsg, "content", None)) if sysmsg is not None else ""


class WirePromptCaptureMiddleware(AgentMiddleware):
    def _stash(self, request) -> None:
        try:
            _WIRE_SYSTEM.set(_wire_text(request))
        except Exception:  # noqa: BLE001 — observation must never touch the turn
            _WIRE_SYSTEM.set(None)

    def wrap_model_call(self, request, handler):
        self._stash(request)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        self._stash(request)
        return await handler(request)
