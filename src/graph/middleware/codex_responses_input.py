"""CodexResponsesInputMiddleware — the Codex backend's input-shape rules (ADR 0097).

It also carries the lane's ``prompt_cache_key`` (#3342): cache routing is a request
shape concern on this backend, and this middleware already owns ``model_settings``
for it. See ``_cache_key`` for why the session id is the grain.

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


def _cache_key(request) -> str:
    """A stable per-thread ``prompt_cache_key``, or "" when there is no session.

    OpenAI routes a request to an inference engine by hashing its first ~256 tokens,
    and a cache hit needs the shared prefix AND the same machine. Every call from this
    agent opens with the same `instructions`, so the GENERIC head (instructions + tool
    schemas) is warm wherever it lands — but the continuation, this thread's
    conversation, exists in KV cache on only the engine that saw it. Without a sticky
    key, calls scatter and each one reuses just that head.

    Measured on protoEngineer before this: every call cached exactly 24,064 tokens and
    never one more, across 204 calls with inputs up to 1.2M (#3342).

    The session id is the right grain: it is stable for the life of a thread, so a
    turn's tool loop and the turns after it route together, and it is distinct per
    thread, so two conversations don't fight over one engine. Absent a session we send
    NOTHING — a random key per call would pin each call to its own engine, which is
    worse than no key at all.
    """
    state = getattr(request, "state", None) or {}
    try:
        return str(state.get("session_id") or "").strip()
    except AttributeError:  # not a mapping — no session to key on
        return ""


class CodexResponsesInputMiddleware(AgentMiddleware):
    """Move the system prompt into the Responses ``instructions`` field for Codex,
    and key the request for cache routing."""

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
        settings = {**(getattr(request, "model_settings", None) or {}), "instructions": text}
        key = _cache_key(request)
        if key:
            # Routing stickiness, not a cache control: it cannot make an unmatched
            # prefix hit, only keep matching requests on the engine that holds it.
            settings["prompt_cache_key"] = key
        return request.override(system_message=None, model_settings=settings)

    def wrap_model_call(self, request, handler):
        return handler(self._transform(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._transform(request))
