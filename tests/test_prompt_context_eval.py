"""The context experiment must detect cache churn without retaining content."""

from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from evals.prompt_context import RequestProbe, request_shape, usage_summary
from graph.context_frame import context_frame_message
from graph.middleware.prompt_cache import PromptCacheMiddleware
from graph.middleware.provider_shape import ProviderShapeMiddleware
from graph.middleware.tool_deferral import ToolDeferralMiddleware


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": "PRIVATE DESCRIPTION", "parameters": {"type": "object", "properties": {}}}}


@dataclass
class Request:
    system_message: object = field(default_factory=lambda: SystemMessage(content="PRIVATE SYSTEM"))
    messages: list = field(default_factory=lambda: [HumanMessage(content="PRIVATE INPUT")])
    tools: list = field(default_factory=lambda: [_tool("base"), _tool("extra")])
    state: dict = field(default_factory=dict)
    model_settings: dict = field(default_factory=dict)
    model: object = field(default_factory=lambda: SimpleNamespace(model_name="fixture"))

    def override(self, **changes):
        return replace(self, **changes)


def test_report_preserves_order_but_not_content_and_counts_mirrored_args_once():
    args = {"query": "PRIVATE ARGUMENT"}
    m = AIMessage(content=[{"type": "tool_use", "id": "c", "name": "base", "input": args}],
                  tool_calls=[{"id": "c", "name": "base", "args": args}])
    a = request_shape(Request(messages=[m]))
    b = request_shape(Request(messages=[m], tools=[_tool("extra"), _tool("base")]))
    assert a["schemas"] != b["schemas"]
    assert a["tool_order"] == ["base", "extra"]
    assert a["messages"][0]["tool_args_chars"] == len('{"query":"PRIVATE ARGUMENT"}')
    assert "PRIVATE" not in str(a)


@pytest.mark.parametrize("provider", ["openai-compat", "anthropic-oauth", "openai-codex"])
def test_dynamic_projection_leaves_cached_prefix_and_history_unchanged(provider):
    model = SimpleNamespace(model_name="fixture", _protoagent_provider_type=provider)
    req = Request(model=model)
    cached, _ = PromptCacheMiddleware(ttl="1h")._transform(req)
    reports = []
    for text in ("first context", "different context"):
        # Actual production order: cache marks history BEFORE projection/provider shape.
        projected = cached.override(messages=[*cached.messages, context_frame_message(text)])
        reports.append(request_shape(ProviderShapeMiddleware()._transform(projected)))
    a, b = reports
    assert a["system"] == b["system"]
    assert a["schemas"] == b["schemas"]
    assert a["messages"][:-1] == b["messages"][:-1]
    assert a["messages"][-1]["text"] != b["messages"][-1]["text"]
    assert a["messages"][-1]["cache_controls"] == []
    assert a["messages"][0]["cache_controls"] == [{"type": "ephemeral", "ttl": "1h"}]
    assert req.messages[0].content == "PRIVATE INPUT"  # no checkpoint mutation
    assert a["system_delivery"] == ("instructions" if provider == "openai-codex" else "system_message")


def test_deferral_schema_widening_and_post_compaction_loss_are_visible():
    mw = ToolDeferralMiddleware({"base"})
    loaded = ToolMessage(content="- `extra` — loaded", name="search_tools", tool_call_id="c")
    base = request_shape(mw._transform(Request()))
    widened = request_shape(mw._transform(Request(state={"messages": [loaded]})))
    compacted = request_shape(mw._transform(Request(state={"messages": [HumanMessage(content="summary")]})))
    assert base["tool_order"] == compacted["tool_order"] == ["base"]
    assert widened["tool_order"] == ["base", "extra"]
    assert widened["schemas"] != base["schemas"]


def test_usage_distinguishes_unknown_zero_and_priority_cache_reads():
    def response(details):
        return SimpleNamespace(result=[AIMessage(content="ok", usage_metadata={
            "input_tokens": 100, "output_tokens": 1, "total_tokens": 101,
            "input_token_details": details,
        })])
    unknown = usage_summary(response({}))
    zero = usage_summary(response({"cache_read": 0}))
    hit = usage_summary(response({"priority_cache_read": 80, "priority_cache_creation": 10}))
    assert unknown["cache_read_tokens"] is None
    assert unknown["cache_read_ratio"] is None
    assert zero["cache_read_tokens"] == 0
    assert zero["cache_write_tokens"] is None
    assert hit["cache_read_ratio"] == .8
    assert hit["non_read_input_tokens"] == 20
    assert hit["cache_write_tokens"] == 10
    write_only = usage_summary(response({"cache_creation": 80}))
    assert write_only["cache_write_tokens"] == 80
    assert write_only["cache_read_tokens"] is None
    assert write_only["cache_read_ratio"] is None


