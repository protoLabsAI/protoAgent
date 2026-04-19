"""Integration tests: verify redaction is applied in AuditMiddleware flow."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graph.middleware.redaction import redact


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_message(content: str):
    msg = MagicMock()
    msg.content = content
    return msg


def _make_request(name: str, args: dict):
    req = MagicMock()
    req.tool_call = {"name": name, "args": args}
    return req


# ---------------------------------------------------------------------------
# AuditMiddleware sync integration
# ---------------------------------------------------------------------------

class TestAuditMiddlewareSyncRedaction:
    """Test that _handle_tool_call applies redaction before audit/tracing."""

    def _build_middleware(self):
        from graph.middleware.audit import AuditMiddleware
        return AuditMiddleware()

    def test_bearer_token_in_args_redacted_in_audit(self, tmp_path):
        """Credentials in tool args must not appear in audit.jsonl."""
        audit_file = tmp_path / "audit.jsonl"

        from audit import AuditLogger
        fake_logger = AuditLogger(path=audit_file)

        middleware = self._build_middleware()
        request = _make_request(
            "fetch_data",
            {"Authorization": "Bearer supersecret12345678", "url": "https://example.com"},
        )
        result_msg = _make_tool_message("some plain result")

        with (
            patch("audit.audit_logger", fake_logger),
            patch("tracing.current_session_id", return_value="sess-1"),
            patch("tracing.current_trace_id", return_value="trace-1"),
            patch("tracing.trace_tool_call"),
            patch("metrics.record_tool_call"),
        ):
            middleware._handle_tool_call(request, lambda r: result_msg)

        entries = list(audit_file.read_text().strip().splitlines())
        assert len(entries) == 1
        entry = json.loads(entries[0])
        assert "supersecret12345678" not in json.dumps(entry)
        assert entry["args"]["Authorization"] == "[REDACTED]"

    def test_openai_key_in_result_redacted_in_audit(self, tmp_path):
        """OpenAI keys in tool results must not appear in audit.jsonl."""
        audit_file = tmp_path / "audit.jsonl"

        from audit import AuditLogger
        fake_logger = AuditLogger(path=audit_file)

        middleware = self._build_middleware()
        request = _make_request("get_config", {"section": "openai"})
        secret_key = "sk-TestKeyABCDEFGHIJKLMNOPQR"
        result_msg = _make_tool_message(f"api_key={secret_key}")

        with (
            patch("audit.audit_logger", fake_logger),
            patch("tracing.current_session_id", return_value="sess-2"),
            patch("tracing.current_trace_id", return_value="trace-2"),
            patch("tracing.trace_tool_call"),
            patch("metrics.record_tool_call"),
        ):
            middleware._handle_tool_call(request, lambda r: result_msg)

        entries = list(audit_file.read_text().strip().splitlines())
        entry = json.loads(entries[0])
        assert secret_key not in json.dumps(entry)
        assert "[REDACTED]" in entry["result_summary"]

    def test_redaction_applied_to_langfuse_call(self):
        """trace_tool_call must receive redacted args, not raw credentials."""
        middleware = self._build_middleware()
        request = _make_request(
            "env_fetch",
            {"OPENAI_API_KEY": "sk-real-key-abcdefghijk12345"},
        )
        result_msg = _make_tool_message("ok")

        captured_trace_args = {}

        def fake_trace(tool_name, args, result, duration_ms, success, session_id):
            captured_trace_args.update(args)

        with (
            patch("audit.audit_logger") as fake_audit,
            patch("tracing.current_session_id", return_value="sess-3"),
            patch("tracing.trace_tool_call", side_effect=fake_trace),
            patch("metrics.record_tool_call"),
        ):
            middleware._handle_tool_call(request, lambda r: result_msg)

        assert captured_trace_args.get("OPENAI_API_KEY") == "[REDACTED]"

    def test_non_sensitive_args_preserved(self, tmp_path):
        """Non-credential args must pass through unchanged."""
        audit_file = tmp_path / "audit.jsonl"

        from audit import AuditLogger
        fake_logger = AuditLogger(path=audit_file)

        middleware = self._build_middleware()
        request = _make_request("search", {"query": "Python decorators", "limit": 10})
        result_msg = _make_tool_message("3 results found")

        with (
            patch("audit.audit_logger", fake_logger),
            patch("tracing.current_session_id", return_value="sess-4"),
            patch("tracing.current_trace_id", return_value=""),
            patch("tracing.trace_tool_call"),
            patch("metrics.record_tool_call"),
        ):
            middleware._handle_tool_call(request, lambda r: result_msg)

        entries = list(audit_file.read_text().strip().splitlines())
        entry = json.loads(entries[0])
        assert entry["args"]["query"] == "Python decorators"
        assert entry["result_summary"] == "3 results found"


# ---------------------------------------------------------------------------
# AuditMiddleware async integration
# ---------------------------------------------------------------------------

class TestAuditMiddlewareAsyncRedaction:
    """Test that _ahandle_tool_call applies redaction before audit/tracing."""

    def _build_middleware(self):
        from graph.middleware.audit import AuditMiddleware
        return AuditMiddleware()

    async def test_bearer_token_in_args_redacted_async(self, tmp_path):
        audit_file = tmp_path / "audit.jsonl"

        from audit import AuditLogger
        fake_logger = AuditLogger(path=audit_file)

        middleware = self._build_middleware()
        request = _make_request(
            "async_fetch",
            {"authorization": "Bearer asynctoken9876543210"},
        )
        result_msg = _make_tool_message("result data")

        async def fake_handler(r):
            return result_msg

        with (
            patch("audit.audit_logger", fake_logger),
            patch("tracing.current_session_id", return_value="sess-async-1"),
            patch("tracing.current_trace_id", return_value="trace-async-1"),
            patch("tracing.trace_tool_call"),
            patch("metrics.record_tool_call"),
        ):
            await middleware._ahandle_tool_call(request, fake_handler)

        entries = list(audit_file.read_text().strip().splitlines())
        entry = json.loads(entries[0])
        assert "asynctoken9876543210" not in json.dumps(entry)
        assert entry["args"]["authorization"] == "[REDACTED]"

    async def test_exception_path_redacts_args(self):
        """Even when the tool raises, logged args must be redacted."""
        middleware = self._build_middleware()
        request = _make_request(
            "bad_tool",
            {"LANGFUSE_SECRET_KEY": "lf_secret_key_abc123def456"},
        )

        captured_audit_args = {}

        def fake_log(**kwargs):
            captured_audit_args.update(kwargs.get("args", {}))

        async def raising_handler(r):
            raise ValueError("tool failure")

        with (
            patch("audit.audit_logger") as fake_audit,
            patch("tracing.current_session_id", return_value="sess-exc"),
            patch("tracing.trace_tool_call"),
            patch("metrics.record_tool_call"),
        ):
            fake_audit.log.side_effect = fake_log
            with pytest.raises(ValueError):
                await middleware._ahandle_tool_call(request, raising_handler)

        assert captured_audit_args.get("LANGFUSE_SECRET_KEY") == "[REDACTED]"


# ---------------------------------------------------------------------------
# Standalone redact() contract tests
# ---------------------------------------------------------------------------

def test_redact_returns_valid_json_serializable_values():
    """Ensure [REDACTED] placeholder is JSON-safe."""
    data = {
        "OPENAI_API_KEY": "sk-secret12345678901234",
        "normal": "value",
    }
    result = redact(data)
    serialized = json.dumps(result)
    parsed = json.loads(serialized)
    assert parsed["OPENAI_API_KEY"] == "[REDACTED]"
    assert parsed["normal"] == "value"


def test_redact_does_not_mutate_original():
    """redact() must not modify the input dict in place."""
    original = {"OPENAI_API_KEY": "sk-real-key-1234567890abcdef"}
    _ = redact(original)
    assert original["OPENAI_API_KEY"] == "sk-real-key-1234567890abcdef"
