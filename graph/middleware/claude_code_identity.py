"""ClaudeCodeIdentityMiddleware — the OAuth system-prompt requirement (ADR 0097).

Anthropic's OAuth infrastructure only routes subscription (Claude Code) traffic when
the system prompt's FIRST block is the Claude Code identity line. Without it, OAuth
requests intermittently 500 / are refused. This middleware guarantees that line leads
the system prompt for the ``anthropic-oauth`` provider, and is a hard no-op for every
other provider.

It is added INNERMOST (last in the middleware list → transforms the request last, per
``langchain`` compose order), so it has the final say on ``system_message`` regardless
of what PromptCache or context injection did — and it is idempotent, so re-running never
stacks the prefix.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

from graph.providers.anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX


def _leads_with_prefix(text: str) -> bool:
    return text.lstrip().startswith(CLAUDE_CODE_SYSTEM_PREFIX)


class ClaudeCodeIdentityMiddleware(AgentMiddleware):
    """Ensure the Claude Code identity line is the first system block for OAuth."""

    def _transform(self, request):
        sysmsg = getattr(request, "system_message", None)
        if sysmsg is None:
            # No system prompt at all — deliver the identity line as the whole system.
            return request  # create_agent always supplies one; nothing safe to attach to
        content = getattr(sysmsg, "content", None)

        if isinstance(content, str):
            if _leads_with_prefix(content):
                return request
            new = sysmsg.model_copy(update={"content": f"{CLAUDE_CODE_SYSTEM_PREFIX}\n\n{content}"})
            return request.override(system_message=new)

        if isinstance(content, list) and content:
            first = content[0]
            first_text = first.get("text", "") if isinstance(first, dict) else str(first)
            if _leads_with_prefix(first_text):
                return request
            prefix_block = {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}
            new = sysmsg.model_copy(update={"content": [prefix_block, *content]})
            return request.override(system_message=new)

        return request

    def wrap_model_call(self, request, handler):
        return handler(self._transform(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._transform(request))
