"""Select native-provider wire shaping from the model on the current call."""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

from graph.providers.identity import model_provider_type


class ProviderShapeMiddleware(AgentMiddleware):
    """Apply exactly one provider transform, based on ``request.model``.

    This middleware is inside model override and fallback middleware.  It is
    therefore re-entered with the replacement model on every attempt and cannot
    leak the graph's default provider shape into another provider's request.
    """

    def _transform(self, request):
        provider_type = model_provider_type(getattr(request, "model", None))
        if provider_type == "anthropic-oauth":
            # Lazy imports preserve the gateway-only path's optional-dependency
            # boundary: merely compiling a graph must not load OAuth providers.
            from graph.middleware.claude_code_identity import ClaudeCodeIdentityMiddleware

            return ClaudeCodeIdentityMiddleware()._transform(request)
        if provider_type == "openai-codex":
            from graph.middleware.codex_responses_input import CodexResponsesInputMiddleware

            return CodexResponsesInputMiddleware()._transform(request)
        return request

    def wrap_model_call(self, request, handler):
        return handler(self._transform(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._transform(request))
