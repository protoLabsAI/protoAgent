"""Encrypted-reasoning capture + issuer-scoped replay on the Codex path (ADR 0097).

#3199 stopped protoAgent sending a reasoning item the backend can't verify. This is
the other direction: capture the blob that makes replay possible at all, and replay
it ONLY back to the endpoint that minted it.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_openai.chat_models import base as lc_base
from openai.types.responses import ResponseReasoningItem
from openai.types.responses.response_output_item_added_event import ResponseOutputItemAddedEvent
from openai.types.responses.response_output_item_done_event import ResponseOutputItemDoneEvent
from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent

from graph.providers.codex_client import (
    _CAPTURE_ISSUER,
    ISSUER_KEY,
    CodexChatOpenAI,
    _install_reasoning_capture,
    build_codex_client,
    issuer_fingerprint,
    sanitize_responses_input,
)

ISSUER = issuer_fingerprint("https://chatgpt.com/backend-api/codex", "acct-7")
OTHER_ISSUER = issuer_fingerprint("https://api.openai.com/v1", "acct-7")


@pytest.fixture(autouse=True)
def _capture_installed():
    _install_reasoning_capture()
    yield


def _client(issuer: str = ISSUER) -> CodexChatOpenAI:
    return build_codex_client(
        issuer=issuer,
        model="gpt-5-codex",
        api_key="x",
        use_responses_api=True,
        output_version="responses/v1",
        store=False,
        include=["reasoning.encrypted_content"],
    )


def _reasoning_added(output_index: int, item_id: str):
    return ResponseOutputItemAddedEvent(
        type="response.output_item.added",
        output_index=output_index,
        sequence_number=output_index * 2,
        item=ResponseReasoningItem(id=item_id, type="reasoning", summary=[]),
    )


def _reasoning_done(output_index: int, item_id: str, blob: str | None):
    return ResponseOutputItemDoneEvent(
        type="response.output_item.done",
        output_index=output_index,
        sequence_number=output_index * 2 + 1,
        item=ResponseReasoningItem(id=item_id, type="reasoning", summary=[], encrypted_content=blob),
    )


def _text_delta(output_index: int, text: str):
    return ResponseTextDeltaEvent(
        type="response.output_text.delta",
        output_index=output_index,
        content_index=0,
        item_id="msg_1",
        delta=text,
        sequence_number=99,
        logprobs=[],
    )


def _accumulate(events, *, issuer: str = ISSUER):
    """Drive events through the real converter the way `_stream_responses` does."""
    token = _CAPTURE_ISSUER.set(issuer)
    try:
        idx = out_idx = sub_idx = -1
        acc: AIMessageChunk | None = None
        for event in events:
            idx, out_idx, sub_idx, gen = lc_base._convert_responses_chunk_to_generation_chunk(
                event, idx, out_idx, sub_idx, output_version="responses/v1"
            )
            if gen is not None:
                acc = gen.message if acc is None else acc + gen.message
        return acc
    finally:
        _CAPTURE_ISSUER.reset(token)


# ── capture ─────────────────────────────────────────────────────────────────────


def test_the_blob_lands_on_the_reasoning_block_it_belongs_to():
    """`output_item.done` is the only event carrying encrypted_content, and
    langchain drops it. It must merge onto the block `.added` opened — by index."""
    acc = _accumulate([_reasoning_added(0, "rs_1"), _reasoning_done(0, "rs_1", "BLOB1"), _text_delta(1, "hi")])

    reasoning = [b for b in acc.content if b.get("type") == "reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0]["id"] == "rs_1"
    assert reasoning[0]["encrypted_content"] == "BLOB1"
    assert reasoning[0][ISSUER_KEY] == ISSUER
    # `type` must survive as a plain string — merge_dicts concatenates equal
    # strings for every key but `id`, so re-sending it would give "reasoningreasoning".
    assert reasoning[0]["type"] == "reasoning"
    assert acc.text == "hi"


def test_two_reasoning_items_keep_their_own_blobs_in_order():
    """The v0 slot this replaced could hold only one item per turn; a tool loop
    routinely produces several."""
    acc = _accumulate(
        [
            _reasoning_added(0, "rs_1"),
            _reasoning_done(0, "rs_1", "BLOB1"),
            _reasoning_added(1, "rs_2"),
            _reasoning_done(1, "rs_2", "BLOB2"),
        ]
    )

    reasoning = [b for b in acc.content if b.get("type") == "reasoning"]
    assert [(b["id"], b["encrypted_content"]) for b in reasoning] == [
        ("rs_1", "BLOB1"),
        ("rs_2", "BLOB2"),
    ]


def test_capture_is_inert_for_every_other_client():
    """The wrapper sits on a module-level function shared with the gateway path, so
    'off unless we asked for it' is the whole safety argument. Asserted on the STAMP,
    not the blob: langchain >= 1.6 surfaces the blob on its own, and suppressing that
    for other clients would be a regression, not a safeguard."""
    acc = _accumulate([_reasoning_added(0, "rs_1"), _reasoning_done(0, "rs_1", "BLOB1")], issuer="")

    reasoning = [b for b in acc.content if b.get("type") == "reasoning"]
    assert ISSUER_KEY not in reasoning[0]
    assert not any(k.startswith("_protoagent") for k in reasoning[0])


def test_a_done_event_with_no_blob_adds_nothing():
    acc = _accumulate([_reasoning_added(0, "rs_1"), _reasoning_done(0, "rs_1", None)])
    assert "encrypted_content" not in acc.content[0]


def test_install_is_idempotent():
    first = lc_base._convert_responses_chunk_to_generation_chunk
    _install_reasoning_capture()
    _install_reasoning_capture()
    assert lc_base._convert_responses_chunk_to_generation_chunk is first


# ── issuer fingerprint ──────────────────────────────────────────────────────────


def test_fingerprint_separates_endpoints_and_accounts():
    base = "https://chatgpt.com/backend-api/codex"
    assert issuer_fingerprint(base, "acct-7") != issuer_fingerprint(base, "acct-8")
    assert issuer_fingerprint(base, "acct-7") != issuer_fingerprint("https://api.openai.com/v1", "acct-7")
    assert issuer_fingerprint(base + "/", "acct-7") == issuer_fingerprint(base, " acct-7 ")


def test_fingerprint_does_not_leak_the_account_id():
    """It is checkpointed next to the conversation; a raw account id has no business
    living in that file."""
    fp = issuer_fingerprint("https://chatgpt.com/backend-api/codex", "acct-secret")
    assert "acct-secret" not in fp
    assert len(fp) == 16


# ── replay ──────────────────────────────────────────────────────────────────────


def test_a_blob_from_this_issuer_replays_without_its_id_or_our_private_keys():
    items = [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "encrypted_content": "BLOB1",
            ISSUER_KEY: ISSUER,
        }
    ]
    assert sanitize_responses_input(items, issuer=ISSUER) == [
        {"type": "reasoning", "summary": [], "encrypted_content": "BLOB1"}
    ]


def test_a_blob_from_another_issuer_is_dropped():
    """The exact 400 this whole line of work exists to prevent: a mid-conversation
    model swap replaying a Codex-minted blob at an endpoint that can't decrypt it."""
    items = [{"type": "reasoning", "encrypted_content": "BLOB1", ISSUER_KEY: OTHER_ISSUER}]
    assert sanitize_responses_input(items, issuer=ISSUER) == []


