"""Tests for LLM kwargs assembly — sampling params + extra_body wiring."""

import httpcore
import httpx
import pytest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from graph.config import LangGraphConfig
from graph.llm import (
    _GATEWAY_UA,
    _ReasoningChatOpenAI,
    _build_llm_kwargs,
    _stream_with_reconnect,
    gateway_client,
    gateway_sync_client,
)


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


# ── #2642: reasoning_content round-trips outbound when thinking is enabled ──────────


def _thinking_model(**extra_body_overrides):
    extra_body = {"thinking": {"type": "enabled"}, **extra_body_overrides}
    return _ReasoningChatOpenAI(
        model="deepseek-v4", api_key="sk-test", base_url="http://localhost:1/v1", extra_body=extra_body
    )


def test_reasoning_content_round_trips_across_a_multi_tool_turn_conversation():
    # The base ChatOpenAI._get_request_payload silently drops additional_kwargs
    # ["reasoning_content"] when building the outbound request — DeepSeek then 400s
    # any turn after a tool call that's missing it. This is the exact shape from the
    # issue: tool call → tool result → summary → a SECOND tool call.
    model = _thinking_model()
    messages = [
        SystemMessage("be helpful"),
        HumanMessage("use a tool please"),
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "I should call the tool now."},
            tool_calls=[{"name": "get_weather", "args": {"city": "NYC"}, "id": "call_1"}],
        ),
        ToolMessage(content="72F sunny", tool_call_id="call_1"),
        AIMessage(content="It is 72F and sunny in NYC.", additional_kwargs={"reasoning_content": "Summarize."}),
        HumanMessage("thanks, and Boston?"),
        AIMessage(
            content="",
            additional_kwargs={},  # no reasoning captured this turn — see the next test
            tool_calls=[{"name": "get_weather", "args": {"city": "Boston"}, "id": "call_2"}],
        ),
        ToolMessage(content="65F cloudy", tool_call_id="call_2"),
    ]
    payload = model._get_request_payload(messages)
    assistant_msgs = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert len(assistant_msgs) == 3
    assert assistant_msgs[0]["reasoning_content"] == "I should call the tool now."
    assert assistant_msgs[1]["reasoning_content"] == "Summarize."
    # Every non-tool, non-user message keeps its own role/content untouched.
    assert payload["messages"][0] == {"role": "system", "content": "be helpful"}
    assert payload["messages"][3]["role"] == "tool"


def test_reasoning_content_defaults_to_empty_string_when_not_captured():
    # DeepSeek requires the KEY present, not merely non-empty — a tool-only turn
    # whose delta never carried reasoning must still get "", not be omitted, or it
    # 400s on exactly the turn this fix exists to unbreak.
    model = _thinking_model()
    messages = [
        HumanMessage("go"),
        AIMessage(content="", additional_kwargs={}, tool_calls=[{"name": "x", "args": {}, "id": "c1"}]),
    ]
    payload = model._get_request_payload(messages)
    assistant_msg = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert assistant_msg["reasoning_content"] == ""


def test_reasoning_content_not_injected_when_thinking_disabled():
    model = _thinking_model()
    model.extra_body = {"thinking": {"type": "disabled"}}
    messages = [
        HumanMessage("hi"),
        AIMessage(
            content="", additional_kwargs={"reasoning_content": "x"}, tool_calls=[{"name": "y", "args": {}, "id": "c1"}]
        ),
    ]
    payload = model._get_request_payload(messages)
    assistant_msg = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert "reasoning_content" not in assistant_msg


def test_reasoning_content_not_injected_without_extra_body():
    # The default shape for every non-DeepSeek-style model (Claude, GPT, ungated
    # gateway slots) — no extra_body at all. Must stay a true no-op.
    model = _ReasoningChatOpenAI(model="gpt-5.6-sol", api_key="sk-test", base_url="http://localhost:1/v1")
    messages = [
        HumanMessage("hi"),
        AIMessage(
            content="", additional_kwargs={"reasoning_content": "x"}, tool_calls=[{"name": "y", "args": {}, "id": "c1"}]
        ),
    ]
    payload = model._get_request_payload(messages)
    assistant_msg = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert "reasoning_content" not in assistant_msg


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


# --- #1931: the shared gateway HTTP client (api_base + bearer + allowlisted UA) ---


async def test_gateway_client_is_preconfigured_for_the_gateway(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LangGraphConfig(api_base="http://gw.test/v1/", api_key="sk-abc")
    async with gateway_client(cfg) as client:
        assert isinstance(client, httpx.AsyncClient)
        assert str(client.base_url).rstrip("/") == "http://gw.test/v1"  # trailing slash normalized
        assert client.headers["User-Agent"] == _GATEWAY_UA  # the WAF-allowlisted UA
        assert client.headers["Authorization"] == "Bearer sk-abc"
        assert client.timeout.read == 120.0  # sane default, overridable per call


async def test_gateway_client_posts_extra_endpoints_with_zero_hand_set_headers():
    # The acceptance shape for #1931: a plugin POSTs an OpenAI-compatible endpoint the
    # chat client doesn't cover, setting NO headers itself — base_url, bearer, and the
    # allowlisted UA all come from the factory.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["ua"] = request.headers.get("user-agent")
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"b64_json": "…"}]})

    cfg = LangGraphConfig(api_base="http://gw.test/v1", api_key="sk-abc")
    async with gateway_client(cfg, transport=httpx.MockTransport(handler)) as client:
        resp = await client.post("/images/generations", json={"model": "img-1", "prompt": "a cat"})
    assert resp.status_code == 200
    assert seen["url"] == "http://gw.test/v1/images/generations"
    assert seen["ua"] == _GATEWAY_UA
    assert seen["auth"] == "Bearer sk-abc"


