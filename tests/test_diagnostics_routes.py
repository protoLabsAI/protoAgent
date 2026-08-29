"""Member-local diagnostics reads (#3168) — bounded logs + exact A2A task inspection.

Covers the acceptance criteria that are this module's own: output bounds, task shapes,
local failure containment (bad limits, unknown task, malformed row, no store), redaction,
and the POSITIONAL auth guarantee that ``/api/diagnostics/*`` is operator-only and denied
to a federation credential.

A stopped/unreachable/slow MEMBER is the proxy's containment case, not this module's —
``graph/fleet/proxy.py`` already answers 409/502/504 and is tested there.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.diagnostics_routes import register_diagnostics_routes


def _client() -> TestClient:
    app = FastAPI()
    register_diagnostics_routes(app)
    return TestClient(app)


# ── logs ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def ring(monkeypatch):
    """A configured ring buffer, isolated from whatever the process already logged."""
    from observability import logging_config

    buf = logging_config.RingBufferHandler(50)
    monkeypatch.setattr(logging_config, "_ring", buf, raising=False)
    return buf


def _emit(buf, message: str, *, level: str = "INFO", logger: str = "probe") -> None:
    record = logging.LogRecord(logger, getattr(logging, level), __file__, 1, message, None, None)
    buf.emit(record)


def test_logs_returns_bounded_tail(ring):
    for i in range(10):
        _emit(ring, f"line {i}")
    body = _client().get("/api/diagnostics/logs?lines=3").json()
    assert body["enabled"] is True
    assert body["returned"] == 3
    assert [r["message"] for r in body["lines"]] == ["line 7", "line 8", "line 9"]
    assert body["capacity"] == 50


def test_logs_records_carry_level_and_logger(ring):
    _emit(ring, "boom", level="ERROR", logger="graph.agent")
    row = _client().get("/api/diagnostics/logs").json()["lines"][-1]
    assert row["level"] == "ERROR"
    assert row["logger"] == "graph.agent"
    assert row["ts"].endswith("+00:00")


@pytest.mark.parametrize(
    ("query", "expected_returned"),
    [("lines=0", 1), ("lines=-5", 1), ("lines=99999", 5), ("lines=abc", 5)],
)
def test_logs_clamps_invalid_limits_instead_of_failing(ring, query, expected_returned):
    """A nonsense ``lines`` is clamped to the nearest useful answer, never a 4xx/5xx —
    and the response SAYS it was adjusted so a UI can surface that."""
    for i in range(5):
        _emit(ring, f"line {i}")
    resp = _client().get(f"/api/diagnostics/logs?{query}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["returned"] == expected_returned
    assert "note" in body


def test_logs_never_exceed_the_hard_cap(ring, monkeypatch):
    from operator_api import diagnostics_routes

    monkeypatch.setattr(diagnostics_routes, "_MAX_LINES", 4)
    for i in range(20):
        _emit(ring, f"line {i}")
    assert _client().get("/api/diagnostics/logs?lines=1000").json()["returned"] == 4


def test_logs_redact_credentials(ring):
    """Log lines routinely echo tool errors and payloads — the read must scrub them."""
    _emit(ring, "calling with Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345")
    _emit(ring, "env OPENAI_API_KEY=sk-supersecretvalue0123456789")
    text = " ".join(r["message"] for r in _client().get("/api/diagnostics/logs").json()["lines"])
    assert "REDACTED" in text
    assert "supersecretvalue" not in text
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in text


def test_logs_degrade_when_buffer_unconfigured(monkeypatch):
    from observability import logging_config

    monkeypatch.setattr(logging_config, "_ring", None, raising=False)
    body = _client().get("/api/diagnostics/logs").json()
    assert body == {
        "enabled": False,
        "lines": [],
        "returned": 0,
        "capacity": 0,
        "note": "log buffer not configured",
    }


# ── tasks ────────────────────────────────────────────────────────────────────


@pytest.fixture
async def engine(tmp_path, monkeypatch):
    from a2a.server.tasks.database_task_store import TaskModel

    from a2a_impl.stores import make_sqlite_engine

    eng = make_sqlite_engine(str(tmp_path / "tasks.db"))
    async with eng.begin() as conn:
        await conn.run_sync(TaskModel.metadata.create_all)
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "a2a_task_engine", eng, raising=False)
    return eng


async def _insert(eng, **overrides):
    from a2a.server.tasks.database_task_store import TaskModel
    from sqlalchemy import insert

    row = {
        "id": "task-1",
        "context_id": "ctx-1",
        "kind": "task",
        "owner": "",
        "last_updated": datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        "status": {"state": "completed", "message": {"parts": [{"kind": "text", "text": "all done"}]}},
        "artifacts": [{"artifactId": "a1", "name": "answer", "parts": [{"kind": "text", "text": "the answer"}]}],
        "history": [{"role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": "the question"}]}],
        "protocol_version": "0.3.0",
        "metadata": {},
    }
    row.update(overrides)
    async with eng.begin() as conn:
        await conn.execute(insert(TaskModel.__table__).values(**row))


async def test_task_lookup_returns_full_shape(engine):
    await _insert(engine)
    body = _client().get("/api/diagnostics/tasks/task-1").json()
    assert body["task_id"] == "task-1"
    assert body["context_id"] == "ctx-1"
    assert body["state"] == "completed"
    assert body["status_message"] == "all done"
    assert body["accumulated_text"] == "the answer"
    assert body["history"] == [{"role": "user", "message_id": "m1", "text": "the question"}]
    assert body["artifacts"] == [{"artifact_id": "a1", "name": "answer", "text": "the answer"}]
    assert body["last_updated"].startswith("2026-08-28T12:00")
    assert body["truncated"] == [] and body["malformed"] == []


async def test_unknown_task_is_404_not_500(engine):
    resp = _client().get("/api/diagnostics/tasks/nope")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "no such task on this member", "task_id": "nope"}


async def test_task_read_without_store_is_503_not_500(monkeypatch):
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "a2a_task_engine", None, raising=False)
    resp = _client().get("/api/diagnostics/tasks/task-1")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "task store is not configured on this member"


async def test_malformed_row_degrades_and_names_the_bad_columns(engine):
    """An operator reading this is usually here BECAUSE something is broken — a partial
    answer naming what it couldn't parse beats an error page."""
    await _insert(engine, id="task-bad", status="not-a-dict", history="not-a-list", artifacts=42)
    resp = _client().get("/api/diagnostics/tasks/task-bad")
    assert resp.status_code == 200
    body = resp.json()
    assert body["malformed"] == ["artifacts", "history", "status"]
    assert body["state"] is None
    assert body["history"] == [] and body["artifacts"] == []


