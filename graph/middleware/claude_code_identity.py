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

from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX


def _split_leading_prefix(text: str) -> str | None:
    """The remainder of ``text`` after a leading identity line, or None if absent.

    Handles the shape this middleware's own older string branch produced
    (``"{prefix}\\n\\n{rest}"``) so an already-"prefixed" prompt is REPAIRED into
    the exact-block shape rather than skipped as done — skipping it is exactly
    what kept the old merged shape failing forever.
    """
    stripped = text.lstrip()
    if not stripped.startswith(CLAUDE_CODE_SYSTEM_PREFIX):
        return None
    return stripped[len(CLAUDE_CODE_SYSTEM_PREFIX) :].lstrip("\n")


class ClaudeCodeIdentityMiddleware(AgentMiddleware):
    """Ensure the identity line is its own exact first system block for OAuth."""

    def _transform(self, request):
        sysmsg = getattr(request, "system_message", None)
        if sysmsg is None:
            # No system prompt at all — deliver the identity line as the whole system.
            return request  # create_agent always supplies one; nothing safe to attach to
        content = getattr(sysmsg, "content", None)
        prefix_block = {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}

        if isinstance(content, str):
            # A string system prompt ALWAYS becomes a block list — the enforcement
            # matches on the first block exactly, so there is no valid single-block
            # or merged-string shape. A string already carrying the line is split
            # back apart, not left alone.
            rest = _split_leading_prefix(content)
            body = content if rest is None else rest
            blocks = [prefix_block] + ([{"type": "text", "text": body}] if body else [])
            new = sysmsg.model_copy(update={"content": blocks})
            return request.override(system_message=new)

        if isinstance(content, list) and content:
            first = content[0]
            first_text = first.get("text", "") if isinstance(first, dict) else str(first)
            if first_text == CLAUDE_CODE_SYSTEM_PREFIX:
                return request  # already the exact shape — idempotent no-op
            rest = _split_leading_prefix(first_text) if isinstance(first, dict) else None
            if rest is not None:
                # First block starts with the line but carries more — split it so the
                # line stands alone (keep the original block's other keys, e.g.
                # cache_control, on the REMAINDER, not the identity line: a stable
                # one-line block is a pointless cache anchor).
                rest_block = {**first, "text": rest}
                new_content = [prefix_block, rest_block, *content[1:]] if rest else [prefix_block, *content[1:]]
                new = sysmsg.model_copy(update={"content": new_content})
                return request.override(system_message=new)
            new = sysmsg.model_copy(update={"content": [prefix_block, *content]})
            return request.override(system_message=new)

        return request

    def wrap_model_call(self, request, handler):
        return handler(self._transform(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._transform(request))
