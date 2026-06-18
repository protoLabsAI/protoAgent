"""Tests for LLM kwargs assembly — sampling params + extra_body wiring."""

from graph.config import LangGraphConfig
from graph.llm import _build_llm_kwargs


def test_defaults_omit_optional_sampling_params():
    kwargs = _build_llm_kwargs(LangGraphConfig())
    # Always present.
    assert kwargs["model"]
    assert kwargs["stream_usage"] is True
    assert kwargs["max_tokens"] == LangGraphConfig().max_tokens
    # Opt-in params are absent by default → gateway/model-card defaults win.
    assert "top_p" not in kwargs
    assert "presence_penalty" not in kwargs
    assert "extra_body" not in kwargs


def test_request_timeout_and_max_retries_bound_the_gateway():
    # Prod-readiness: the client must carry a per-call timeout + retry cap so a
    # hung/slow gateway can't block a turn (and the A2A task) indefinitely.
    kwargs = _build_llm_kwargs(LangGraphConfig())
    assert kwargs["timeout"] == 120.0
    assert kwargs["max_retries"] == 2
    custom = _build_llm_kwargs(LangGraphConfig(request_timeout=45.0, llm_max_retries=0))
    assert custom["timeout"] == 45.0 and custom["max_retries"] == 0


def test_standard_openai_params_passed_directly():
    cfg = LangGraphConfig(top_p=0.95, presence_penalty=0.5)
    kwargs = _build_llm_kwargs(cfg)
    assert kwargs["top_p"] == 0.95
    assert kwargs["presence_penalty"] == 0.5
    # These aren't extra_body fields.
    assert "extra_body" not in kwargs


def test_non_openai_params_ride_extra_body():
    cfg = LangGraphConfig(
        top_k=20,
        repetition_penalty=1.1,
        chat_template_kwargs={"preserve_thinking": True},
    )
    kwargs = _build_llm_kwargs(cfg)
    eb = kwargs["extra_body"]
    assert eb["top_k"] == 20
    assert eb["repetition_penalty"] == 1.1
    assert eb["chat_template_kwargs"] == {"preserve_thinking": True}


def test_negative_top_k_means_default_and_is_omitted():
    # -1 is the "let the gateway decide" sentinel.
    kwargs = _build_llm_kwargs(LangGraphConfig(top_k=-1))
    assert "extra_body" not in kwargs


def test_reasoning_controls_omitted_by_default():
    # #1113 — thinking/reasoning_effort are opt-in: unset → nothing emitted,
    # so the provider/model-card default wins and existing configs are unchanged.
    kwargs = _build_llm_kwargs(LangGraphConfig())
    assert "reasoning_effort" not in kwargs
    assert "extra_body" not in kwargs


def test_reasoning_effort_is_top_level():
    # #1113 — reasoning_effort is a native ChatOpenAI param, sent top-level
    # (NOT extra_body).
    kwargs = _build_llm_kwargs(LangGraphConfig(reasoning_effort="high"))
    assert kwargs["reasoning_effort"] == "high"
    assert "extra_body" not in kwargs


def test_thinking_rides_extra_body():
    # #1113 — DeepSeek's thinking toggle rides extra_body as {"thinking": {"type": ...}}.
    for state in ("enabled", "disabled"):
        kwargs = _build_llm_kwargs(LangGraphConfig(thinking=state))
        assert kwargs["extra_body"]["thinking"] == {"type": state}


def test_blank_thinking_is_omitted():
    # "" is the inherit sentinel — no thinking key emitted.
    kwargs = _build_llm_kwargs(LangGraphConfig(thinking=""))
    assert "extra_body" not in kwargs


def test_from_yaml_reads_reasoning_controls(tmp_path):
    import yaml

    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"model": {"thinking": "disabled", "reasoning_effort": "max"}}))
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.thinking == "disabled"
    assert cfg.reasoning_effort == "max"


def test_from_yaml_reads_sampling_block(tmp_path):
    import yaml

    p = tmp_path / "c.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "top_p": 0.9,
                    "top_k": 40,
                    "presence_penalty": 0.3,
                    "repetition_penalty": 1.05,
                    "chat_template_kwargs": {"preserve_thinking": True},
                }
            }
        )
    )
    cfg = LangGraphConfig.from_yaml(p)
    assert cfg.top_p == 0.9
    assert cfg.top_k == 40
    assert cfg.presence_penalty == 0.3
    assert cfg.repetition_penalty == 1.05
    assert cfg.chat_template_kwargs == {"preserve_thinking": True}


def test_create_llm_routes_acp_model_name_to_acp_aux(monkeypatch):
    # An `acp:<agent>` override (aux_model / eval_model / compaction.model / a subagent's
    # model) routes THAT call through the named ACP agent, not the gateway — regardless of
    # the main runtime. Parses the agent off the prefix and hands it to make_acp_aux_model.
    import runtime.acp_runtime as AR
    from graph.llm import create_llm

    captured = {}
    sentinel = object()

    def _fake(config, agent=None):
        captured["agent"] = agent
        return sentinel

    monkeypatch.setattr(AR, "make_acp_aux_model", _fake)
    out = create_llm(LangGraphConfig(), model_name="acp:claude")
    assert out is sentinel and captured["agent"] == "claude"
