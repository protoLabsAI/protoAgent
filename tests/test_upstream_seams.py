"""Contract tests for the PRIVATE upstream APIs protoAgent reaches into.

protoAgent subclasses langchain's chat models and overrides underscore-prefixed
methods, because the public surface has no seam for the things it must do —
lift a gateway's native reasoning stream, swap Anthropic's `api_key` for an OAuth
`auth_token`, shape a Responses payload for the Codex backend. That is a
deliberate trade (ADR 0097), and the cost of it is that an upstream refactor can
change protoAgent's behavior without changing a line of protoAgent.

The failure mode is what makes these tests worth their weight: when one of these
seams moves, the symptom appears three layers away — a wrong header, a silently
un-hooked override, a request the provider rejects — and the version that changed
is nowhere in the traceback. This week that cost a full debugging cycle when
langchain-openai 1.6 quietly took over an event our code was compensating for.

So each test below pins ONE seam, asserts the BEHAVIOR rather than just the
symbol, and says in its docstring what breaks when it fails. A red test here means
"upstream moved" — go read the changelog for the package named in the failure, not
protoAgent's diff. `python scripts/dep_drift.py` prints what changed.
"""

from __future__ import annotations

import inspect

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils._merge import merge_dicts
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models import base as openai_base


# ── langchain-openai: the chat-completions seams (graph/llm.py) ─────────────────


def test_get_request_payload_is_the_outbound_seam():
    """`_ReasoningChatOpenAI._get_request_payload` (graph/llm.py) round-trips
    `reasoning_content` back out, and the Codex client sanitizes reasoning items
    there. If the name or signature moves, both silently stop running — every
    later turn on a thinking model 400s, and unverifiable reasoning items go back
    on the wire."""
    assert hasattr(ChatOpenAI, "_get_request_payload")
    params = inspect.signature(ChatOpenAI._get_request_payload).parameters
    assert "input_" in params
    assert "stop" in params

    payload = ChatOpenAI(model="gpt-4o-mini", api_key="x")._get_request_payload([HumanMessage("hi")])
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_chunk_converter_is_the_inbound_seam():
    """`_ReasoningChatOpenAI._convert_chunk_to_generation_chunk` (graph/llm.py)
    lifts the gateway's non-OpenAI `reasoning_content` delta so the console can
    render real thinking. If it moves, reasoning silently stops streaming."""
    assert hasattr(ChatOpenAI, "_convert_chunk_to_generation_chunk")
    params = list(inspect.signature(ChatOpenAI._convert_chunk_to_generation_chunk).parameters)
    assert params[1:4] == ["chunk", "default_chunk_class", "base_generation_info"]


def test_stream_dispatch_reaches_a_subclass_override_of_stream_not_stream_responses():
    """`ChatOpenAI._stream` routes the Responses path with
    `super()._stream_responses(...)` — an explicit `super(ChatOpenAI, self)` bind
    that SKIPS any subclass override of `_stream_responses`. Anything that must
    wrap a Responses stream has to hook `_stream`/`_astream` instead.

    This is not hypothetical: the Codex reasoning capture was first written on
    `_stream_responses`, where it was dead code in production while every unit
    test still passed. If upstream ever dispatches on the instance instead, this
    test flips and the workaround can be simplified."""
    reached = []

    class _Probe(ChatOpenAI):
        def _stream_responses(self, *args, **kwargs):
            reached.append("_stream_responses")
            return iter([])

        def _stream(self, *args, **kwargs):
            reached.append("_stream")
            return iter([])

    probe = _Probe(model="gpt-5-codex", api_key="x", use_responses_api=True)
    list(probe._stream([HumanMessage("hi")]))

    assert reached == ["_stream"], (
        "`_stream` is the seam that actually runs. If `_stream_responses` now appears here, "
        "langchain-openai changed its dispatch and graph/providers/codex_client.py can hook "
        "the narrower method again."
    )


# ── langchain-openai: the Responses seams (graph/providers/codex_client.py) ─────


