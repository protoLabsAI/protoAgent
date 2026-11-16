"""Role-chain behavior through memory, drift, and image-description consumers."""

import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from graph.config import LangGraphConfig
from graph.llm import create_describe_image_fn
from graph.projection import ProjectionOptions
from knowledge.memory_select import select_memories
from server.maintenance_loops import _judge_soul_drift


@pytest.fixture
def models(monkeypatch):
    calls = []

    class RateLimit(Exception):
        status_code = 429

    def install(answer, image_inputs=None):
        def build(config, model_name):
            def invoke(messages):
                calls.append(model_name)
                if model_name == "primary":
                    raise RateLimit()
                return AIMessage(content=answer)
            model = RunnableLambda(invoke)
            model.profile = {} if image_inputs is None else {"image_inputs": image_inputs}
            return model
        monkeypatch.setattr("graph.model_roles.create_llm", build)
        return calls
    return install


@pytest.mark.parametrize("chain", [["primary", "backup"], "backup"])
def test_memory_selection_uses_role_fallback(models, chain):
    calls = models('["memory:2"]')
    cfg = LangGraphConfig(model_name="main", memory_select_model=chain)
    opts = ProjectionOptions.from_config(cfg)
    picked = select_memories(
        "quiet places", [{"id": "memory:2", "content": "avoids crowds"}],
        model=opts.select_model_factory(), budget=1000,
    )
    assert picked == ["memory:2"]
    assert calls == (chain if isinstance(chain, list) else [chain])


@pytest.mark.parametrize("chain", [["primary", "backup"], "backup"])
def test_drift_judge_uses_supplied_config_and_role_fallback(models, monkeypatch, chain):
    verdict = dict(drift_score=0.5, identity_preserved=True, doctrine_leak=True, rationale="procedure added")
    calls = models(json.dumps(verdict))
    monkeypatch.setattr("graph.config_io.read_soul_version", lambda version: "baseline")
    monkeypatch.setattr("graph.config_io.read_soul", lambda: "current")
    cfg = LangGraphConfig(soul_drift_judge_enabled=True, soul_drift_judge_model=chain)
    assert _judge_soul_drift(cfg, {"baseline_id": "v1"}) == verdict
    assert calls == (chain if isinstance(chain, list) else [chain])


@pytest.mark.parametrize("explicit", [True, False])
def test_image_description_falls_back_and_blank_inherits_main(models, explicit):
    # A blank describe role inherits the main chain only when the main model can read images
    # (model_vision on), so the chain reaches "backup" after the primary rate-limits.
    calls = models("a blue square")
    cfg = LangGraphConfig(
        model_name=["primary", "backup"], aux_model="text-only-aux", model_vision=True,
        image_describe_model=["primary", "backup"] if explicit else "",
    )
    describe = create_describe_image_fn(cfg)
    assert describe(b"image", "image/png", "square.png") == "a blue square"
    assert calls == ["primary", "backup"]


@pytest.mark.parametrize("image_inputs", [None, False])
def test_blank_image_role_without_main_vision_builds_no_client(monkeypatch, caplog, image_inputs):
    # A blank describe role inherits main (D2); when the main model can't read images the role
    # reports itself unavailable and — crucially — builds no throwaway main-model client to do
    # so (that extra build is observable to subagent profile plumbing). Decided from config.
    built = []

    def build(config, model_name):
        built.append(model_name)
        model = RunnableLambda(lambda messages: AIMessage(content="must not invoke"))
        model.profile = {} if image_inputs is None else {"image_inputs": image_inputs}
        return model

    monkeypatch.setattr("graph.model_roles.create_llm", build)
    cfg = LangGraphConfig(model_name="backup", model_vision=False, image_describe_model="")
    assert create_describe_image_fn(cfg) is None
    assert built == []
    assert "role unavailable" in caplog.text


def test_scalar_image_model_without_profile_preserves_existing_behavior(models):
    # An explicit describe model is vision-capable by the field's purpose — available and run
    # whatever its profile reports (matching the M6 status surface: explicit → always "ok").
    calls = models("a blue square")
    describe = create_describe_image_fn(LangGraphConfig(image_describe_model="backup"))
    assert describe(b"image", "image/png", "square.png") == "a blue square"
    assert calls == ["backup"]


def test_memory_selection_blank_role_preserves_session_model_guard():
    cfg = LangGraphConfig(model_name="configured-main")
    opts = ProjectionOptions.from_config(cfg, model_name="session-main")
    assert opts.select_model_factory is None
    assert opts.select_model_name == "session-main"
    assert cfg.model_name == "configured-main"
