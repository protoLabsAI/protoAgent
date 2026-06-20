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


def test_is_terminal_handles_1_0_and_legacy_states():
    assert _is_terminal("TASK_STATE_COMPLETED")
    assert _is_terminal("TASK_STATE_FAILED")
    assert _is_terminal("TASK_STATE_CANCELLED")
    assert _is_terminal("TASK_STATE_REJECTED")
    assert _is_terminal("completed")  # v0.3 lowercase
    assert not _is_terminal("TASK_STATE_WORKING")
    assert not _is_terminal(None)
