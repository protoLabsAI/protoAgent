"""Tests for tool-output transcript redaction (#3070).

Verifies that AuditMiddleware.wrap_tool_call / awrap_tool_call applies
redact() to ToolMessage.content before returning the result to the graph,
and that the security.redact_tool_output config flag gates it.
"""

import json
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(content):
    msg = MagicMock()
    msg.content = content
    return msg


def _make_request(name="test_tool", args=None):
    req = MagicMock()
    req.tool_call = {"name": name, "args": args or {}}
    return req


def _build_middleware():
    from graph.middleware.audit import AuditMiddleware

    return AuditMiddleware()


def _mock_config(redact_tool_output: bool):
    cfg = MagicMock()
    cfg.security_redact_tool_output = redact_tool_output
    return cfg


# ---------------------------------------------------------------------------
# r1: credential patterns in tool output are redacted in the transcript
# ---------------------------------------------------------------------------


def test_openai_key_in_content_redacted():
    """r1: OPENAI_API_KEY=sk-... in content → [REDACTED] reaches the model."""
    middleware = _build_middleware()
    secret = "sk-test12345678901234567890"
    result = _make_result(f"OPENAI_API_KEY={secret}")

    with (
        patch("observability.audit.audit_logger"),
        patch("observability.tracing.current_session_id", return_value="sess-1"),
        patch("observability.tracing.trace_tool_call"),
        patch("observability.metrics.record_tool_call"),
        patch("graph.sdk.STATE") as mock_state,
    ):
        mock_state.graph_config = _mock_config(redact_tool_output=True)
        returned = middleware._handle_tool_call(_make_request(), lambda r: result)

    assert secret not in returned.content
    assert "[REDACTED]" in returned.content


def test_github_token_in_content_redacted():
    """r1: ghp_ token in content → [REDACTED] reaches the model."""
    middleware = _build_middleware()
    token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    result = _make_result(f"Found token: {token}")

    with (
        patch("observability.audit.audit_logger"),
        patch("observability.tracing.current_session_id", return_value="sess-2"),
        patch("observability.tracing.trace_tool_call"),
        patch("observability.metrics.record_tool_call"),
        patch("graph.sdk.STATE") as mock_state,
    ):
        mock_state.graph_config = _mock_config(redact_tool_output=True)
        returned = middleware._handle_tool_call(_make_request(), lambda r: result)

    assert token not in returned.content
    assert "[REDACTED]" in returned.content


# ---------------------------------------------------------------------------
# r2: secrets-manager values are redacted
# ---------------------------------------------------------------------------


def test_secrets_manager_value_redacted():
    """r2: a value known to the secrets manager is exact-matched and redacted."""
    middleware = _build_middleware()
    managed_secret = "my-very-secret-manager-value-xyz"
    result = _make_result(f"Config retrieved: {managed_secret}")

    with (
        patch("observability.audit.audit_logger"),
        patch("observability.tracing.current_session_id", return_value="sess-3"),
        patch("observability.tracing.trace_tool_call"),
        patch("observability.metrics.record_tool_call"),
        patch("graph.sdk.STATE") as mock_state,
        patch("infra.secrets.sensitive_values", return_value=frozenset({managed_secret})),
    ):
        mock_state.graph_config = _mock_config(redact_tool_output=True)
        returned = middleware._handle_tool_call(_make_request(), lambda r: result)

    assert managed_secret not in returned.content
    assert "[REDACTED]" in returned.content


# ---------------------------------------------------------------------------
# r3: audit log continues to receive redacted output regardless of toggle
# ---------------------------------------------------------------------------


def test_audit_always_receives_redacted_output(tmp_path):
    """r3: audit log is redacted whether or not transcript redaction is on."""
    from observability.audit import AuditLogger

    audit_file = tmp_path / "audit.jsonl"
    fake_logger = AuditLogger(path=audit_file)

    middleware = _build_middleware()
    secret = "sk-auditcheck12345678901234567890"
    result = _make_result(f"OPENAI_API_KEY={secret}")

    with (
        patch("observability.audit.audit_logger", fake_logger),
        patch("observability.tracing.current_session_id", return_value="sess-audit"),
        patch("observability.tracing.trace_tool_call"),
        patch("observability.metrics.record_tool_call"),
        patch("graph.sdk.STATE") as mock_state,
    ):
        # Transcript redaction OFF — audit must still be redacted.
        mock_state.graph_config = _mock_config(redact_tool_output=False)
        middleware._handle_tool_call(_make_request(), lambda r: result)

    entries = list(audit_file.read_text().strip().splitlines())
    assert len(entries) == 1
    entry = json.loads(entries[0])
    assert secret not in json.dumps(entry), "audit log must not contain the raw secret"
    assert "[REDACTED]" in entry["result_summary"]


