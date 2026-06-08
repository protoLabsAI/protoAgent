"""Plugin-contributed middleware (ADR 0032): register_middleware on the registry,
extra_middleware threaded into the chain before MessageCapture, factories resolved
best-effort, and per-request metadata exposed to middleware via a contextvar."""

from __future__ import annotations


from graph.config import LangGraphConfig
from graph.middleware.request_context import current_request_metadata, request_metadata_scope


def _no_llm_config() -> LangGraphConfig:
    c = LangGraphConfig()
    # Disable every middleware that constructs an LLM so _build_middleware needs no gateway.
    c.compaction_enabled = False
    c.routing_fallback_models = []
    c.enforcement_enabled = False
    c.tools_deferred_enabled = False
    c.audit_middleware = False
    c.memory_middleware = False
    c.ingest_enabled = False
    c.knowledge_middleware = False
    c.skills_enabled = False
    return c


def test_register_middleware_collects_callables_only():
    from graph.plugins.registry import PluginRegistry

    r = PluginRegistry("test", "/tmp")
    r.register_middleware(lambda config: "MW")
    r.register_middleware("not-callable")  # skipped + logged
    assert len(r.middleware) == 1 and callable(r.middleware[0])


def test_build_middleware_appends_extra_before_message_capture():
    from graph.agent import _build_middleware

    class FakeMW:
        pass

    mw = _build_middleware(_no_llm_config(), extra_middleware=[FakeMW()])
    names = [type(m).__name__ for m in mw]
    assert names[-1] == "MessageCaptureMiddleware", names
    assert names[-2] == "FakeMW", names  # plugin middleware sits just before capture


def test_resolve_plugin_middleware_is_best_effort():
    from server.agent_init import _resolve_plugin_middleware

    def good(config):
        return "MW"

    def opts_out(config):
        return None

    def boom(config):
        raise RuntimeError("bad plugin")

    out = _resolve_plugin_middleware(_no_llm_config(), [good, opts_out, boom])
    assert out == ["MW"]  # None filtered, exception swallowed


def test_request_metadata_scope_sets_and_resets():
    assert current_request_metadata() == {}
    with request_metadata_scope({"project": "alpha", "origin": "scheduler"}):
        assert current_request_metadata()["project"] == "alpha"
    assert current_request_metadata() == {}
