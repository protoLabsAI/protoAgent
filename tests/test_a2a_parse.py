"""Parse helpers in tools.a2a_parse (shared by the delegates a2a adapter).

The peer_consult/peer_list tools were retired (delegate_to over the registry,
ADR 0025); these two helpers stay because the a2a adapter reuses them to read a
reply off an A2A 1.0 SendMessage/GetTask result.
"""

import pytest

from tools.a2a_parse import _extract_text, _is_terminal, extract_park_question


def test_extract_text_unwraps_a2a_1_0_task_envelope():
    assert _extract_text({"task": {"artifacts": [{"parts": [{"text": "hello"}]}]}}) == "hello"
    assert _extract_text({"task": {"status": {"message": {"parts": [{"text": "via status"}]}}}}) == "via status"
    assert _extract_text({"artifacts": [{"parts": [{"kind": "text", "text": "legacy"}]}]}) == "legacy"
    assert _extract_text(None) is None
    assert _extract_text({"task": {}}) is None


def test_extract_text_joins_streaming_chunks_without_mid_word_breaks():
    """A peer that streams its reply one part per delta (each delta carrying its own
    leading whitespace) must be reassembled verbatim. Joining on a newline spliced a
    break into every word that fell on a chunk boundary — "Let" + " me" became a
    two-line "Let\\n me" and "didn" + "'t" became "didn\\n't" (#3085)."""
    streamed = {
        "task": {
            "artifacts": [
                {
                    "parts": [
                        {"text": "Let"},
                        {"text": " me"},
                        {"text": " pull"},
                        {"text": " the"},
                        {"text": " logs"},
                    ]
                }
            ]
        }
    }
    out = _extract_text(streamed)
    assert out == "Let me pull the logs"
    assert "\n" not in out  # no delimiter spliced between chunks

    contraction = {"task": {"artifacts": [{"parts": [{"text": "didn"}, {"text": "'t"}]}]}}
    assert _extract_text(contraction) == "didn't"


def test_extract_text_gathers_every_artifact_not_just_the_first():
    """A reply streamed across multiple artifacts must not be truncated to the first
    one (#3085) — every text-bearing artifact contributes, concatenated in order."""
    multi = {
        "task": {
            "artifacts": [
                {"parts": [{"text": "The answer "}]},
                {"parts": [{"text": "spans three "}]},
                {"parts": [{"text": "artifacts."}]},
            ]
        }
    }
    assert _extract_text(multi) == "The answer spans three artifacts."


def test_extract_text_status_message_parts_join_without_newlines():
    """The status.message fallback (a parked peer's question) reassembles its streamed
    parts the same verbatim way — no spurious newline between chunks (#3085)."""
    parked = {
        "task": {"status": {"message": {"parts": [{"text": "Which"}, {"text": " repo"}, {"text": "?"}]}}}
    }
    assert _extract_text(parked) == "Which repo?"


def test_is_terminal_handles_1_0_and_legacy_states():
    assert _is_terminal("TASK_STATE_COMPLETED")
    assert _is_terminal("TASK_STATE_FAILED")
    assert _is_terminal("TASK_STATE_CANCELLED")
    assert _is_terminal("TASK_STATE_REJECTED")
    assert _is_terminal("completed")  # v0.3 lowercase
    assert not _is_terminal("TASK_STATE_WORKING")
    assert not _is_terminal(None)


# Park questions deliberately have different precedence from ordinary answers (#4506).

@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize(
    "artifact,status,expected",
    [
        ("Let me check gh", " Approve shell command?\n\ngh auth status\n", "Approve shell command?\n\ngh auth status"),
        ("\n", "Approve shell command?\n\ngh auth status", "Approve shell command?\n\ngh auth status"),
        (" fallback question \n", " \n", "fallback question"),
        ("\n", " \t", ""),
        ("", "", ""),
    ],
)
def test_extract_park_question(wrapped, artifact, status, expected):
    task = _parked_task(artifact, status)
    result = {"task": task} if wrapped else task
    assert extract_park_question(result) == expected
    # Ordinary answers retain artifact-first order and original whitespace.
    assert _extract_text(result) == (artifact or status or None)


def _parked_task(artifact, status):
    return {
        "id": "park-4506",
        "status": {
            "state": "TASK_STATE_INPUT_REQUIRED",
            "message": {"parts": [{"text": status[:8]}, {"data": {"schema": "hitl-v1"}}, {"text": status[8:]}]},
        },
        "artifacts": [{"parts": [{"text": artifact}]}],
    }


@pytest.mark.parametrize("result", [None, {}, {"task": {}}, {"task": {"status": {"message": None}}}])
def test_extract_park_question_missing(result):
    assert extract_park_question(result) == ""


@pytest.mark.parametrize("question", ["", "\n \t"])
def test_park_message_missing_question_keeps_resume_handle(question):
    from plugins.delegates.a2a import _park_message

    message = _park_message("peer", "park-4506", question)
    assert "Question: (the delegate sent no readable question" in message
    assert "resume_task_id='park-4506'" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["dispatch", "pending", "keyless"])
@pytest.mark.parametrize(
    "artifact,status,expected",
    [
        ("Let me check gh", "Approve shell command?\n\ngh auth status", "Approve shell command?\n\ngh auth status"),
        ("\n", "Approve shell command?\n\ngh auth status", "Approve shell command?\n\ngh auth status"),
        ("\n", " \n", "(the delegate sent no readable question"),
        (" fallback question \n", "", "fallback question"),
    ],
)
async def test_park_paths_use_status_question(monkeypatch, path, artifact, status, expected):
    from unittest.mock import AsyncMock

    from plugins.delegates import conversations, late
    from plugins.delegates.a2a import A2aAdapter
    from plugins.delegates.adapters import Delegate

    d = Delegate(name="peer", type="a2a", url="https://peer.example/a2a")
    task = _parked_task(artifact, status)
    if path == "dispatch":
        rpc = AsyncMock(return_value={"task": task})
        message = await A2aAdapter()._dispatch_traced(
            d, "check auth", send_timeout=1, poll_timeout=1, _rpc=rpc
        )
        assert rpc.await_count == 1
        assert rpc.call_args.args[1] == "SendMessage"
    else:
        get_task = AsyncMock(return_value=task)
        monkeypatch.setattr(late.ADAPTERS["a2a"], "get_task", get_task)
        sleep = AsyncMock()
        if path == "pending":
            conv_id = "conv-4506"
            conversations.remember_pending(conv_id, d.name, d.url, task["id"])
            pending = conversations.pending_for(conv_id, d.name, d.url)
            try:
                outcome, message = await late.collect(d, conv_id, "", pending, sleep=sleep)
                assert conversations.pending_for(conv_id, d.name, d.url) is None
            finally:
                conversations.forget_pending(conv_id, d.name, d.url)
        else:
            outcome, message, state, question = await late.collect_task(d, task["id"], sleep=sleep)
            assert state == "TASK_STATE_INPUT_REQUIRED"
            assert question == extract_park_question(task)
        assert outcome == late.PARKED
        get_task.assert_awaited_once_with(d, task["id"])
    assert f"Question: {expected}" in message
    assert "Let me check gh" not in message
    assert "resume_task_id='park-4506'" in message
