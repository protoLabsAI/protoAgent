"""ADR 0128 S2b: real subagent stacks with a fake gateway client."""
from dataclasses import replace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from graph import agent, delegation_usage, llm, sdk
from graph.config import GenerationProfile, LangGraphConfig
from graph.middleware.completion_guard import CompletionGuardMiddleware
from graph.middleware.profile_phase import ProfilePhaseMiddleware
from graph.middleware.tool_result_pruner import ToolResultPrunerMiddleware
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
from runtime.state import STATE

PROBE = "profile-probe"


class ProbeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def harness(monkeypatch):
    config = LangGraphConfig(api_base="http://gateway.test/v1", api_key="test", filesystem_enabled=False)
    builds, stacks = [], []
    monkeypatch.setattr(STATE, "graph_config", config)
    monkeypatch.setattr(STATE, "plugin_late_tool_factories", [])
    monkeypatch.setattr(agent, "get_all_tools", lambda *a, **kw: [])
    monkeypatch.setattr(agent, "build_subagent_prompt", lambda *a: "Return done.")
    monkeypatch.setitem(SUBAGENT_REGISTRY, PROBE, SubagentConfig(
        name=PROBE, description="probe", system_prompt="Return done.", tools=[],
        max_turns=3, completion_marker="done", inherit_read_tools=False,
    ))
    monkeypatch.setattr("graph.model_window.context_window_for_slot", lambda *a: 196608)

    def fake_client(**kwargs):
        builds.append(kwargs)
        return ProbeModel(messages=iter([AIMessage(
            content="done", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        )]), disable_streaming=True)

    monkeypatch.setattr(llm, "_ReasoningChatOpenAI", fake_client)
    original = agent.create_agent

    def capture(**kwargs):
        stacks.append(kwargs["middleware"])
        return original(**kwargs)

    monkeypatch.setattr(agent, "create_agent", capture)

    async def run(path="direct", profile=None):
        with delegation_usage.collect() as rows:
            if path == "direct":
                out = await agent._run_subagent(
                    config=config, tool_map={}, available_subagents=PROBE,
                    description="probe", prompt="go", subagent_type=PROBE, profile=profile,
                )
            elif path == "sdk":
                out = await sdk.run_subagent(PROBE, "go", description="probe", extra_tools=[], profile=profile)
            else:
                from plugins.workflows import _run_prepared
                from plugins.workflows.run_state import WorkflowRunStore
                from infra.paths import instance_paths

                step = {"id": "probe", "subagent": PROBE, "prompt": "go"}
                if profile is not None:
                    step["profile"] = profile
                recipe = {"name": "probe", "steps": [step], "output": "{{steps.probe.output}}"}
                store = WorkflowRunStore(instance_paths().store("workflows"))
                run_id = store.start("probe", {}, steps=recipe["steps"])
                result = await _run_prepared(recipe, "probe", {}, store, run_id)
                assert result["failed"] == []
                out = result["output"]
        assert "done" in out
        return rows

    return config, builds, stacks, run


@pytest.mark.parametrize("path", ["direct", "sdk", "workflow"])
async def test_no_profiles_preserves_client_kwargs_and_stack(harness, path):
    config, builds, stacks, run = harness
    llm.create_llm(config)  # Today's unprofiled client build.
    baseline = builds.pop()
    rows = await run(path)
    assert builds == [baseline]
    assert not any(isinstance(m, ProfilePhaseMiddleware) for m in stacks[-1])
    assert [row["profile"] for row in rows] == [None]


