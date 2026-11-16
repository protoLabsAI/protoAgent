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


@pytest.mark.parametrize("main,fallbacks,expected", [
    ("main-model", [], ["main-model"]),
    (["primary", "backup"], [], ["primary", "backup"]),
    ("primary", ["backup"], ["primary", "backup"]),
])
@pytest.mark.parametrize("aux", ["aux-model", ["aux-model", "aux-backup"]])
def test_blank_drift_judge_uses_main_not_aux(models, monkeypatch, main, fallbacks, expected, aux):
    # A blank soul_drift_judge_model with aux set judges on MAIN (its pre-0133 model), not
    # the cheap aux model — a single-string config behaves exactly as it did before ADR 0133.
    verdict = dict(drift_score=0.4, identity_preserved=True, doctrine_leak=False, rationale="ok")
    calls = models(json.dumps(verdict))
    monkeypatch.setattr("graph.config_io.read_soul_version", lambda version: "baseline")
    monkeypatch.setattr("graph.config_io.read_soul", lambda: "current")
    cfg = LangGraphConfig(
        model_name=main, routing_fallback_models=fallbacks, aux_model=aux,
        soul_drift_judge_enabled=True, soul_drift_judge_model="",
    )
    assert _judge_soul_drift(cfg, {"baseline_id": "v1"}) == verdict
    assert calls == expected


@pytest.mark.parametrize("explicit", [True, False])
def test_image_description_falls_back_and_blank_inherits_main(models, explicit):
    calls = models("a blue square", image_inputs=True)
    cfg = LangGraphConfig(
        model_name=["primary", "backup"], aux_model="text-only-aux",
        image_describe_model=["primary", "backup"] if explicit else "",
    )
    describe = create_describe_image_fn(cfg)
    assert describe(b"image", "image/png", "square.png") == "a blue square"
    assert calls == ["primary", "backup"]


@pytest.mark.parametrize("explicit,image_inputs,vision", [
    (False, False, True), (True, False, True), (False, None, False),
])
def test_nonvision_image_role_is_unavailable(models, caplog, explicit, image_inputs, vision):
    calls = models("must not invoke", image_inputs=image_inputs)
    cfg = LangGraphConfig(
        model_name="backup", model_vision=vision,
        image_describe_model="backup" if explicit else "",
    )
    assert create_describe_image_fn(cfg) is None
    assert calls == []
    assert "role unavailable" in caplog.text


def test_scalar_image_model_without_profile_preserves_existing_behavior(models):
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
