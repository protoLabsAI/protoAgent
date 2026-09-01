"""Guarded read-only fleet diagnostics tool (#3170, ADR 0071).

Covers the acceptance criteria that are the tool's own:

* config gating — the tool binds to the model ONLY when ``fleet.diagnostics.enabled`` is set;
* roster-only addressing — an unknown/unregistered member is refused and no alternate
  host/URL path is ever constructed;
* the happy path — a rostered member's bounded logs and one exact task, reached through the
  #3168 ``/agents/<slug>/api/diagnostics/*`` API with the fleet service token as operator;
* each failure mode — proxy 409/502/504, 401 unauthorized, 404 missing task, transport
  errors, a missing task id, and a missing server port — returns a compact, actionable body;
* output stays bounded, preserves the server's truncated/malformed signals, and is redacted;
* read-only — the tool only ever issues GET.
"""

from __future__ import annotations

import json

import httpx
import pytest

import graph.fleet.service_token as service_token
import graph.fleet.supervisor as supervisor
import tools.fleet_diagnostics as fd
from graph.config import LangGraphConfig
from runtime.state import STATE
from tools.fleet_diagnostics import fleet_diagnostics
from tools.lg_tools import get_all_tools

_ROSTER = [
    {"id": "host", "name": "protoagent", "running": True},
    {"id": "ws_abc", "name": "researcher", "running": True},
    {"id": "rem_x", "name": "remote-peer", "running": False, "remote": True, "url": "http://peer:7870"},
]


class _FakeResp:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


@pytest.fixture
def wired(monkeypatch):
    """Roster + server port + fleet token stubbed; the HTTP seam is set per-test."""
    monkeypatch.setattr(supervisor, "status", lambda: list(_ROSTER))
    monkeypatch.setattr(STATE, "active_port", 7870, raising=False)
    monkeypatch.setattr(service_token, "resolve_service_token", lambda: "FLEETTOKEN")


def _stub_http(monkeypatch, resp=None, *, raises=None, capture=None):
    async def _fake(url, params, headers):
        if capture is not None:
            capture["url"] = url
            capture["params"] = params
            capture["headers"] = headers
        if raises is not None:
            raise raises
        return resp

    monkeypatch.setattr(fd, "_http_get", _fake)


async def _invoke(**kwargs) -> dict:
    return json.loads(await fleet_diagnostics.ainvoke(kwargs))


# ── r3: configuration gate (model exposure default-off) ───────────────────────


def test_tool_is_not_bound_by_default():
    cfg = LangGraphConfig()
    assert cfg.fleet_diagnostics_enabled is False  # default off
    names = {t.name for t in get_all_tools(graph_config=cfg)}
    assert "fleet_diagnostics" not in names


def test_tool_binds_when_explicitly_enabled():
    cfg = LangGraphConfig()
    cfg.fleet_diagnostics_enabled = True
    names = {t.name for t in get_all_tools(graph_config=cfg)}
    assert "fleet_diagnostics" in names


def test_config_parses_the_enable_flag(tmp_path):
    doc = tmp_path / "cfg.yaml"
    doc.write_text("fleet:\n  diagnostics:\n    enabled: true\n")
    assert LangGraphConfig.from_yaml(str(doc)).fleet_diagnostics_enabled is True


# ── r2: roster-only addressing ────────────────────────────────────────────────


async def test_unknown_member_is_refused(wired, monkeypatch):
    _stub_http(monkeypatch, _FakeResp(200, {"enabled": True}))  # never reached
    out = await _invoke(member="does-not-exist", what="logs")
    assert out["ok"] is False
    assert "no fleet member" in out["error"]
    assert "researcher" in out["error"]  # lists the known roster


async def test_resolves_by_id_and_by_name_to_the_same_slug(wired, monkeypatch):
    cap: dict = {}
    _stub_http(monkeypatch, _FakeResp(200, {"enabled": True, "lines": [], "returned": 0, "capacity": 5}), capture=cap)

    by_name = await _invoke(member="researcher", what="logs")
    assert by_name["member_id"] == "ws_abc"
    assert cap["url"] == "http://127.0.0.1:7870/agents/ws_abc/api/diagnostics/logs"

    by_id = await _invoke(member="ws_abc", what="logs")
    assert by_id["member_id"] == "ws_abc"


# ── r1 + auth: logs and one exact task through the #3168 API ───────────────────


async def test_logs_happy_path_hits_the_diagnostics_proxy_with_the_fleet_token(wired, monkeypatch):
    cap: dict = {}
    payload = {"enabled": True, "capacity": 2000, "returned": 2, "lines": [{"message": "a"}, {"message": "b"}]}
    _stub_http(monkeypatch, _FakeResp(200, payload), capture=cap)

    out = await _invoke(member="researcher", what="logs", lines=50)

    assert out["ok"] is True and out["kind"] == "logs"
    assert out["member"] == "researcher" and out["member_id"] == "ws_abc"
    assert out["returned"] == 2 and len(out["lines"]) == 2
    # roster-only path, exact #3168 endpoint, caller-bounded line selector, operator token.
    assert cap["url"] == "http://127.0.0.1:7870/agents/ws_abc/api/diagnostics/logs"
    assert cap["params"] == {"lines": 50}
    assert cap["headers"]["Authorization"] == "Bearer FLEETTOKEN"