def test_gateway_client_auth_falls_back_to_env_and_omits_when_absent(monkeypatch):
    cfg = LangGraphConfig(api_base="http://gw.test/v1", api_key="")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    with gateway_sync_client(cfg) as client:
        assert isinstance(client, httpx.Client)
        assert client.headers["Authorization"] == "Bearer env-key"
        assert client.headers["User-Agent"] == _GATEWAY_UA
    monkeypatch.delenv("OPENAI_API_KEY")
    with gateway_sync_client(cfg) as client:
        assert "Authorization" not in client.headers  # no key → no header, not "Bearer "


def test_gateway_base_is_egress_trusted_while_backends_are_not():
    # The egress-trust property the client rides on (ADR 0008): under a deny-by-default
    # allowlist the configured api_base host is auto-included, so a call through the
    # gateway client passes — while a direct provider-backend host is denied.
    from security import egress

    cfg = LangGraphConfig(api_base="http://gw.test/v1")
    egress.set_allowed_hosts(["example.com"], also_allow_url=cfg.api_base)
    try:
        assert egress.check_url("http://gw.test/v1/images/generations") is None
        assert egress.check_url("http://backend.internal/v1/images/generations") is not None
    finally:
        egress.set_allowed_hosts([])


async def test_sdk_gateway_client_resolves_the_live_config(monkeypatch):
    # The plugin-facing handle: graph.sdk.gateway_client() reads the LIVE runtime config
    # (no config argument for the plugin to thread through).
    from graph import sdk
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(api_base="http://gw.test/v1", api_key="sk-live"))
    async with sdk.gateway_client(timeout=7.5) as client:
        assert str(client.base_url).rstrip("/") == "http://gw.test/v1"
        assert client.headers["Authorization"] == "Bearer sk-live"
        assert client.headers["User-Agent"] == _GATEWAY_UA
        assert client.timeout.read == 7.5


# --- #1728: reconnect a provider stream that drops before emitting any content ---


async def _nosleep(_delay):
    return None


def _scripted_stream(script):
    """Build a `make_stream` whose Nth call replays script[N] = (items, exc_or_None):
    yield each item, then raise `exc` if given. Returns (make_stream, state) so a test
    can assert how many times the stream was (re)opened."""
    state = {"opens": 0}

    def make_stream():
        idx = state["opens"]
        state["opens"] += 1
        items, exc = script[idx]

        async def gen():
            for it in items:
                yield it
            if exc is not None:
                raise exc

        return gen()

    return make_stream, state


async def test_stream_reconnect_happy_path_is_transparent():
    make, state = _scripted_stream([(["a", "b", "c"], None)])
    out = [x async for x in _stream_with_reconnect(make, max_retries=2, sleep=_nosleep)]
    assert out == ["a", "b", "c"]
    assert state["opens"] == 1  # no reconnect on a clean stream


async def test_stream_reconnect_recovers_when_nothing_emitted():
    # Provider closed the stream at the top (rate-limit case) — zero chunks, safe to retry.
    make, state = _scripted_stream([([], httpcore.ReadError()), (["x", "y"], None)])
    out = [x async for x in _stream_with_reconnect(make, max_retries=2, sleep=_nosleep)]
    assert out == ["x", "y"]
    assert state["opens"] == 2  # reconnected once


async def test_stream_reconnect_does_not_retry_after_a_token_streamed():
    # A drop AFTER a token would duplicate it on a fresh stream — must propagate.
    make, state = _scripted_stream([(["a"], httpcore.ReadError())])
    got = []
    with pytest.raises(httpcore.ReadError):
        async for x in _stream_with_reconnect(make, max_retries=2, sleep=_nosleep):
            got.append(x)
    assert got == ["a"]
    assert state["opens"] == 1  # no reconnect once content emitted


async def test_stream_reconnect_is_bounded_by_max_retries():
    make, state = _scripted_stream([([], httpcore.ReadError())] * 3)  # attempts = 2 + 1
    with pytest.raises(httpcore.ReadError):
        async for _ in _stream_with_reconnect(make, max_retries=2, sleep=_nosleep):
            pass
    assert state["opens"] == 3  # bounded — no infinite reconnect loop


async def test_stream_reconnect_propagates_non_transport_errors():
    make, state = _scripted_stream([([], ValueError("boom"))])
    with pytest.raises(ValueError, match="boom"):
        async for _ in _stream_with_reconnect(make, max_retries=2, sleep=_nosleep):
            pass
    assert state["opens"] == 1  # a non-transport error is never retried
