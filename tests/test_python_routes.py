"""Operator endpoints for the managed Python runtime (ADR 0094)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.python_routes import register_python_routes


def _client() -> TestClient:
    app = FastAPI()
    register_python_routes(app)
    return TestClient(app)


def test_status_reports_python_and_install(monkeypatch):
    status = {"needed": True, "managed": False, "supported": True, "target_version": "3.12.13"}
    monkeypatch.setattr("runtime.python_install.python_status", lambda: status)
    r = _client().get("/api/runtime/python")
    assert r.status_code == 200
    body = r.json()
    assert body["python"]["target_version"] == "3.12.13"
    assert body["install"]["state"] in {"idle", "running", "done", "error"}


def test_install_rejected_on_unsupported_platform(monkeypatch):
    monkeypatch.setattr("runtime.python_install.is_supported", lambda: False)
    monkeypatch.setattr("runtime.python_install.python_status", lambda: {"managed": False, "supported": False})
    r = _client().post("/api/runtime/python/install")
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_install_accepts_and_reports_running(monkeypatch):
    monkeypatch.setattr("runtime.python_install.is_supported", lambda: True)
    monkeypatch.setattr("runtime.python_install.python_status", lambda: {"managed": False, "supported": True})
    # A fast, side-effect-free install so the background thread can't do real work.
    monkeypatch.setattr("runtime.python_install.install_managed_python", lambda **_: {"managed": True})
    r = _client().post("/api/runtime/python/install")
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True
    assert body["install"]["state"] == "running"
