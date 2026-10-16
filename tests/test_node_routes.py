"""Operator endpoints for the managed Node runtime (ADR 0085)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.node_routes import register_node_routes


def _client() -> TestClient:
    app = FastAPI()
    register_node_routes(app)
    return TestClient(app)


def test_status_reports_node_and_install(monkeypatch):
    status = {"source": None, "version": None, "supported": True, "target_version": "v24.18.0"}
    monkeypatch.setattr("runtime.node_install.node_status", lambda: status)
    r = _client().get("/api/runtime/node")
    assert r.status_code == 200
    body = r.json()
    assert body["node"]["target_version"] == "v24.18.0"
    assert body["install"]["state"] in {"idle", "running", "done", "error"}


def test_install_rejected_on_unsupported_platform(monkeypatch):
    monkeypatch.setattr("runtime.node_install.is_supported", lambda: False)
    monkeypatch.setattr("runtime.node_install.node_status", lambda: {"source": None, "supported": False})
    r = _client().post("/api/runtime/node/install")
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_install_accepts_and_reports_running(monkeypatch):
    monkeypatch.setattr("runtime.node_install.is_supported", lambda: True)
    monkeypatch.setattr("runtime.node_install.node_status", lambda: {"source": None, "supported": True})
    # A fast, side-effect-free install so the background thread can't do real work.
    monkeypatch.setattr("runtime.node_install.install_managed_node", lambda **_: {"source": "managed"})
    r = _client().post("/api/runtime/node/install")
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True
    assert body["install"]["state"] == "running"


def test_second_install_while_running_is_idempotent(monkeypatch):
    """A POST while an install is already in flight returns 202 with the live state and
    does NOT spawn a second download (the lock-protected `running` check)."""
    from operator_api import node_routes

    calls: list[bool] = []
    monkeypatch.setattr("runtime.node_install.is_supported", lambda: True)
    monkeypatch.setattr("runtime.node_install.node_status", lambda: {"source": None, "supported": True})
    monkeypatch.setattr(node_routes, "_run_install", lambda force: calls.append(force))
    monkeypatch.setitem(node_routes._install, "state", "running")
    monkeypatch.setitem(node_routes._install, "pct", 42)
    monkeypatch.setitem(node_routes._install, "message", "downloading… 42%")

    r = _client().post("/api/runtime/node/install")
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True
    assert body["install"]["state"] == "running"
    assert body["install"]["pct"] == 42  # the in-flight progress, not a reset
    assert calls == []  # no second download spawned
