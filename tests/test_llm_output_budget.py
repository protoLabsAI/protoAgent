"""Output budget sized to fit the provider's context window (#3502).

`model.max_tokens` is a flat reservation; a prompt within it of the window used to fail
outright. The window is the one the PROVIDER enforces, learned from its own overflow error
(never the gateway's advertised ``max_input_tokens``, which can be input-only or lower than
the backend's real limit). The first overflow on a model is retried once with the budget
that fits; later calls on that model are sized up front. The prompt is sized off the
conversation's MEASURED tokens-per-char, and everything is left exactly as configured
whenever that can't be done safely.
"""

from __future__ import annotations

import json
import math

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from graph import llm
from graph.llm import _ReasoningChatOpenAI

_VLLM_OVERFLOW = (
    "This model's maximum context length is {w} tokens. However, you requested {r} output tokens "
    "and your prompt contains at least {p} input tokens, for a total of at least {t} tokens."
)


@pytest.fixture(autouse=True)
def _clean():
    llm._CALIBRATIONS.clear()
    llm._LEARNED_WINDOWS.clear()
    yield
    llm._CALIBRATIONS.clear()
    llm._LEARNED_WINDOWS.clear()


def _payload(content: str, *, system: str = "You are the reviewer.", task: str = "", requested: int = 32_768) -> dict:
    messages = [{"role": "system", "content": system}]
    if task:
        messages.append({"role": "user", "content": task})
    messages.append({"role": "user", "content": content})
    return {"model": "protolabs/smart", "messages": messages, "max_completion_tokens": requested}


def _calibrate(payload: dict, ratio: float, chars: int | None = None) -> None:
    key = llm._calibration_key("protolabs/smart", payload)
    llm._CALIBRATIONS[key] = (ratio, chars or llm._request_chars(payload))


def _expected_room(payload: dict, ratio: float, window: int) -> int:
    prompt = math.ceil(llm._request_chars(payload) * ratio * (1 + llm._BUDGET_MARGIN_FRACTION))
    return window - (prompt + llm._BUDGET_MARGIN_TOKENS)


# ── which window ──────────────────────────────────────────────────────────────────────


def test_learns_the_shared_window_from_the_providers_overflow_message():
    exc = ValueError(_VLLM_OVERFLOW.format(w=262144, r=32768, p=229377, t=262145))
    assert llm._learn_window(exc, "protolabs/smart") == 262_144
    assert llm._LEARNED_WINDOWS["protolabs/smart"] == 262_144
    classic = ValueError(
        "This model's maximum context length is 128000 tokens. However, you requested 140000 tokens "
        "(120000 in the messages, 20000 in the completion). Please reduce the length of the messages."
    )
    assert llm._learn_window(classic, "gpt-4o") == 128_000


@pytest.mark.parametrize(
    "text",
    [
        "prompt is too long: 210000 tokens > 200000 maximum",  # Anthropic — no shared statement
        "Input tokens exceed the configured limit of 272000 tokens.",  # an input-only cap
        "context_length_exceeded",
    ],
)
def test_messages_that_dont_state_a_shared_window_teach_nothing(text):
    assert llm._learn_window(ValueError(text), "m") is None
    assert "m" not in llm._LEARNED_WINDOWS


def test_an_advertised_window_alone_never_sizes_a_request():
    # gpt-5's bundled profile says 272k input with a SEPARATE 128k output cap; smart's gateway
    # entry says 196,608 in front of a 262,144-token vLLM. Neither is the shared limit, so a
    # call that fits today must go out untouched however big it is.
    model = _ReasoningChatOpenAI(
        model="protolabs/smart", api_key="sk-test", base_url="http://gw.test/v1",
        max_tokens=32_768, profile={"max_input_tokens": 196_608},
    )
    messages = [SystemMessage("You are the reviewer."), HumanMessage("x" * 700_000)]
    _calibrate(model._get_request_payload(messages), 0.25)
    assert model._get_request_payload(messages)["max_completion_tokens"] == 32_768


# ── the sizing rule (window learned) ─────────────────────────────────────────────────


def _learned(window: int = 262_144) -> None:
    llm._LEARNED_WINDOWS["protolabs/smart"] = window


def test_a_prompt_that_would_overflow_the_reservation_gets_the_room_that_fits():
    _learned()
    p = _payload("x" * 900_000)  # ~0.25 tok/char → ~230k tokens: over 262144 - 32768
    _calibrate(p, 0.25)
    llm._fit_output_budget(p, "protolabs/smart")
    room = _expected_room(_payload("x" * 900_000), 0.25, 262_144)
    assert llm._MIN_OUTPUT_TOKENS <= room < 32_768
    assert p["max_completion_tokens"] == room


def test_a_prompt_that_fits_is_sent_unchanged():
    _learned()
    p = _payload("x" * 400_000)
    _calibrate(p, 0.25)
    llm._fit_output_budget(p, "protolabs/smart")
    assert p["max_completion_tokens"] == 32_768


def test_no_measurement_leaves_the_request_alone():
    _learned()
    p = _payload("x" * 900_000)
    llm._fit_output_budget(p, "protolabs/smart")
    assert p["max_completion_tokens"] == 32_768


def test_too_little_room_is_left_to_the_overflow_path():
    _learned()
    p = _payload("x" * 1_030_000)  # ~258k tokens: under _MIN_OUTPUT_TOKENS of room
    _calibrate(p, 0.25)
    llm._fit_output_budget(p, "protolabs/smart")
    assert p["max_completion_tokens"] == 32_768