def test_an_unstamped_blob_still_replays():
    """Written before the stamp existed — dropping it would silently break continuity
    on every thread that predates this change."""
    items = [{"type": "reasoning", "encrypted_content": "BLOB1"}]
    assert sanitize_responses_input(items, issuer=ISSUER) == [{"type": "reasoning", "encrypted_content": "BLOB1"}]


def test_a_ghost_is_still_dropped_whatever_its_issuer():
    items = [{"type": "reasoning", "id": "rs_1", "summary": [], ISSUER_KEY: ISSUER}]
    assert sanitize_responses_input(items, issuer=ISSUER) == []


def test_an_unknown_issuer_on_this_client_keeps_everything_verifiable():
    """No fingerprint (an unstamped client) must not become 'drop everything'."""
    items = [{"type": "reasoning", "encrypted_content": "BLOB1", ISSUER_KEY: OTHER_ISSUER}]
    assert len(sanitize_responses_input(items, issuer="")) == 1


def test_end_to_end_capture_then_replay():
    """The whole contract in one pass: what the stream captured is what the next
    turn sends back — blob kept, id and private keys gone."""
    acc = _accumulate([_reasoning_added(0, "rs_1"), _reasoning_done(0, "rs_1", "BLOB1"), _text_delta(1, "hi")])
    turn = AIMessage(content=acc.content, id="msg_1")

    payload = _client()._get_request_payload([HumanMessage("hi"), turn])
    reasoning = [i for i in payload["input"] if isinstance(i, dict) and i.get("type") == "reasoning"]

    assert len(reasoning) == 1
    assert reasoning[0]["encrypted_content"] == "BLOB1"
    assert "id" not in reasoning[0]
    assert ISSUER_KEY not in reasoning[0]
    assert not any(k.startswith("_protoagent") for k in reasoning[0])


def test_end_to_end_a_foreign_thread_sends_no_reasoning_at_all():
    acc = _accumulate([_reasoning_added(0, "rs_1"), _reasoning_done(0, "rs_1", "BLOB1")])
    turn = AIMessage(content=acc.content, id="msg_1")

    payload = _client(issuer=OTHER_ISSUER)._get_request_payload([HumanMessage("hi"), turn])
    assert [i for i in payload["input"] if isinstance(i, dict) and i.get("type") == "reasoning"] == []


