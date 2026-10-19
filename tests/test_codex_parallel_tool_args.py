"""Codex function-call arguments survive every stream arrangement (#4368).

The ChatGPT/Codex backend expands a parallel batch (``multi_tool_use.parallel``)
into N ordinary ``function_call`` items and, as a live ``gpt-6.1-sol`` capture
showed, streams each one as ``output_item.added`` (``arguments: ""``) →
``function_call_arguments.done`` → ``output_item.done`` with NO delta events.
langchain-openai reads arguments only from ``.added`` and the deltas, so every
call in the batch reached its tool as ``{}``. These tests drive the real
converter (with protoAgent's shim installed) through each arrangement.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_openai.chat_models import base as lc_base
from openai.types.responses import (
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseReasoningItem,
)
from openai.types.responses.response_output_item_added_event import ResponseOutputItemAddedEvent
from openai.types.responses.response_output_item_done_event import ResponseOutputItemDoneEvent

from graph.providers.codex_client import (
    _CAPTURE_ISSUER,
    _FC_STATE,
    _install_reasoning_capture,
    _route_function_call,
    build_codex_client,
    issuer_fingerprint,
)

ISSUER = issuer_fingerprint("https://chatgpt.com/backend-api/codex", "acct-7")

# The batch from the issue: three board reads and one component read.
CALLS = [
    ("fc_a", "call_a", "board_get_feature", {"feature_id": "ds-a1"}),
    ("fc_b", "call_b", "board_get_feature", {"feature_id": "ds-b2"}),
    ("fc_c", "call_c", "board_get_feature", {"feature_id": "ds-c3"}),
    ("fc_d", "call_d", "ds_component", {"name": "Menu"}),
]

_seq = iter(range(10_000))


@pytest.fixture(autouse=True)
def _capture_installed():
    _install_reasoning_capture()
    yield


def _item(i: int, *, arguments: str = "", status: str = "in_progress") -> ResponseFunctionToolCall:
    fc_id, call_id, name, _ = CALLS[i]
    return ResponseFunctionToolCall(
        type="function_call", id=fc_id, call_id=call_id, name=name, arguments=arguments, status=status
    )


def _args(i: int) -> str:
    return json.dumps(CALLS[i][3])


def added(i: int, out: int, *, arguments: str = ""):
    return ResponseOutputItemAddedEvent(
        type="response.output_item.added",
        output_index=out,
        sequence_number=next(_seq),
        item=_item(i, arguments=arguments),
    )


def delta(i: int, out: int, text: str):
    return ResponseFunctionCallArgumentsDeltaEvent(
        type="response.function_call_arguments.delta",
        output_index=out,
        item_id=CALLS[i][0],
        delta=text,
        sequence_number=next(_seq),
    )


def args_done(i: int, out: int):
    return ResponseFunctionCallArgumentsDoneEvent(
        type="response.function_call_arguments.done",
        output_index=out,
        item_id=CALLS[i][0],
        arguments=_args(i),
        sequence_number=next(_seq),
    )


def item_done(i: int, out: int):
    return ResponseOutputItemDoneEvent(
        type="response.output_item.done",
        output_index=out,
        sequence_number=next(_seq),
        item=_item(i, arguments=_args(i), status="completed"),
    )


def deltas(i: int, out: int, pieces: int = 3) -> list:
    text = _args(i)
    step = max(1, len(text) // pieces)
    return [delta(i, out, text[k : k + step]) for k in range(0, len(text), step)]


def reasoning(out: int):
    return [
        ResponseOutputItemAddedEvent(
            type="response.output_item.added",
            output_index=out,
            sequence_number=next(_seq),
            item=ResponseReasoningItem(id="rs_1", type="reasoning", summary=[]),
        ),
        ResponseOutputItemDoneEvent(
            type="response.output_item.done",
            output_index=out,
            sequence_number=next(_seq),
            item=ResponseReasoningItem(id="rs_1", type="reasoning", summary=[], encrypted_content="BLOB"),
        ),
    ]


def _accumulate(events, *, issuer: str = ISSUER, convert=None, output_version: str = "responses/v1"):
    """Drive events through the converter the way `_stream_responses` does."""
    convert = convert or lc_base._convert_responses_chunk_to_generation_chunk
    token = _CAPTURE_ISSUER.set(issuer)
    fc_token = _FC_STATE.set({})
    try:
        idx = out_idx = sub_idx = -1
        acc: AIMessageChunk | None = None
        for event in events:
            idx, out_idx, sub_idx, gen = convert(event, idx, out_idx, sub_idx, output_version=output_version)
            if gen is not None:
                acc = gen.message if acc is None else acc + gen.message
        return acc
    finally:
        _FC_STATE.reset(fc_token)
        _CAPTURE_ISSUER.reset(token)


def _assert_calls(acc: AIMessageChunk, which: list[int]) -> None:
    assert not acc.invalid_tool_calls
    got = [(tc["id"], tc["name"], tc["args"]) for tc in acc.tool_calls]
    assert got == [(CALLS[i][1], CALLS[i][2], CALLS[i][3]) for i in which]
    # The replayed history item carries the same arguments, exactly once.
    blocks = [b for b in acc.content if isinstance(b, dict) and b.get("type") == "function_call"]
    assert [(b["id"], b["call_id"], b["name"], b["arguments"]) for b in blocks] == [
        (CALLS[i][0], CALLS[i][1], CALLS[i][2], _args(i)) for i in which
    ]


# ── the live arrangement ────────────────────────────────────────────────────────


def test_live_codex_shape_args_only_on_done_events():
    """The exact shape captured from gpt-6.1-sol: no deltas; arguments on both
    `function_call_arguments.done` and `output_item.done`. Delivered once, not twice."""
    events = reasoning(0)
    for i in range(4):
        events += [added(i, i + 1), args_done(i, i + 1), item_done(i, i + 1)]
    acc = _accumulate(events)
    _assert_calls(acc, [0, 1, 2, 3])
    assert [b for b in acc.content if b.get("type") == "reasoning"][0]["encrypted_content"] == "BLOB"


def test_without_the_shim_the_live_shape_loses_every_argument():
    """Characterizes the bug (and that the fix stays gated on the Codex client):
    the same stream through the un-armed converter yields `{}` for every call."""
    events = []
    for i in range(3):
        events += [added(i, i), args_done(i, i), item_done(i, i)]
    acc = _accumulate(events, issuer="")
    assert [tc["args"] for tc in acc.tool_calls] == [{}, {}, {}]


def test_args_only_on_output_item_done():
    events = []
    for i in range(4):
        events += [added(i, i), item_done(i, i)]
    _assert_calls(_accumulate(events), [0, 1, 2, 3])


def test_args_only_on_function_call_arguments_done():
    events = []
    for i in range(4):
        events += [added(i, i), args_done(i, i)]
    _assert_calls(_accumulate(events), [0, 1, 2, 3])


def test_done_events_for_a_batch_opened_all_at_once_arrive_out_of_order():
    """Every item opens first, then the done events land in a different order."""
    events = [added(i, i) for i in range(4)]
    for i in (2, 0, 3, 1):
        events += [args_done(i, i), item_done(i, i)]
    _assert_calls(_accumulate(events), [0, 1, 2, 3])


# ── deltas: unchanged, never doubled ────────────────────────────────────────────


def test_streamed_deltas_come_out_exactly_as_upstream_builds_them():
    """Normal delta streaming (then both done events) is byte-identical to the
    un-armed converter — the done events add nothing on top of the deltas."""
    events = []
    for i in range(3):
        events += [added(i, i), *deltas(i, i), args_done(i, i), item_done(i, i)]
    armed = _accumulate(events)
    _assert_calls(armed, [0, 1, 2])

    plain_events = []
    for i in range(3):
        plain_events += [added(i, i), *deltas(i, i)]
    plain = _accumulate(plain_events, issuer="")
    assert armed.tool_calls == plain.tool_calls
    assert armed.content == plain.content


def test_arguments_already_on_added_are_not_doubled():
    events = []
    for i in range(2):
        events += [added(i, i, arguments=_args(i)), args_done(i, i), item_done(i, i)]
    _assert_calls(_accumulate(events), [0, 1])


def test_interleaved_deltas_from_parallel_calls_land_on_their_own_calls():
    """Upstream's cursor advances on every output_index change, so interleaved
    deltas would each open a new nameless block; they must route by item id."""
    a, b, c = (deltas(i, i) for i in range(3))
    events = [added(0, 0), added(1, 1), added(2, 2)]
    for k in range(max(len(a), len(b), len(c))):
        for stream in (c, a, b):
            if k < len(stream):
                events.append(stream[k])
    events += [item_done(1, 1), args_done(0, 0), item_done(0, 0), args_done(2, 2), item_done(2, 2), args_done(1, 1)]
    _assert_calls(_accumulate(events), [0, 1, 2])


def test_mixed_batch_some_streamed_some_done_only():
    events = [
        added(0, 0),
        *deltas(0, 0),
        added(1, 1),
        args_done(1, 1),
        item_done(0, 0),
        item_done(1, 1),
        added(3, 2),
        item_done(3, 2),
    ]
    _assert_calls(_accumulate(events), [0, 1, 3])


def test_a_later_item_still_opens_its_own_block_after_a_routed_event():
    """The shim returns the caller's cursor unchanged, so the next `.added` advances
    from where upstream left it rather than colliding with an earlier block."""
    events = [added(0, 0), added(1, 1), args_done(0, 0), item_done(0, 0), added(2, 2), item_done(2, 2), item_done(1, 1)]
    _assert_calls(_accumulate(events), [0, 1, 2])


# ── guards ──────────────────────────────────────────────────────────────────────


def test_a_future_upstream_that_handles_output_item_done_is_not_doubled():
    """If a langchain bump starts emitting arguments on `output_item.done`, the
    shim must not add its own copy on top (nor keep upstream's after supplying them)."""
    real = lc_base._convert_responses_chunk_to_generation_chunk.__wrapped__

    def future_upstream(chunk, ci, coi, csi, *args, **kwargs):
        if chunk.type == "response.output_item.done" and chunk.item.type == "function_call":
            synthetic = ResponseFunctionCallArgumentsDeltaEvent(
                type="response.function_call_arguments.delta",
                output_index=chunk.output_index,
                item_id=chunk.item.id,
                delta=chunk.item.arguments,
                sequence_number=0,
            )
            return real(synthetic, ci, coi, csi, *args, **kwargs)
        return real(chunk, ci, coi, csi, *args, **kwargs)

    def convert(chunk, ci, coi, csi, *args, **kwargs):
        routed = _route_function_call(future_upstream, chunk, ci, coi, csi, args, kwargs)
        return routed if routed is not None else future_upstream(chunk, ci, coi, csi, *args, **kwargs)

    # Only output_item.done: upstream's own args are used, once.
    events = []
    for i in range(2):
        events += [added(i, i), item_done(i, i)]
    _assert_calls(_accumulate(events, convert=convert), [0, 1])

    # arguments.done first (shim supplies them), then upstream's output_item.done: stripped.
    events = []
    for i in range(2):
        events += [added(i, i), args_done(i, i), item_done(i, i)]
    _assert_calls(_accumulate(events, convert=convert), [0, 1])

    # Deltas streamed, then upstream's output_item.done: stripped.
    events = []
    for i in range(2):
        events += [added(i, i), *deltas(i, i), item_done(i, i)]
    _assert_calls(_accumulate(events, convert=convert), [0, 1])


def test_v0_output_version_still_gets_the_arguments():
    events = []
    for i in range(2):
        events += [added(i, i), args_done(i, i), item_done(i, i)]
    acc = _accumulate(events, output_version="v0")
    assert [(tc["name"], tc["args"]) for tc in acc.tool_calls] == [(CALLS[i][2], CALLS[i][3]) for i in range(2)]


# ── through the client's own stream ─────────────────────────────────────────────


class _FakeStream:
    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, *exc):
        return False


class _FakeResponses:
    def __init__(self, events):
        self._events = events

    def create(self, **payload):
        return _FakeStream(self._events)


class _FakeRootClient:
    def __init__(self, events):
        self.responses = _FakeResponses(events)


def test_the_client_stream_delivers_batched_args_and_releases_its_state():
    client = build_codex_client(
        issuer=ISSUER,
        model="gpt-5-codex",
        api_key="x",
        use_responses_api=True,
        output_version="responses/v1",
        store=False,
        include=["reasoning.encrypted_content"],
    )
    events = []
    for i in range(4):
        events += [added(i, i), args_done(i, i), item_done(i, i)]
    object.__setattr__(client, "root_client", _FakeRootClient(events))

    before = _FC_STATE.get()
    acc = None
    for gen in client._stream([HumanMessage("hi")]):
        acc = gen.message if acc is None else acc + gen.message

    _assert_calls(acc, [0, 1, 2, 3])
    assert _FC_STATE.get() is before
    assert _CAPTURE_ISSUER.get() == ""
