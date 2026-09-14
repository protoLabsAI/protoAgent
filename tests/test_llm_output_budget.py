"""Output budget sized to fit the context window (#3502).

`model.max_tokens` is a flat reservation; a prompt within it of the window used to fail
outright. The request now asks for what fits — sized off the agent's MEASURED
tokens-per-char, never a chars/4 guess — and is left exactly as configured whenever the
window is unknown, nothing has been measured yet, the call already fits, or too little room
remains for a lowered budget to be useful.
"""

from __future__ import annotations

import json
import math

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from graph import llm
from graph.llm import _ReasoningChatOpenAI


@pytest.fixture(autouse=True)
def _clean_calibration():
    llm._TOKENS_PER_CHAR.clear()
    yield
    llm._TOKENS_PER_CHAR.clear()


def _payload(content: str, *, system: str = "You are the reviewer.", requested: int = 32_768) -> dict:
    return {
        "model": "protolabs/smart",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "max_completion_tokens": requested,
    }


def _calibrate(payload: dict, ratio: float) -> None:
    llm._TOKENS_PER_CHAR[llm._calibration_key("protolabs/smart", payload)] = ratio


def _expected_room(payload: dict, ratio: float, window: int) -> int:
    prompt = math.ceil(llm._request_chars(payload) * ratio * (1 + llm._BUDGET_MARGIN_FRACTION))
    return window - (prompt + llm._BUDGET_MARGIN_TOKENS)


# ── the sizing rule ───────────────────────────────────────────────────────────────────


def test_a_prompt_that_would_overflow_the_reservation_gets_the_room_that_fits():
    p = _payload("x" * 900_000)  # ~0.25 tok/char → ~230k tokens: over 262144 - 32768
    _calibrate(p, 0.25)
    llm._fit_output_budget(p, "protolabs/smart", 262_144)
    room = _expected_room(_payload("x" * 900_000), 0.25, 262_144)
    assert llm._MIN_OUTPUT_TOKENS <= room < 32_768
    assert p["max_completion_tokens"] == room


def test_a_prompt_that_fits_is_sent_unchanged():
    p = _payload("x" * 400_000)  # ~100k tokens: plenty of room for the full reservation
    _calibrate(p, 0.25)
    llm._fit_output_budget(p, "protolabs/smart", 262_144)
    assert p["max_completion_tokens"] == 32_768


def test_unknown_window_or_no_measurement_leaves_the_request_alone():
    p = _payload("x" * 900_000)
    llm._fit_output_budget(p, "protolabs/smart", None)  # window unknown
    assert p["max_completion_tokens"] == 32_768
    llm._fit_output_budget(p, "protolabs/smart", 262_144)  # window known, nothing measured
    assert p["max_completion_tokens"] == 32_768


def test_too_little_room_is_left_to_the_overflow_path():
    # Squeezing to a few hundred tokens would starve a thinking model; the provider's
    # overflow error reaches force-compact-and-retry (#2783) instead.
    p = _payload("x" * 1_030_000)  # ~258k tokens: under _MIN_OUTPUT_TOKENS of room
    _calibrate(p, 0.25)
    llm._fit_output_budget(p, "protolabs/smart", 262_144)
    assert p["max_completion_tokens"] == 32_768


def test_a_small_configured_budget_is_never_touched():
    p = _payload("x" * 1_000_000, requested=4_096)
    _calibrate(p, 0.25)
    llm._fit_output_budget(p, "protolabs/smart", 262_144)
    assert p["max_completion_tokens"] == 4_096


def test_one_agents_measurement_never_sizes_another_agents_request():
    lane = _payload("x" * 900_000, system="You are a finder lane.")
    _calibrate(lane, 0.25)
    chat = _payload("x" * 900_000, system="You are the operator's assistant.")
    llm._fit_output_budget(chat, "protolabs/smart", 262_144)
    assert chat["max_completion_tokens"] == 32_768