async def test_task_happy_path_targets_the_exact_task(wired, monkeypatch):
    cap: dict = {}
    task = {"task_id": "task-123", "state": "completed", "truncated": ["history"], "malformed": []}
    _stub_http(monkeypatch, _FakeResp(200, task), capture=cap)

    out = await _invoke(member="ws_abc", what="task", task_id="task-123")

    assert out["ok"] is True and out["kind"] == "task"
    assert cap["url"] == "http://127.0.0.1:7870/agents/ws_abc/api/diagnostics/tasks/task-123"
    assert cap["params"] is None
    # server truncation/malformed signals preserved (r4).
    assert out["task"]["truncated"] == ["history"] and out["task"]["malformed"] == []


async def test_task_requires_a_task_id(wired, monkeypatch):
    _stub_http(monkeypatch, _FakeResp(200, {}))
    out = await _invoke(member="ws_abc", what="task", task_id="")
    assert out["ok"] is False and "task_id is required" in out["error"]


async def test_unknown_what_is_rejected(wired, monkeypatch):
    _stub_http(monkeypatch, _FakeResp(200, {}))
    out = await _invoke(member="ws_abc", what="restart")
    assert out["ok"] is False and "unknown 'what'" in out["error"]


# ── r5: every failure mode returns a compact, actionable body (never raises) ──


@pytest.mark.parametrize(
    "status,detail",
    [
        (409, "agent 'researcher' is not running"),
        (502, "agent is not reachable"),
        (504, "agent did not respond in time"),
        (401, "Unauthorized"),
        (503, "task store is not configured on this member"),
    ],
)
async def test_proxy_and_auth_failures_map_to_compact_errors(wired, monkeypatch, status, detail):
    _stub_http(monkeypatch, _FakeResp(status, {"detail": detail}))
    out = await _invoke(member="researcher", what="logs")
    assert out["ok"] is False
    assert out["status"] == status
    assert detail in out["error"]


async def test_missing_task_returns_404_body(wired, monkeypatch):
    _stub_http(monkeypatch, _FakeResp(404, {"detail": "no such task on this member", "task_id": "nope"}))
    out = await _invoke(member="ws_abc", what="task", task_id="nope")
    assert out["ok"] is False and out["status"] == 404
    assert "no such task" in out["error"]


async def test_unreachable_proxy_is_contained(wired, monkeypatch):
    _stub_http(monkeypatch, raises=httpx.ConnectError("boom"))
    out = await _invoke(member="researcher", what="logs")
    assert out["ok"] is False and "unreachable" in out["error"]


async def test_timeout_is_contained(wired, monkeypatch):
    _stub_http(monkeypatch, raises=httpx.ReadTimeout("slow"))
    out = await _invoke(member="researcher", what="logs")
    assert out["ok"] is False and "timed out" in out["error"]


async def test_no_active_port_is_contained(wired, monkeypatch):
    monkeypatch.setattr(STATE, "active_port", None, raising=False)
    _stub_http(monkeypatch, _FakeResp(200, {}))
    out = await _invoke(member="researcher", what="logs")
    assert out["ok"] is False and "no active server port" in out["error"]


async def test_non_json_body_is_contained(wired, monkeypatch):
    _stub_http(monkeypatch, _FakeResp(200, None, text="<html>oops</html>"))
    out = await _invoke(member="researcher", what="logs")
    assert out["ok"] is False and "non-JSON" in out["error"]


# ── r4: bounding, secret redaction ────────────────────────────────────────────


async def test_secrets_in_log_lines_are_redacted(wired, monkeypatch):
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    payload = {"enabled": True, "capacity": 10, "returned": 1, "lines": [{"message": f"key={secret}"}]}
    _stub_http(monkeypatch, _FakeResp(200, payload))
    raw = await fleet_diagnostics.ainvoke({"member": "researcher", "what": "logs"})
    assert secret not in raw
    assert "[REDACTED]" in raw


async def test_output_is_bounded_and_flags_truncation(wired, monkeypatch):
    big = "x" * 500
    lines = [{"message": f"{i} {big}"} for i in range(200)]  # ~100k of content
    _stub_http(monkeypatch, _FakeResp(200, {"enabled": True, "capacity": 2000, "returned": 200, "lines": lines}))
    raw = await fleet_diagnostics.ainvoke({"member": "researcher", "what": "logs", "lines": 1000})
    assert len(raw) <= fd._MAX_OUTPUT_CHARS
    out = json.loads(raw)  # still valid JSON after trimming
    assert out["output_truncated"] is True


async def test_lines_selector_is_clamped(wired, monkeypatch):
    cap: dict = {}
    _stub_http(monkeypatch, _FakeResp(200, {"enabled": True, "lines": [], "returned": 0, "capacity": 5}), capture=cap)
    await _invoke(member="researcher", what="logs", lines=999999)
    assert cap["params"]["lines"] == fd._MAX_LINES  # clamped to the ceiling, not sent verbatim


# ── r6: read-only (only ever issues GET) ──────────────────────────────────────


async def test_tool_only_ever_issues_get(wired, monkeypatch):
    seen: list[str] = []

    class _RecordingClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            seen.append("GET")
            return _FakeResp(200, {"enabled": True, "capacity": 5, "returned": 0, "lines": []})

        async def _mutate(self, *a, **k):
            raise AssertionError("fleet_diagnostics must be read-only — it issued a mutating request")

        post = put = patch = delete = _mutate

    # Swap ONLY AsyncClient on the real httpx module (exception classes stay intact) and let
    # the real _http_get run, so this exercises the tool's actual transport, not a stub.
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    out = await _invoke(member="researcher", what="logs")
    assert out["ok"] is True
    assert seen == ["GET"]