async def test_history_and_artifacts_are_bounded(engine, monkeypatch):
    from operator_api import diagnostics_routes

    monkeypatch.setattr(diagnostics_routes, "_MAX_HISTORY", 2)
    monkeypatch.setattr(diagnostics_routes, "_MAX_ARTIFACTS", 1)
    await _insert(
        engine,
        id="task-big",
        history=[{"role": "user", "messageId": f"m{i}", "parts": [{"kind": "text", "text": f"q{i}"}]} for i in range(6)],
        artifacts=[{"artifactId": f"a{i}", "parts": [{"kind": "text", "text": f"r{i}"}]} for i in range(4)],
    )
    body = _client().get("/api/diagnostics/tasks/task-big").json()
    assert len(body["history"]) == 2
    assert [h["message_id"] for h in body["history"]] == ["m4", "m5"]  # the RECENT end
    assert len(body["artifacts"]) == 1
    assert set(body["truncated"]) == {"history", "artifacts"}


async def test_long_text_is_truncated_and_flagged(engine, monkeypatch):
    from operator_api import diagnostics_routes

    monkeypatch.setattr(diagnostics_routes, "_MAX_TEXT_CHARS", 10)
    await _insert(
        engine,
        id="task-long",
        artifacts=[{"artifactId": "a1", "parts": [{"kind": "text", "text": "x" * 500}]}],
    )
    body = _client().get("/api/diagnostics/tasks/task-long").json()
    assert body["accumulated_text"] == "x" * 10
    assert "accumulated_text" in body["truncated"]


async def test_task_output_is_redacted(engine):
    await _insert(
        engine,
        id="task-secret",
        artifacts=[{"artifactId": "a1", "parts": [{"kind": "text", "text": "token ghp_" + "a" * 36}]}],
    )
    body = _client().get("/api/diagnostics/tasks/task-secret").json()
    assert "REDACTED" in body["accumulated_text"]
    assert "ghp_" + "a" * 36 not in body["accumulated_text"]


# ── auth: the guarantee is positional, so assert the position ────────────────


def test_diagnostics_is_not_in_the_public_allowlist():
    """These endpoints serve prompts, user content, and credentials. If someone ever
    adds them to the public allowlist, this fails loudly."""
    from a2a_impl import auth

    assert not any("/api/diagnostics".startswith(p) or p.startswith("/api/diagnostics") for p in auth._PUBLIC_PREFIXES)


def test_federation_credential_is_denied_diagnostics():
    """ADR 0066 R1 path ceiling: a federation token is confined to /a2a + /v1. It must
    not reach diagnostics, directly or through the fleet proxy path."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from a2a_impl import auth

    async def _ok(_request):
        return JSONResponse({"ok": True})

    auth.configure(
        bearer_token="op-secret", api_key="", allowed_origins_raw="", federation_token="fed-secret"
    )
    app = Starlette(
        routes=[
            Route("/api/diagnostics/logs", _ok),
            Route("/api/diagnostics/tasks/{task_id}", _ok),
            Route("/agents/{slug}/api/diagnostics/logs", _ok),
        ]
    )
    app.add_middleware(auth.A2AAuthMiddleware)
    c = TestClient(app)

    fed = {"Authorization": "Bearer fed-secret"}
    assert c.get("/api/diagnostics/logs", headers=fed).status_code == 403
    assert c.get("/api/diagnostics/tasks/t1", headers=fed).status_code == 403
    assert c.get("/agents/slug/api/diagnostics/logs", headers=fed).status_code == 403

    op = {"Authorization": "Bearer op-secret"}
    assert c.get("/api/diagnostics/logs", headers=op).status_code == 200
    # …and no credential at all is refused outright.
    assert c.get("/api/diagnostics/logs").status_code == 401