@pytest.mark.asyncio
async def test_probe_incognito_and_error_privacy():
    probe = RequestProbe()

    async def fail(request):
        raise ValueError("PRIVATE PROVIDER ERROR")

    with pytest.raises(ValueError):
        await probe.awrap_model_call(Request(state={"incognito": True}), fail)
    assert probe.rows == []
    with pytest.raises(ValueError):
        await probe.awrap_model_call(Request(), fail)
    assert probe.rows[0]["error_type"] == "ValueError"
    assert "PRIVATE" not in str(probe.report())


@pytest.mark.platform_sensitive
def test_failed_live_run_retains_completed_quality_outcomes(monkeypatch, tmp_path):
    import json

    from evals import prompt_context
    from graph.config import LangGraphConfig

    monkeypatch.setattr(LangGraphConfig, "from_yaml", lambda _: object())

    async def partial(config, model, repeat, probe, outcomes):
        outcomes.append({"scenario": "completed", "passed": True})
        probe.scenario = "failed"
        raise ValueError("PRIVATE PROVIDER ERROR")

    monkeypatch.setattr(prompt_context, "_live", partial)
    output = tmp_path / "report.json"
    assert prompt_context.main(["--config", str(tmp_path / "config.yaml"), "--model", "fixture", "--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert report["quality"] == [
        {"scenario": "completed", "passed": True},
        {"scenario": "failed", "passed": False, "incomplete": True},
    ]
    assert report["failure_type"] == "ValueError"
    assert "PRIVATE" not in output.read_text()


@pytest.mark.platform_sensitive
@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["minimal", "full", "projects_delegates", "catalog", "background", "incognito"])
async def test_real_graph_request_profiles_and_reload(monkeypatch, tmp_path, profile):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import StructuredTool

    import graph.agent as agent
    from graph.config import LangGraphConfig
    from runtime.state import STATE

    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    cfg = LangGraphConfig(memory_middleware=False, compaction_enabled=False, audit_middleware=False)
    probe = RequestProbe()
    shaping = agent.provider_shape_middleware
    monkeypatch.setattr(agent, "provider_shape_middleware", lambda c: [*shaping(c), probe])
    monkeypatch.setattr(agent, "create_llm", lambda *a, **k: Model(messages=iter([AIMessage(content="done")]), disable_streaming=True))
    monkeypatch.setattr(STATE, "delegate_registry", None)
    extras = []
    if profile == "minimal":
        cfg.tools_disabled = [t.name for t in agent.create_agent_graph(cfg, include_subagents=False).bound_tools
                              if t.name != "current_time"]
    if profile == "catalog":
        def inspect_fixture(query: str) -> str:
            return "fixture"
        extras = [StructuredTool.from_function(inspect_fixture, name=f"fixture_{i}", description=f"Read-only synthetic catalog capability {i}.") for i in range(40)]
    if profile == "projects_delegates":
        cfg.filesystem_enabled = True
        cfg.projects = [{"name": "fixture", "path": str(tmp_path), "write": False}]
        monkeypatch.setattr(STATE, "delegate_registry", SimpleNamespace(names=lambda: ["reviewer"]))
    state = {"messages": [HumanMessage(content="hello")], "session_id": "fixture"}
    if profile == "background":
        state["subagent_fence"] = ["current_time"]
    if profile == "incognito":
        state["incognito"] = True
    for _ in range(2):  # actual graph rebuild, same config and synthetic input
        graph = agent.create_agent_graph(cfg, extra_tools=extras, include_subagents=profile != "minimal")
        if profile == "projects_delegates":
            assert {"Managed projects", "Collaboration"} <= {label for label, _ in graph.system_prompt_parts}
        await graph.ainvoke(state, {"recursion_limit": 100})
    if profile == "incognito":
        assert probe.rows == []
        return
    a, b = probe.rows
    assert a["system"] == b["system"]
    assert a["schemas"] == b["schemas"]
    assert a["tool_order"] == b["tool_order"]
    if profile in {"minimal", "background"}:
        assert a["tool_order"] == ["current_time"]
    if profile == "catalog":
        assert len([n for n in a["tool_order"] if n.startswith("fixture_")]) == 40