def test_a_legacy_v0_turn_in_the_same_thread_still_sends_no_ghost():
    """Threads checkpointed before the flip carry v0-shaped messages; they must keep
    working alongside the new block shape."""
    v0_turn = AIMessage(
        content=[{"type": "text", "text": "old"}],
        id="msg_0",
        additional_kwargs={"reasoning": {"type": "reasoning", "id": "rs_old", "summary": []}},
    )
    payload = _client()._get_request_payload([HumanMessage("hi"), v0_turn])
    assert [i for i in payload["input"] if isinstance(i, dict) and i.get("type") == "reasoning"] == []


# ── the builder wires both halves ───────────────────────────────────────────────


def test_the_builder_stamps_the_live_endpoint_and_account(monkeypatch):
    import graph.providers.openai_codex as ocx
    from graph.config import LangGraphConfig
    from graph.llm import create_llm
    from graph.providers.oauth import CodexOAuthCreds

    monkeypatch.setattr(
        ocx,
        "resolve_codex_oauth",
        lambda *a, **k: CodexOAuthCreds(
            access_token="t",
            account_id="acct-7",
            base_url="https://chatgpt.com/backend-api/codex",
            source="instance_store",
        ),
    )
    llm = create_llm(LangGraphConfig(model_provider="openai-codex", model_name="gpt-5-codex"))

    assert llm.output_version == "responses/v1"
    assert llm._issuer == ISSUER


def test_the_output_version_escape_hatch(monkeypatch):
    """Reverting to v0 costs replay but restores the old content shape, if some
    surface turns out to still assume the string."""
    import graph.providers.openai_codex as ocx
    from graph.config import LangGraphConfig
    from graph.llm import create_llm
    from graph.providers.oauth import CodexOAuthCreds

    monkeypatch.setattr(
        ocx,
        "resolve_codex_oauth",
        lambda *a, **k: CodexOAuthCreds(access_token="t", account_id="a", base_url="b", source="s"),
    )
    monkeypatch.setenv("PROTOAGENT_CODEX_OUTPUT_VERSION", "v0")
    llm = create_llm(LangGraphConfig(model_provider="openai-codex", model_name="gpt-5-codex"))
    assert llm.output_version == "v0"


# ── reasoning never reaches the surfaces that WRITE what they read ──────────────


def test_reasoning_blocks_are_skipped_not_placeholdered():
    """`text_of` feeds exports, session memory and chat bundles — all of which
    persist. ADR 0021: reasoning is never persisted."""
    from graph.message_blocks import text_of

    msg = AIMessage(
        content=[
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "secret"}], "encrypted_content": "B"},
            {"type": "text", "text": "the answer"},
        ]
    )
    assert text_of(msg) == "the answer"


def test_other_non_text_blocks_still_placeholder():
    from graph.message_blocks import text_of

    msg = AIMessage(content=[{"type": "image_url", "image_url": {"url": "x"}}, {"type": "text", "text": "hi"}])
    assert text_of(msg) == "_[image_url]_\n\nhi"


# ── the capture actually arms on the real stream path ───────────────────────────


class _FakeStream:
    """Stands in for `root_client.responses.create(...)` — a context manager over
    a canned Responses event sequence."""

    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, *exc):
        return False


class _FakeResponses:
    def __init__(self, events):
        self._events = events
        self.payloads: list[dict] = []

    def create(self, **payload):
        self.payloads.append(payload)
        return _FakeStream(self._events)


class _FakeRootClient:
    def __init__(self, events):
        self.responses = _FakeResponses(events)


def test_capture_arms_on_the_real_stream_path():
    """Regression: arming was first written on `_stream_responses`, which
    `ChatOpenAI._stream` reaches via `super()._stream_responses(...)` — an explicit
    `super(ChatOpenAI, self)` bind that skips the subclass entirely. The override was
    dead code, and every test that set the contextvar by hand still passed. This one
    drives the client's OWN stream, so nothing sets the contextvar but the client."""
    client = _client()
    events = [
        _reasoning_added(0, "rs_1"),
        _reasoning_done(0, "rs_1", "BLOB1"),
        _text_delta(1, "hi"),
    ]
    object.__setattr__(client, "root_client", _FakeRootClient(events))

    acc = None
    for gen in client._stream([HumanMessage("hi")]):
        acc = gen.message if acc is None else acc + gen.message

    reasoning = [b for b in acc.content if b.get("type") == "reasoning"]
    assert reasoning and reasoning[0]["encrypted_content"] == "BLOB1"
    assert reasoning[0][ISSUER_KEY] == ISSUER
    assert acc.text == "hi"


def test_the_contextvar_is_released_after_the_stream():
    """It gates a process-wide wrapper — leaking it would arm capture for every
    other client in the process."""
    client = _client()
    object.__setattr__(client, "root_client", _FakeRootClient([_text_delta(0, "hi")]))

    list(client._stream([HumanMessage("hi")]))
    assert _CAPTURE_ISSUER.get() == ""