def test_a_small_configured_budget_is_never_touched():
    _learned()
    p = _payload("x" * 1_000_000, requested=4_096)
    _calibrate(p, 0.25)
    llm._fit_output_budget(p, "protolabs/smart")
    assert p["max_completion_tokens"] == 4_096


def test_parallel_lanes_of_one_agent_never_size_each_other():
    # Same subagent system prompt, different tasks: separate conversations, separate ratios.
    _learned()
    lane_a = _payload("x" * 900_000, system="You are a review-finder.", task="Angle: correctness")
    _calibrate(lane_a, 0.25)
    lane_b = _payload("x" * 900_000, system="You are a review-finder.", task="Angle: cross-file")
    llm._fit_output_budget(lane_b, "protolabs/smart")
    assert lane_b["max_completion_tokens"] == 32_768


def test_a_measurement_from_a_much_smaller_request_is_not_trusted():
    # Template overhead inflates a small call's ratio; a sudden jump is left alone.
    _learned()
    p = _payload("x" * 900_000)
    _calibrate(p, 0.25, chars=100_000)
    llm._fit_output_budget(p, "protolabs/smart")
    assert p["max_completion_tokens"] == 32_768


def test_calibration_ignores_small_calls():
    llm._record_calibration(("k", 20_000, 32_768), 5_000)
    assert "k" not in llm._CALIBRATIONS
    llm._record_calibration(("k", 400_000, 32_768), 100_000)
    assert llm._CALIBRATIONS["k"] == (pytest.approx(0.25), 400_000)


def test_media_parts_are_not_counted_as_prompt_text():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 500_000}}
    with_image = {"messages": [{"role": "user", "content": [{"type": "text", "text": "look"}, image]}]}
    text_only = {"messages": [{"role": "user", "content": [{"type": "text", "text": "look"}]}]}
    assert llm._request_chars(with_image) == llm._request_chars(text_only)


def test_a_call_with_media_does_not_calibrate():
    p = _payload("unused")
    p["messages"][-1]["content"] = [
        {"type": "text", "text": "x" * 100_000},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    llm._fit_output_budget(p, "protolabs/smart")
    assert llm._REQUEST_MEASURE.get() is None
    llm._fit_output_budget(_payload("x" * 100_000), "protolabs/smart")
    assert llm._REQUEST_MEASURE.get() is not None


def test_a_sizing_error_never_breaks_the_request(monkeypatch):
    def boom(_payload):
        raise RuntimeError("sizing bug")

    monkeypatch.setattr(llm, "_request_chars", boom)
    model = _ReasoningChatOpenAI(model="protolabs/smart", api_key="sk-test", base_url="http://gw.test/v1", max_tokens=32_768)
    assert model._get_request_payload([HumanMessage("hi")])["max_completion_tokens"] == 32_768


# ── end to end: the real request path over a fake gateway that enforces its window ────


def _sse(prompt_tokens: int) -> bytes:
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "protolabs/smart"}
    frames = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {**base, "choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 1, "total_tokens": prompt_tokens + 1}},
    ]
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames).encode() + b"data: [DONE]\n\n"


def _gateway(window: int, ratio: float, sent: list):
    """A vLLM-like endpoint: prompt tokens = ratio × body chars; prompt + budget > window → 400."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt, budget = round(llm._request_chars(body) * ratio), body["max_completion_tokens"]
        sent.append(budget)
        if prompt + budget > window:
            msg = _VLLM_OVERFLOW.format(w=window, r=budget, p=window - budget + 1, t=window + 1)
            return httpx.Response(400, json={"error": {"message": msg, "type": "BadRequestError", "code": 400}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(prompt))

    return handler


def _model(handler) -> _ReasoningChatOpenAI:
    return _ReasoningChatOpenAI(
        model="protolabs/smart", api_key="sk-test", base_url="http://gw.test/v1",
        max_tokens=16_384, streaming=True, stream_usage=True, max_retries=0,
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_an_overflow_is_retried_once_with_the_budget_that_fits_then_sized_up_front():
    sent: list[int] = []
    model = _model(_gateway(window=60_000, ratio=0.25, sent=sent))
    convo = [SystemMessage("You are a review-finder."), HumanMessage("Angle: correctness")]

    # 1. ~37.5k tokens + 16,384 fits: goes out as configured and calibrates this conversation.
    convo += [HumanMessage("a" * 150_000)]
    await model.ainvoke(convo)
    assert sent == [16_384]

    # 2. The conversation grows to ~45k tokens: prompt + 16,384 > 60,000. The provider's
    #    overflow states its window; the call is retried once with what fits, and succeeds.
    convo += [AIMessage("ok"), HumanMessage("b" * 30_000)]
    await model.ainvoke(convo)
    assert sent[1] == 16_384 and llm._MIN_OUTPUT_TOKENS <= sent[2] < 16_384
    assert llm._LEARNED_WINDOWS["protolabs/smart"] == 60_000

    # 3. The window is known now: the next near-window call is sized before it's sent.
    convo += [AIMessage("ok"), HumanMessage("c" * 5_000)]
    await model.ainvoke(convo)
    assert len(sent) == 4 and sent[3] < 16_384


async def test_an_overflow_with_nothing_to_size_it_from_propagates_unchanged():
    sent: list[int] = []
    model = _model(_gateway(window=60_000, ratio=0.25, sent=sent))
    with pytest.raises(Exception, match="maximum context length"):
        await model.ainvoke([SystemMessage("s"), HumanMessage("z" * 200_000)])  # first call: no measurement
    assert sent == [16_384]  # one request, no retry