@pytest.mark.parametrize("path", ["direct", "sdk", "workflow"])
async def test_profile_and_call_precedence(harness, monkeypatch, path):
    config, builds, stacks, run = harness
    config.model_profiles = {
        "routing": GenerationProfile(max_tokens=4096, thinking="disabled", context_budget=48000),
        "call": GenerationProfile(max_tokens=1024),
        "final": GenerationProfile(max_tokens=16000, thinking="enabled"),
    }
    monkeypatch.setitem(SUBAGENT_REGISTRY, PROBE, replace(
        SUBAGENT_REGISTRY[PROBE], profile="routing", final_profile="final",
    ))
    rows = await run(path, "call" if path != "direct" else None)
    name = "call" if path != "direct" else "routing"
    assert builds[0]["max_tokens"] == (1024 if name == "call" else 4096)
    assert builds[0]["extra_body"]["thinking"] == {"type": "disabled"}
    assert builds[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert [row["profile"] for row in rows] == [name]
    phase = next(m for m in stacks[-1] if isinstance(m, ProfilePhaseMiddleware))
    assert phase.explore_profile.context_budget == 48000
    assert phase.final_profile.max_tokens == 16000
    assert len(builds) == 1  # Declaring final_profile never switches S2 out of explore.


@pytest.mark.parametrize("window,expected", [(196608, 48000), (32000, 32000), (None, 48000)])
async def test_context_budget_caps_both_guards(harness, monkeypatch, window, expected):
    config, _, stacks, run = harness
    config.model_profiles = {"lane": GenerationProfile(context_budget=48000)}
    monkeypatch.setattr("graph.model_window.context_window_for_slot", lambda *a: window)
    await run(profile="lane")
    pruner = next(m for m in stacks[-1] if isinstance(m, ToolResultPrunerMiddleware))
    guard = next(m for m in stacks[-1] if isinstance(m, CompletionGuardMiddleware))
    assert pruner._max_input == expected
    assert guard._context_window == expected
    assert guard._context_wrap_up_at == int(expected * 0.8)


@pytest.mark.parametrize("path", ["direct", "sdk", "workflow"])
async def test_unknown_profile_warns_and_runs_unchanged(harness, caplog, path):
    config, builds, stacks, run = harness
    llm.create_llm(config)
    baseline = builds.pop()
    rows = await run(path, "missing")
    assert builds == [baseline]
    assert not any(isinstance(m, ProfilePhaseMiddleware) for m in stacks[-1])
    assert [r["profile"] for r in rows] == [None]
    assert "missing" in caplog.text and "not defined" in caplog.text


async def test_explore_middleware_preserves_request_and_caches_clients(monkeypatch):
    from graph.generation_profile import ResolvedProfile

    client, request = object(), object()
    profile = ResolvedProfile(max_tokens=4096)
    middleware = ProfilePhaseMiddleware(
        LangGraphConfig(), model_name="one", explore_profile=profile,
        explore_name="routing", explore_client=client,
    )
    assert middleware.wrap_model_call(request, lambda r: r) is request

    async def handler(value):
        return value

    assert await middleware.awrap_model_call(request, handler) is request
    assert middleware._llm_for("one", "routing", profile) is client
    builds = []
    def build(*args, **kwargs):
        builds.append(kwargs)
        return object()
    monkeypatch.setattr(llm, "create_llm", build)
    other = middleware._llm_for("two", "routing", profile)
    assert middleware._llm_for("two", "routing", profile) is other
    assert middleware._llm_for("two", "final", ResolvedProfile(max_tokens=8192)) is not other
    assert len(builds) == 2


async def test_undefined_subagent_role_is_inert_without_profiles(harness, monkeypatch, caplog):
    config, builds, stacks, run = harness
    monkeypatch.setitem(SUBAGENT_REGISTRY, PROBE, replace(
        SUBAGENT_REGISTRY[PROBE], profile="routing", final_profile="judgement",
    ))
    llm.create_llm(config)
    baseline = builds.pop()
    rows = await run()
    assert builds == [baseline]
    assert rows[0]["profile"] is None
    assert not any(isinstance(m, ProfilePhaseMiddleware) for m in stacks[-1])
    assert "routing" in caplog.text and "not defined" in caplog.text


async def test_unknown_call_inherits_defined_subagent_profile(harness, monkeypatch, caplog):
    config, builds, _, run = harness
    config.model_profiles = {"routing": GenerationProfile(max_tokens=4096)}
    monkeypatch.setitem(SUBAGENT_REGISTRY, PROBE, replace(SUBAGENT_REGISTRY[PROBE], profile="routing"))
    rows = await run("sdk", "missing")
    assert builds[0]["max_tokens"] == 4096
    assert rows[0]["profile"] == "routing"
    assert "missing" in caplog.text


async def test_fallback_clients_keep_generation_profile(harness):
    config, builds, _, run = harness
    config.model_profiles = {"routing": GenerationProfile(max_tokens=4096, thinking="disabled")}
    config.routing_fallback_models = ["fallback"]
    await run(profile="routing")
    assert len(builds) == 2
    assert all(b["max_tokens"] == 4096 for b in builds)
    assert all(b["extra_body"]["thinking"] == {"type": "disabled"} for b in builds)
