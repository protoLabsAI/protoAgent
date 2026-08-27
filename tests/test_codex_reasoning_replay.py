"""Encrypted-reasoning replay hygiene + recovery for the Codex Responses path (ADR 0097).

Two halves, matching the two ways a thread gets bricked by a reasoning item the
backend can't verify (400 ``invalid_encrypted_content``):

- ``CodexChatOpenAI`` never SENDS an un-verifiable item.
- ``CodexReasoningReplayRecoveryMiddleware`` heals a thread when the provider
  rejects one anyway (a cross-issuer replay, a rotated credential, a relay).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from graph.middleware.codex_reasoning_replay import (
    CodexReasoningReplayRecoveryMiddleware,
    _replay_disabled,
    carries_replay_state,
    is_invalid_encrypted_content,
    replay_repairs,
    strip_replay_state,
)
from graph.providers.codex_client import CodexChatOpenAI, sanitize_responses_input

# The exact 400 the ChatGPT/Codex backend returns for a replayed reasoning item.
REPORTED_ERROR = (
    "Error code: 400 - {'error': {'message': 'The encrypted content for item "
    "rs_03e86a337ad1e78d016a907f52017087d188417d90ed80226e could not be verified. "
    "Reason: Encrypted content could not be decrypted or parsed.', 'type': "
    "'invalid_request_error', 'param': None, 'code': 'invalid_encrypted_content'}}"
)


@pytest.fixture(autouse=True)
def _clear_session_flags():
    _replay_disabled.clear()
    yield
    _replay_disabled.clear()


def _codex_client() -> CodexChatOpenAI:
    return CodexChatOpenAI(
        model="gpt-5-codex",
        api_key="x",
        use_responses_api=True,
        output_version="v0",
        store=False,
        include=["reasoning.encrypted_content"],
    )


def _streamed_ai_message() -> AIMessage:
    """An assistant turn exactly as langchain-openai's STREAMING Responses path
    leaves it: the reasoning item's ``rs_…`` id survives into additional_kwargs,
    its ``encrypted_content`` never does (captured at ``output_item.added``, and
    ``response.completed`` keeps only usage/metadata)."""
    return AIMessage(
        content=[{"type": "text", "text": "done"}],
        id="msg_1",
        additional_kwargs={
            "reasoning": {
                "type": "reasoning",
                "id": "rs_03e86a337ad1e78d016a907f52017087d188417d90ed80226e",
                "summary": [{"type": "summary_text", "text": "thinking"}],
            }
        },
    )


# ── outbound sanitation ─────────────────────────────────────────────────────────


def test_sanitize_drops_reasoning_item_with_no_blob():
    items = [
        {"type": "message", "role": "user", "content": "hi"},
        {"type": "reasoning", "id": "rs_ghost", "summary": [{"type": "summary_text", "text": "t"}]},
    ]
    assert sanitize_responses_input(items) == [items[0]]


def test_sanitize_keeps_blob_but_drops_the_unresolvable_id():
    items = [{"type": "reasoning", "id": "rs_x", "summary": [], "encrypted_content": "gAAAAblob"}]
    assert sanitize_responses_input(items) == [{"type": "reasoning", "summary": [], "encrypted_content": "gAAAAblob"}]


def test_sanitize_treats_an_empty_blob_as_no_blob():
    assert sanitize_responses_input([{"type": "reasoning", "id": "rs_x", "encrypted_content": ""}]) == []


def test_sanitize_leaves_every_other_item_alone():
    items = [
        {"type": "function_call", "name": "t", "arguments": "{}", "call_id": "c1", "id": "fc_1"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "x"}]},
        "not-a-dict",
    ]
    assert sanitize_responses_input(list(items)) == items


def test_sanitize_passes_through_a_non_list_payload():
    assert sanitize_responses_input(None) is None


def test_codex_client_never_ships_a_ghost_reasoning_item():
    """End to end through the real client: the streamed shape above must not put
    an ``rs_…`` id on the wire. This is the reported 400, reproduced at the seam
    that produced it."""
    payload = _codex_client()._get_request_payload([HumanMessage("hi"), _streamed_ai_message()])

    reasoning = [i for i in payload["input"] if isinstance(i, dict) and i.get("type") == "reasoning"]
    assert reasoning == []
    # …and the turn itself still goes out intact.
    assert any(i.get("role") == "user" for i in payload["input"] if isinstance(i, dict))
    assert any(i.get("role") == "assistant" for i in payload["input"] if isinstance(i, dict))


def test_codex_client_replays_a_real_blob_without_its_id():
    msg = _streamed_ai_message()
    msg.additional_kwargs["reasoning"]["encrypted_content"] = "gAAAAblob"
    payload = _codex_client()._get_request_payload([HumanMessage("hi"), msg])

    reasoning = [i for i in payload["input"] if isinstance(i, dict) and i.get("type") == "reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0]["encrypted_content"] == "gAAAAblob"
    assert "id" not in reasoning[0]  # store=false cannot resolve an item id


def test_codex_client_leaves_a_chat_completions_payload_alone():
    """The sanitizer keys off ``input``, which only the Responses path builds — so
    a client that routes to chat-completions is untouched (the shared
    ``_ReasoningChatOpenAI`` behaviour below this must stay intact)."""
    payload = CodexChatOpenAI(model="gpt-4o-mini", api_key="x")._get_request_payload([HumanMessage("hi")])
    assert "input" not in payload
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


# ── error classification ────────────────────────────────────────────────────────


def test_classifier_matches_the_reported_error():
    assert is_invalid_encrypted_content(RuntimeError(REPORTED_ERROR)) is True


def test_classifier_matches_a_bare_code_attribute():
    exc = RuntimeError("400 bad request")
    exc.code = "invalid_encrypted_content"
    assert is_invalid_encrypted_content(exc) is True


def test_classifier_matches_a_wrapped_body():
    exc = RuntimeError("400 bad request")
    exc.body = {"error": {"code": "invalid_encrypted_content"}}
    assert is_invalid_encrypted_content(exc) is True


def test_classifier_matches_the_alternate_wording():
    assert is_invalid_encrypted_content(RuntimeError("Could not decrypt the provided encrypted_content."))


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 400 - context_length_exceeded",
        "An assistant message with 'tool_calls' must be followed by tool messages",
        "Item with id 'rs_x' not found.",
    ],
)
def test_classifier_ignores_unrelated_errors(message):
    assert is_invalid_encrypted_content(RuntimeError(message)) is False


# ── replay-state helpers ────────────────────────────────────────────────────────


def test_strip_returns_the_whole_list_cleaned():
    messages = [HumanMessage("hi"), _streamed_ai_message()]
    assert carries_replay_state(messages) is True

    stripped = strip_replay_state(messages)
    assert len(stripped) == 2
    assert "reasoning" not in stripped[1].additional_kwargs
    # The original is untouched — the retry must not mutate checkpointed state.
    assert "reasoning" in messages[1].additional_kwargs


def test_repairs_return_only_changed_messages_keeping_their_ids():
    messages = [HumanMessage("hi"), _streamed_ai_message(), AIMessage(content="clean", id="msg_2")]
    repairs = replay_repairs(messages)
    assert [m.id for m in repairs] == ["msg_1"]
    assert "reasoning" not in repairs[0].additional_kwargs


def test_no_replay_state_is_detected_as_such():
    assert carries_replay_state([HumanMessage("hi"), AIMessage(content="x", id="a")]) is False


# ── recovery middleware ─────────────────────────────────────────────────────────


class _Req:
    """Minimal ModelRequest stand-in with the immutable ``override`` contract."""

    def __init__(self, messages, state=None):
        self.messages = messages
        self.state = state or {"session_id": "s1"}

    def override(self, **overrides):
        req = _Req(list(self.messages), dict(self.state))
        for key, value in overrides.items():
            setattr(req, key, value)
        return req


def test_retries_once_with_the_replay_state_stripped():
    calls: list[list] = []

    def handler(request):
        calls.append(request.messages)
        if len(calls) == 1:
            raise RuntimeError(REPORTED_ERROR)
        return "ok"

    req = _Req([HumanMessage("hi"), _streamed_ai_message()])
    assert CodexReasoningReplayRecoveryMiddleware().wrap_model_call(req, handler) == "ok"

    assert len(calls) == 2
    assert "reasoning" in calls[0][1].additional_kwargs  # first attempt sent it
    assert "reasoning" not in calls[1][1].additional_kwargs  # the retry did not


async def test_async_path_retries_the_same_way():
    calls: list[list] = []

    async def handler(request):
        calls.append(request.messages)
        if len(calls) == 1:
            raise RuntimeError(REPORTED_ERROR)
        return "ok"

    req = _Req([HumanMessage("hi"), _streamed_ai_message()])
    result = await CodexReasoningReplayRecoveryMiddleware().awrap_model_call(req, handler)

    assert result == "ok"
    assert "reasoning" not in calls[1][1].additional_kwargs


def test_an_unrelated_error_propagates_untouched():
    def handler(request):
        raise RuntimeError("Error code: 400 - context_length_exceeded")

    req = _Req([HumanMessage("hi"), _streamed_ai_message()])
    with pytest.raises(RuntimeError, match="context_length_exceeded"):
        CodexReasoningReplayRecoveryMiddleware().wrap_model_call(req, handler)
    assert not _replay_disabled  # nothing disabled on someone else's error


def test_no_retry_when_the_history_carries_no_replay_state():
    """The same 400 on a history with nothing to strip means the provider is
    objecting to something we didn't send — retrying would just burn a call."""
    calls = []

    def handler(request):
        calls.append(request)
        raise RuntimeError(REPORTED_ERROR)

    req = _Req([HumanMessage("hi"), AIMessage(content="x", id="a")])
    with pytest.raises(RuntimeError, match="invalid_encrypted_content"):
        CodexReasoningReplayRecoveryMiddleware().wrap_model_call(req, handler)
    assert len(calls) == 1


