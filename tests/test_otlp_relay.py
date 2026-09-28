"""The local OTLP relay between a coding agent's native spans and Langfuse (#3742)."""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from observability import otlp_relay, tracing
from plugins.coding_agent.acp_client import AcpClient

SPAWN_TRACE, SPAWN_SPAN = "a" * 32, "b" * 16
TURN2_TRACE, TURN2_SPAN = "c" * 32, "d" * 16


def _kv(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _request(*spans: dict) -> ExportTraceServiceRequest:
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.extend([_kv("service.name", "coder"), _kv("user.email", "me@example.com")])
    ss = rs.scope_spans.add()
    for spec in spans:
        sp = ss.spans.add()
        sp.name = spec["name"]
        sp.trace_id = bytes.fromhex(spec.get("trace", SPAWN_TRACE))
        sp.span_id = bytes.fromhex(spec.get("span", "1" * 16))
        sp.parent_span_id = bytes.fromhex(spec.get("parent", SPAWN_SPAN))
        sp.start_time_unix_nano = spec.get("start", 100)
        sp.attributes.extend([_kv(k, v) for k, v in spec.get("attrs", {}).items()])
    return req


def _attrs(span) -> dict:
    return {
        a.key: (a.value.int_value if a.value.HasField("int_value") else a.value.string_value) for a in span.attributes
    }


@pytest.fixture
def tracing_on(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "_langfuse", MagicMock())
    monkeypatch.setattr(tracing, "_export_target", ("https://lf.example", "pk-lf-x", "sk-lf-y"))


def test_identity_is_dropped_and_usage_mapped():
    req = _request(
        {
            "name": "claude_code.llm_request",
            "attrs": {
                "user.email": "me@example.com",
                "organization.id": "org-1",
                "user.account_uuid": "u-1",
                "model": "claude-haiku",
                "input_tokens": "10",
                "output_tokens": "245",
                "cache_read_tokens": "16491",
            },
        }
    )
    otlp_relay.rewrite(req, None)

    rs = req.resource_spans[0]
    assert [a.key for a in rs.resource.attributes] == ["service.name"]
    attrs = _attrs(rs.scope_spans[0].spans[0])
    assert "user.email" not in attrs and "organization.id" not in attrs and "user.account_uuid" not in attrs
    assert attrs["gen_ai.usage.input_tokens"] == 10 and attrs["gen_ai.usage.output_tokens"] == 245
    assert attrs["gen_ai.usage.cache_read_input_tokens"] == 16491
    assert attrs["model"] == "claude-haiku"


def test_string_attributes_are_redacted_as_a_backstop():
    secret = "sk-" + "Q" * 40
    req = _request({"name": "claude_code.tool", "attrs": {"full_command": f"echo {secret}"}})
    otlp_relay.rewrite(req, None)
    assert "Q" * 40 not in _attrs(req.resource_spans[0].scope_spans[0].spans[0])["full_command"]


def test_a_later_turns_spans_move_into_that_turns_trace():
    """claude-agent-acp keeps one process per session, so turn 2's spans still carry the
    spawn trace. The relay moves them by start time, and re-parents the interaction root."""
    client = otlp_relay._Client(bytes.fromhex(SPAWN_TRACE), bytes.fromhex(SPAWN_SPAN))
    client.turns = [
        otlp_relay._Turn(100, bytes.fromhex(SPAWN_TRACE), bytes.fromhex(SPAWN_SPAN)),
        otlp_relay._Turn(500, bytes.fromhex(TURN2_TRACE), bytes.fromhex(TURN2_SPAN)),
    ]
    req = _request(
        {"name": "turn1.interaction", "start": 150, "span": "1" * 16},
        {"name": "turn2.interaction", "start": 550, "span": "2" * 16},
        {"name": "turn2.llm_request", "start": 560, "span": "3" * 16, "parent": "2" * 16},
    )
    otlp_relay.rewrite(req, client)
    t1, t2, t2_child = req.resource_spans[0].scope_spans[0].spans

    assert t1.trace_id.hex() == SPAWN_TRACE and t1.parent_span_id.hex() == SPAWN_SPAN
    assert t2.trace_id.hex() == TURN2_TRACE and t2.parent_span_id.hex() == TURN2_SPAN
    assert t2_child.trace_id.hex() == TURN2_TRACE and t2_child.parent_span_id.hex() == "2" * 16


def test_the_relay_forwards_with_this_process_credentials(tracing_on, monkeypatch):
    sent = {}

    def fake_post(url, content, headers, timeout):
        sent.update(url=url, headers=headers, body=content)
        return MagicMock(status_code=200, content=b"ok")

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    token, _port = otlp_relay.register(SPAWN_TRACE, SPAWN_SPAN)
    try:
        body = _request({"name": "claude_code.interaction", "attrs": {"user.email": "me@example.com"}})
        status, _ = otlp_relay.handle(token, body.SerializeToString())
    finally:
        otlp_relay.unregister(token)

    assert status == 200
    assert sent["url"] == "https://lf.example/api/public/otel/v1/traces"
    assert sent["headers"]["Authorization"].startswith("Basic ")
    forwarded = ExportTraceServiceRequest()
    forwarded.ParseFromString(sent["body"])
    assert "user.email" not in _attrs(forwarded.resource_spans[0].scope_spans[0].spans[0])


def test_an_unknown_token_is_refused(tracing_on):
    assert otlp_relay.handle("not-a-token", b"")[0] == 404


def test_nothing_registers_while_tracing_is_off(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    assert otlp_relay.register(SPAWN_TRACE, SPAWN_SPAN) is None


def test_unregister_can_wait_for_the_exit_flush(tracing_on):
    token, _ = otlp_relay.register(SPAWN_TRACE, SPAWN_SPAN)
    done = threading.Event()
    otlp_relay.unregister(token, delay=0.05)
    assert token in otlp_relay._clients  # still accepting its last spans
    done.wait(0.3)
    assert token not in otlp_relay._clients


# ─── AcpClient: when native tracing turns on ───────────────────────────────────────────


def _client(command: str, **kw) -> AcpClient:
    return AcpClient(command, [], cwd=".", name="opus", record_runs=False, **kw)


def _in_span():
    from opentelemetry.sdk.trace import TracerProvider

    return TracerProvider().get_tracer("langfuse-sdk").start_as_current_span("acp:opus")


def test_a_claude_code_agent_gets_the_relay_env_inside_a_traced_run(tracing_on):
    client = _client("/usr/local/bin/claude-agent-acp")
    with _in_span():
        env = client._native_tracing_env()
    try:
        assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1" and env["OTEL_TRACES_EXPORTER"] == "otlp"
        assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"].startswith("http://127.0.0.1:")
        assert env["TRACEPARENT"].startswith("00-") and len(env["TRACEPARENT"]) == 55
        assert "sk-lf" not in " ".join(env.values())  # the agent never sees the credentials
        assert client._relay_token is not None
    finally:
        client._drop_relay()


def test_other_agents_and_the_opt_out_get_nothing(tracing_on, monkeypatch):
    with _in_span():
        assert _client(sys.executable)._native_tracing_env() == {}
        monkeypatch.setenv("PROTOAGENT_ACP_NATIVE_TRACING", "0")
        assert _client("/usr/local/bin/claude-agent-acp")._native_tracing_env() == {}
        # An explicit opt-in wins for an agent the auto-detection doesn't know.
        forced = _client(sys.executable, native_tracing=True)
        assert forced._native_tracing_env()
        forced._drop_relay()


def test_no_relay_outside_a_traced_run(tracing_on):
    assert _client("/usr/local/bin/claude-agent-acp")._native_tracing_env() == {}


async def test_with_native_spans_the_turn_usage_is_metadata_not_a_second_generation(tmp_path, monkeypatch):
    """The relay delivers per-call generations with usage + cost; a turn-level generation
    from the agent-reported totals too would count the spend twice."""
    from tests.test_acp_trace_span import _SCRIPTED_AGENT, _msg

    fake, span = MagicMock(), MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=None)
    fake.start_as_current_observation.return_value = cm
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)

    def pretend_native(self):
        self._relay_token = "tok"
        return {}

    monkeypatch.setattr(AcpClient, "_native_tracing_env", pretend_native)
    monkeypatch.setattr(AcpClient, "_drop_relay", lambda self: None)
    handed: list = []
    monkeypatch.setattr(otlp_relay, "end_turn", lambda *a, **k: handed.append(a))
    monkeypatch.setattr("plugins.coding_agent.acp_client._current_span_ids", lambda: ("a" * 32, "b" * 16))

    import json as _json

    script = tmp_path / "agent.py"
    script.write_text(_SCRIPTED_AGENT, encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(
        _json.dumps(
            {
                "updates": [
                    _msg("done"),
                    {"sessionUpdate": "usage_update", "used": 1, "size": 2, "cost": {"amount": 0.3}},
                ],
                "prompt_result": {
                    "stopReason": "end_turn",
                    "usage": {"inputTokens": 5, "outputTokens": 7, "totalTokens": 12},
                },
            }
        ),
        encoding="utf-8",
    )
    client = AcpClient(sys.executable, [str(script), str(spec)], cwd=str(tmp_path), name="opus", record_runs=False)
    try:
        await client.prompt("go", timeout=30.0)
    finally:
        await client.close()

    fake.start_observation.assert_not_called()  # no turn-level generation
    (token, span_id, _end, fallback) = handed[0]  # …handed to the relay as a fallback instead
    assert token == "tok" and span_id == "b" * 16
    assert fallback["usage"]["total"] == 12 and fallback["cost_usd"] == pytest.approx(0.3)
    md = span.update.call_args.kwargs["metadata"]
    assert md["native_tracing"] is True
    assert md["reported_cost_usd"] == pytest.approx(0.3) and md["reported_usage"]["totalTokens"] == 12


def test_a_second_run_in_the_same_trace_goes_under_its_own_span():
    """Two delegate_to calls to one pooled coder inside one orchestrator turn share a
    trace id; run 2's spans still belong under run 2's span."""
    client = otlp_relay._Client(bytes.fromhex(SPAWN_TRACE), bytes.fromhex(SPAWN_SPAN))
    client.turns = [
        otlp_relay._Turn(100, bytes.fromhex(SPAWN_TRACE), bytes.fromhex(SPAWN_SPAN)),
        otlp_relay._Turn(500, bytes.fromhex(SPAWN_TRACE), bytes.fromhex(TURN2_SPAN)),
    ]
    req = _request({"name": "run2.interaction", "start": 550})
    otlp_relay.rewrite(req, client)
    span = req.resource_spans[0].scope_spans[0].spans[0]
    assert span.trace_id.hex() == SPAWN_TRACE and span.parent_span_id.hex() == TURN2_SPAN


def test_identity_and_secrets_are_stripped_at_every_level():
    secret = "sk-" + "Q" * 40
    req = _request({"name": f"tool {secret}", "attrs": {"list": "x"}})
    rs = req.resource_spans[0]
    ss = rs.scope_spans[0]
    ss.scope.attributes.extend([_kv("user.email", "me@example.com")])
    sp = ss.spans[0]
    sp.status.message = f"failed with {secret}"
    ev = sp.events.add()
    ev.name = "tool.output"
    ev.attributes.extend([_kv("user.email", "me@example.com"), _kv("output", secret)])
    ln = sp.links.add()
    ln.attributes.extend([_kv("organization.id", "org-1")])
    arr = sp.attributes.add()
    arr.key = "items"
    arr.value.array_value.values.add().string_value = secret
    kv = sp.attributes.add()
    kv.key = "nested"
    kv.value.kvlist_value.values.extend([_kv("cmd", secret)])
    user = sp.attributes.add()  # an identity key nested as user → {email: …}
    user.key = "user"
    user.value.kvlist_value.values.extend([_kv("email", "me@example.com")])

    otlp_relay.rewrite(req, None)
    dumped = str(req)
    assert "me@example.com" not in dumped and "org-1" not in dumped and "Q" * 40 not in dumped


def test_a_caller_without_a_token_gets_nothing_before_the_body_is_read(tracing_on):
    import http.client

    token, port = otlp_relay.register(SPAWN_TRACE, SPAWN_SPAN)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        # Claims a huge body but never sends it: without the token check first this would
        # sit in rfile.read() until the socket timeout.
        conn.putrequest("POST", "/not-a-token/v1/traces")
        conn.putheader("Content-Length", "8000000")
        conn.endheaders()
        assert conn.getresponse().status == 404
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", f"/{token}/v1/traces", body=b"x", headers={"Content-Length": "abc"})
        assert conn.getresponse().status == 400
    finally:
        otlp_relay.unregister(token)


def test_a_gzip_export_is_accepted(tracing_on, monkeypatch):
    import gzip
    import http.client

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: MagicMock(status_code=200, content=b""))
    token, port = otlp_relay.register(SPAWN_TRACE, SPAWN_SPAN)
    try:
        body = gzip.compress(_request({"name": "claude_code.interaction"}).SerializeToString())
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", f"/{token}/v1/traces", body=body, headers={"Content-Encoding": "gzip"})
        assert conn.getresponse().status == 200
    finally:
        otlp_relay.unregister(token)


