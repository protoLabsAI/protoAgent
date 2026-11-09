"""Per-delegation generation-profile phase state (ADR 0128 S2).

S2 runs only the explore phase. S4 will connect guard switch points to
``_request_for_phase``; merely declaring a final profile never switches a call.
"""

from langchain.agents.middleware import AgentMiddleware

from graph.generation_profile import ResolvedProfile


class ProfilePhaseMiddleware(AgentMiddleware):
    """Hold the lane's profiles and clients without changing explore requests."""

    def __init__(
        self, config, *, model_name: str | None, explore_profile: ResolvedProfile,
        explore_name: str | None, explore_client, final_profile: ResolvedProfile | None = None,
    ):
        super().__init__()
        self._config = config
        self.explore_profile = explore_profile
        self.final_profile = final_profile
        self._cache = {(model_name, explore_name): explore_client}

    def _llm_for(self, model_name: str | None, profile_name: str | None, profile: ResolvedProfile):
        key = (model_name, profile_name)
        if key not in self._cache:
            from graph.llm import create_llm

            self._cache[key] = create_llm(self._config, model_name=model_name, profile=profile)
        return self._cache[key]

    def _request_for_phase(self, request):
        """S4 final-phase switch seam; explore uses the already-profiled client."""
        return request

    def wrap_model_call(self, request, handler):
        return handler(self._request_for_phase(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._request_for_phase(request))