def test_a_second_failure_after_the_strip_propagates():
    calls = []

    def handler(request):
        calls.append(request)
        raise RuntimeError(REPORTED_ERROR)

    req = _Req([HumanMessage("hi"), _streamed_ai_message()])
    with pytest.raises(RuntimeError, match="invalid_encrypted_content"):
        CodexReasoningReplayRecoveryMiddleware().wrap_model_call(req, handler)
    assert len(calls) == 2  # one retry, then it gives up


def test_before_model_is_a_noop_until_the_session_is_flagged():
    mw = CodexReasoningReplayRecoveryMiddleware()
    state = {"session_id": "s1", "messages": [HumanMessage("hi"), _streamed_ai_message()]}
    assert mw.before_model(state, None) is None


def test_before_model_repairs_the_checkpoint_once_flagged():
    """The retry fixes one call; this is what takes the bad item out of history
    so later turns in the thread stop re-sending it."""
    mw = CodexReasoningReplayRecoveryMiddleware()

    def handler(request):
        raise RuntimeError(REPORTED_ERROR)

    req = _Req([HumanMessage("hi"), _streamed_ai_message()])
    with pytest.raises(RuntimeError):
        mw.wrap_model_call(req, handler)

    state = {"session_id": "s1", "messages": [HumanMessage("hi"), _streamed_ai_message()]}
    update = mw.before_model(state, None)
    assert update is not None
    assert [m.id for m in update["messages"]] == ["msg_1"]  # replaced in place by id
    assert "reasoning" not in update["messages"][0].additional_kwargs