def test_a_delegate_with_its_own_telemetry_config_is_left_alone(tracing_on):
    client = _client("/usr/local/bin/claude-agent-acp", env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"})
    with _in_span():
        assert client._native_tracing_env() == {}


# ─── the fallback: agent-reported totals only when no native spans arrived ─────────────


def _turn_client(token_turn_span: str = SPAWN_SPAN):
    token, _ = otlp_relay.register(SPAWN_TRACE, SPAWN_SPAN)
    otlp_relay.begin_turn(token, SPAWN_TRACE, SPAWN_SPAN, 100)
    return token


def test_delivered_model_calls_are_counted_per_turn(tracing_on, monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: MagicMock(status_code=200, content=b""))
    token = _turn_client()
    try:
        otlp_relay.handle(token, _request({"name": "claude_code.interaction", "start": 150}).SerializeToString())
        body = _request({"name": "claude_code.llm_request", "start": 160, "attrs": {"input_tokens": "3"}})
        otlp_relay.handle(token, body.SerializeToString())
        assert otlp_relay._clients[token].turns[0].model_calls == 1
    finally:
        otlp_relay.unregister(token)


def test_the_fallback_is_recorded_when_no_native_span_arrived(tracing_on, monkeypatch):
    import httpx

    posted = []
    monkeypatch.setattr(httpx, "post", lambda url, content, **k: posted.append(content) or MagicMock(status_code=200))
    token = _turn_client()
    try:
        fallback = {
            "name": "acp:opus-model",
            "service": "x",
            "model": "opus",
            "usage": {"input": 5, "total": 9},
            "cost_usd": 0.2,
        }
        otlp_relay.end_turn(token, SPAWN_SPAN, 900, fallback, grace=0.05)
        threading.Event().wait(0.3)
    finally:
        otlp_relay.unregister(token)

    (body,) = posted
    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    span = req.resource_spans[0].scope_spans[0].spans[0]
    attrs = _attrs(span)
    assert span.trace_id.hex() == SPAWN_TRACE and span.parent_span_id.hex() == SPAWN_SPAN
    assert span.name == "acp:opus-model" and attrs["langfuse.observation.type"] == "generation"
    assert attrs["langfuse.observation.model.name"] == "opus"
    assert '"total": 9' in attrs["langfuse.observation.usage_details"]
    assert '"total": 0.2' in attrs["langfuse.observation.cost_details"]


def test_no_fallback_when_the_native_spans_arrived(tracing_on, monkeypatch):
    import httpx

    posted = []
    monkeypatch.setattr(
        httpx, "post", lambda url, content, **k: posted.append(content) or MagicMock(status_code=200, content=b"")
    )
    token = _turn_client()
    try:
        body = _request({"name": "claude_code.llm_request", "start": 160, "attrs": {"output_tokens": "4"}})
        otlp_relay.handle(token, body.SerializeToString())
        posted.clear()  # that was the forwarded span itself
        otlp_relay.end_turn(
            token, SPAWN_SPAN, 900, {"model": "opus", "usage": {"total": 9}, "cost_usd": 0.2}, grace=0.05
        )
        threading.Event().wait(0.3)
    finally:
        otlp_relay.unregister(token)
    assert posted == []