# ---------------------------------------------------------------------------
# r4 / r7: security.redact_tool_output=false disables transcript redaction
# ---------------------------------------------------------------------------


def test_redact_tool_output_false_skips_transcript_redaction():
    """r4/r7: with redact_tool_output=False, content reaches the model unredacted."""
    middleware = _build_middleware()
    raw_content = "OPENAI_API_KEY=sk-shouldnotberedacted1234567890"
    result = _make_result(raw_content)

    with (
        patch("observability.audit.audit_logger"),
        patch("observability.tracing.current_session_id", return_value="sess-4"),
        patch("observability.tracing.trace_tool_call"),
        patch("observability.metrics.record_tool_call"),
        patch("graph.sdk.STATE") as mock_state,
    ):
        mock_state.graph_config = _mock_config(redact_tool_output=False)
        returned = middleware._handle_tool_call(_make_request(), lambda r: result)

    # Transcript content is NOT redacted when the flag is False.
    assert returned.content == raw_content


# ---------------------------------------------------------------------------
# Non-string content must not be mutated
# ---------------------------------------------------------------------------


def test_non_string_content_not_mutated():
    """List / multipart content must pass through untouched (no type error, no mutation)."""
    middleware = _build_middleware()
    multipart = [{"type": "text", "text": "some output"}]
    result = _make_result(multipart)

    with (
        patch("observability.audit.audit_logger"),
        patch("observability.tracing.current_session_id", return_value="sess-5"),
        patch("observability.tracing.trace_tool_call"),
        patch("observability.metrics.record_tool_call"),
        patch("graph.sdk.STATE") as mock_state,
    ):
        mock_state.graph_config = _mock_config(redact_tool_output=True)
        returned = middleware._handle_tool_call(_make_request(), lambda r: result)

    # Non-string content is left exactly as-is — same object, not a copy.
    assert returned.content is multipart


# ---------------------------------------------------------------------------
# Async path mirrors the sync path
# ---------------------------------------------------------------------------


async def test_async_path_redacts_credentials():
    """Async _ahandle_tool_call also applies transcript redaction."""
    middleware = _build_middleware()
    secret = "sk-asynctest12345678901234567890"
    result = _make_result(f"api_key={secret}")

    async def fake_handler(r):
        return result

    with (
        patch("observability.audit.audit_logger"),
        patch("observability.tracing.current_session_id", return_value="sess-async"),
        patch("observability.tracing.trace_tool_call"),
        patch("observability.metrics.record_tool_call"),
        patch("graph.sdk.STATE") as mock_state,
    ):
        mock_state.graph_config = _mock_config(redact_tool_output=True)
        returned = await middleware._ahandle_tool_call(_make_request(), fake_handler)

    assert secret not in returned.content
    assert "[REDACTED]" in returned.content


# ---------------------------------------------------------------------------
# r5: OPENAI_API_KEY=sk-test... combo (integration)
# ---------------------------------------------------------------------------


def test_r5_env_var_and_openai_key_pattern():
    """r5: OPENAI_API_KEY=sk-test... is redacted (env-var and key patterns both fire)."""
    middleware = _build_middleware()
    result = _make_result("OPENAI_API_KEY=sk-test12345678901234567890")

    with (
        patch("observability.audit.audit_logger"),
        patch("observability.tracing.current_session_id", return_value="sess-r5"),
        patch("observability.tracing.trace_tool_call"),
        patch("observability.metrics.record_tool_call"),
        patch("graph.sdk.STATE") as mock_state,
    ):
        mock_state.graph_config = _mock_config(redact_tool_output=True)
        returned = middleware._handle_tool_call(_make_request(), lambda r: result)

    assert "sk-test12345678901234567890" not in returned.content
    assert "[REDACTED]" in returned.content


# ---------------------------------------------------------------------------
# r6: secrets-manager value exact-matched (standalone)
# ---------------------------------------------------------------------------


def test_r6_secrets_manager_exact_match():
    """r6: a value fetched from the secrets manager is exact-matched in content."""
    middleware = _build_middleware()
    managed = "xoxb-shape-agnostic-secret-12345"
    result = _make_result(f"token: {managed}")

    with (
        patch("observability.audit.audit_logger"),
        patch("observability.tracing.current_session_id", return_value="sess-r6"),
        patch("observability.tracing.trace_tool_call"),
        patch("observability.metrics.record_tool_call"),
        patch("graph.sdk.STATE") as mock_state,
        patch("infra.secrets.sensitive_values", return_value=frozenset({managed})),
    ):
        mock_state.graph_config = _mock_config(redact_tool_output=True)
        returned = middleware._handle_tool_call(_make_request(), lambda r: result)

    assert managed not in returned.content
    assert "[REDACTED]" in returned.content