def test_the_legacy_max_tokens_key_is_sized_too():
    p = _payload("x" * 900_000)
    p["max_tokens"] = p.pop("max_completion_tokens")
    _calibrate(p, 0.25)
    llm._fit_output_budget(p, "protolabs/smart", 262_144)
    assert p["max_tokens"] < 32_768


def test_calibration_ignores_small_calls():
    llm._record_calibration(("k", 20_000), 5_000)  # template overhead dominates → skipped
    assert "k" not in llm._TOKENS_PER_CHAR
    llm._record_calibration(("k", 400_000), 100_000)
    assert llm._TOKENS_PER_CHAR["k"] == pytest.approx(0.25)


def test_media_parts_are_not_counted_as_prompt_text():
    # An attached image is base64 in the body; its characters say nothing about its tokens.
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 500_000}}
    with_image = {"messages": [{"role": "user", "content": [{"type": "text", "text": "look"}, image]}]}
    text_only = {"messages": [{"role": "user", "content": [{"type": "text", "text": "look"}]}]}
    assert llm._request_chars(with_image) == llm._request_chars(text_only)


def test_a_call_with_media_does_not_calibrate():
    p = _payload("unused")
    p["messages"][1]["content"] = [
        {"type": "text", "text": "x" * 100_000},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    llm._fit_output_budget(p, "protolabs/smart", 262_144)
    assert llm._REQUEST_MEASURE.get() is None  # nothing for the stream's usage to pair with
    text = _payload("x" * 100_000)
    llm._fit_output_budget(text, "protolabs/smart", 262_144)
    assert llm._REQUEST_MEASURE.get() is not None


def test_a_sizing_error_never_breaks_the_request(monkeypatch):
    def boom(_payload):
        raise RuntimeError("sizing bug")

    monkeypatch.setattr(llm, "_request_chars", boom)
    model = _ReasoningChatOpenAI(
        model="protolabs/smart",
        api_key="sk-test",
        base_url="http://gw.test/v1",
        max_tokens=32_768,
        profile={"max_input_tokens": 262_144},
    )
    payload = model._get_request_payload([HumanMessage("hi")])
    assert payload["max_completion_tokens"] == 32_768


# ── end to end: the real request path and the real wire ──────────────────────────────


def _sse(prompt_tokens: int) -> bytes:
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "protolabs/smart"}
    frames = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {**base, "choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 1, "total_tokens": prompt_tokens + 1}},
    ]
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames).encode() + b"data: [DONE]\n\n"


async def test_a_measured_call_sizes_the_next_request_on_the_wire():
    window, ratio = 60_000, 0.25
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        # The gateway reports the call's true prompt size: `ratio` tokens per body char.
        prompt_tokens = round(llm._request_chars(body) * ratio)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(prompt_tokens))

    model = _ReasoningChatOpenAI(
        model="protolabs/smart",
        api_key="sk-test",
        base_url="http://gw.test/v1",
        max_tokens=16_384,
        streaming=True,
        stream_usage=True,
        max_retries=0,
        profile={"max_input_tokens": window},
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    system = SystemMessage("You are a finder lane.")

    # 1. First call of the process: nothing measured yet, so it goes out as configured —
    #    and its reported usage calibrates this agent.
    await model.ainvoke([system, HumanMessage("a" * 40_000)])
    assert bodies[-1]["max_completion_tokens"] == 16_384
    assert llm._TOKENS_PER_CHAR, "the finished stream's usage should have been recorded"

    # 2. A prompt that would not fit beside the full reservation asks for what fits.
    await model.ainvoke([system, HumanMessage("b" * 180_000)])
    sent = bodies[-1]["max_completion_tokens"]
    measured = next(iter(llm._TOKENS_PER_CHAR.values()))
    assert sent == _expected_room(bodies[-1] | {"max_completion_tokens": 16_384}, measured, window)
    assert llm._MIN_OUTPUT_TOKENS <= sent < 16_384

    # 3. A prompt that fits is sent unchanged.
    await model.ainvoke([system, HumanMessage("c" * 40_000)])
    assert bodies[-1]["max_completion_tokens"] == 16_384
