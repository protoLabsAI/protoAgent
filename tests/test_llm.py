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
