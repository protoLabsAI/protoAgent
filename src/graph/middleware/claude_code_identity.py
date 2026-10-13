"""ClaudeCodeIdentityMiddleware — the OAuth system-prompt requirement (ADR 0097).

Anthropic's OAuth infrastructure only routes subscription (Claude Code) traffic when
the system prompt's FIRST block is EXACTLY the Claude Code identity line — its own
block, byte-equal, nothing appended. A first block that merely *starts with* the
line (the old merged-string shape this middleware used to emit) is refused with a
generic 429 ``rate_limit_error`` whose body is just ``"Error"`` — no rate-limit
headers, quota untouched — indistinguishable from a real rate limit until you A/B
the wire shape (verified live 2026-08-16, #2763: same token, same model —
``[{exact line}, {persona}]`` → 200, ``"{line}\\n\\n{persona}"`` as one block → 429).
This middleware guarantees that exact-first-block shape for the ``anthropic-oauth``
provider, and is a hard no-op for every other provider.

It is added INNERMOST (last in the middleware list → transforms the request last, per
``langchain`` compose order), so it has the final say on ``system_message`` regardless
of what PromptCache or context injection did — and it is idempotent, so re-running never
stacks the prefix.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

from graph.providers.anthropic_oauth import shape_oauth_system


class ClaudeCodeIdentityMiddleware(AgentMiddleware):
    """Ensure the identity line is its own exact first system block for OAuth.

    The shape itself lives in :func:`graph.providers.anthropic_oauth.shape_oauth_system`,
    which the OAuth client ALSO applies to every outgoing request body — so a model call
    that never passes through this middleware (summarization, titles, distill) is still
    shaped. Applying it here too keeps the shaped prompt visible to prompt capture.
    """

    def _transform(self, request):
        sysmsg = getattr(request, "system_message", None)
        if sysmsg is None:
            return request  # the OAuth client adds the identity block to the wire body
        content = getattr(sysmsg, "content", None)
        if not isinstance(content, (str, list)):
            return request
        blocks = shape_oauth_system(content)
        if blocks == content:
            return request  # already the exact shape — idempotent no-op
        new = sysmsg.model_copy(update={"content": blocks})
        return request.override(system_message=new)

    def wrap_model_call(self, request, handler):
        return handler(self._transform(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._transform(request))
