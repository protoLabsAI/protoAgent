"""Parse helpers in tools.a2a_parse (shared by the delegates a2a adapter).

The peer_consult/peer_list tools were retired (delegate_to over the registry,
ADR 0025); these two helpers stay because the a2a adapter reuses them to read a
reply off an A2A 1.0 SendMessage/GetTask result.
"""

from tools.a2a_parse import _extract_text, _is_terminal


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