def test_the_flag_is_scoped_to_one_session():
    mw = CodexReasoningReplayRecoveryMiddleware()

    def handler(request):
        raise RuntimeError(REPORTED_ERROR)

    with pytest.raises(RuntimeError):
        mw.wrap_model_call(_Req([_streamed_ai_message()], {"session_id": "s1"}), handler)

    other = {"session_id": "s2", "messages": [_streamed_ai_message()]}
    assert mw.before_model(other, None) is None


# ── placement in the middleware chain ───────────────────────────────────────────


def test_recovery_sits_inside_the_failover_wrapper():
    """Ordering is load-bearing, not cosmetic. ``ObservableModelFallbackMiddleware``
    swallows each attempt's error and re-raises the PRIMARY one, so a recovery
    placed outside it never sees the 400 a fallback attempt caused — and a fallback
    attempt is exactly when a thread's reasoning items meet an endpoint that did not
    mint them. It also stays outside provider shaping so the retry is still shaped
    for the model it lands on."""
    from graph.agent import _build_middleware
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig(api_key="k", model_name="gpt-4o-mini", routing_fallback_models=["gpt-4o"])
    names = [m.__class__.__name__ for m in _build_middleware(cfg, knowledge_store=None)]

    at = names.index("CodexReasoningReplayRecoveryMiddleware")
    assert at > names.index("ObservableModelFallbackMiddleware")
    assert at < names.index("ProviderShapeMiddleware")
    assert at < names.index("MessageCaptureMiddleware")