def test_responses_chunk_converter_exists_with_its_index_threading_signature():
    """The Codex reasoning capture wraps this module-level function; there is no
    instance seam for it. If it is renamed or its index-threading contract
    changes, encrypted reasoning stops being captured and stamped — cross-turn
    reasoning continuity silently degrades to none."""
    # `inspect.unwrap` so this pins UPSTREAM's contract even when protoAgent's own
    # capture shim is installed over it — otherwise the assertion quietly becomes a
    # test of our wrapper, and only when some earlier test happened to import it.
    convert = inspect.unwrap(openai_base._convert_responses_chunk_to_generation_chunk)
    params = list(inspect.signature(convert).parameters)
    assert params[:4] == ["chunk", "current_index", "current_output_index", "current_sub_index"]
    assert "output_version" in params


def test_responses_input_replays_reasoning_blocks_in_order_with_their_blobs():
    """Cross-turn reasoning continuity depends on langchain replaying each
    `responses/v1` reasoning block — blob included, in position — rather than
    collapsing them. If this stops holding, `output_version: responses/v1` buys
    nothing and the Codex provider should go back to `v0`."""
    turn = AIMessage(
        content=[
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "BLOB1"},
            {"type": "function_call", "name": "t", "arguments": "{}", "call_id": "c1", "id": "fc_1"},
            {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "BLOB2"},
            {"type": "text", "text": "answer", "id": "msg_1"},
        ],
        id="msg_1",
        response_metadata={"output_version": "responses/v1"},
    )
    items = openai_base._construct_responses_api_input([HumanMessage("hi"), turn])

    blobs = [i.get("encrypted_content") for i in items if isinstance(i, dict) and i.get("type") == "reasoning"]
    assert blobs == ["BLOB1", "BLOB2"]
    kinds = [i.get("type") for i in items if isinstance(i, dict)]
    assert kinds.index("function_call") > kinds.index("reasoning")


# ── langchain-core: merge semantics the streamed shapes depend on ───────────────


def test_equal_strings_concatenate_on_merge_except_for_id():
    """Streamed content blocks merge with `merge_dicts`, which CONCATENATES two
    equal strings for every key but `id`. That is why a synthesized reasoning
    delta must omit `type` — re-sending it would produce `"reasoningreasoning"` —
    while re-sending `id` is safe and is what binds the merge to the right block.
    If this ever changes, that asymmetry in codex_client can go."""
    assert merge_dicts({"k": "a"}, {"k": "b"})["k"] == "ab"
    assert merge_dicts({"id": "rs_1"}, {"id": "rs_1"})["id"] == "rs_1"
    assert merge_dicts({"type": "reasoning"}, {"type": "reasoning"})["type"] == "reasoningreasoning"


def test_message_text_yields_text_blocks_only():
    """Every answer/render site reads `AIMessage.text` rather than `.content`,
    which is what let the Codex provider move to block-shaped content. If `.text`
    ever started including reasoning or tool blocks, the console and every export
    would start leaking them."""
    msg = AIMessage(
        content=[
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "secret"}]},
            {"type": "text", "text": "the answer"},
        ]
    )
    assert msg.text == "the answer"


# ── langchain-anthropic: the OAuth header swap (graph/providers/anthropic_oauth.py) ──


def test_anthropic_client_params_is_a_cached_property_with_an_underlying_func():
    """`_OAuthChatAnthropic._client_params` calls `ChatAnthropic._client_params.func(self)`
    to reach the base implementation past the `cached_property` descriptor, then
    swaps `api_key` for `auth_token`. If it stops being a `cached_property` — or
    is renamed — the OAuth path sends `x-api-key` instead of a Bearer token and
    every subscription request fails auth."""
    ChatAnthropic = pytest.importorskip("langchain_anthropic").ChatAnthropic

    descriptor = inspect.getattr_static(ChatAnthropic, "_client_params")
    assert isinstance(descriptor, type(ChatAnthropic.__dict__.get("_client_params"))), (
        "unexpected descriptor type for ChatAnthropic._client_params"
    )
    assert hasattr(descriptor, "func"), (
        "ChatAnthropic._client_params is no longer a cached_property with `.func` — "
        "graph/providers/anthropic_oauth.py reaches through it to swap api_key for auth_token."
    )
    assert "api_key" in inspect.getsource(descriptor.func)
