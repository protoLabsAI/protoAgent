"""Tests for the friction plugin — record/review + auto-capture middleware."""

from __future__ import annotations

import asyncio
import json

import pytest

from plugins.friction import FrictionMiddleware, friction_review, record_friction


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "friction.jsonl"
    monkeypatch.setenv("FRICTION_LOG", str(p))
    return p


class _Req:
    """Minimal stand-in for the middleware's tool-call request."""

    def __init__(self, name, args=None):
        self.tool_call = {"name": name, "args": args or {}}


def _recs(ledger):
    return [json.loads(line) for line in ledger.read_text().splitlines()]


# ── record_friction / friction_review ────────────────────────────────────────


def test_record_and_review(ledger):
    asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "no disk tool", "severity": "major"}))
    asyncio.run(record_friction.ainvoke({"kind": "model", "summary": "took a wrong path"}))
    out = asyncio.run(friction_review.ainvoke({}))
    assert "harness=1" in out and "model=1" in out
    assert "took a wrong path" in asyncio.run(friction_review.ainvoke({"kind": "model"}))
    assert "no disk tool" not in asyncio.run(friction_review.ainvoke({"kind": "model"}))


def test_record_validates(ledger):
    assert "kind must be" in asyncio.run(record_friction.ainvoke({"kind": "bogus", "summary": "x"}))
    assert "summary is required" in asyncio.run(record_friction.ainvoke({"kind": "harness", "summary": "  "}))


def test_review_empty(ledger):
    assert "empty" in asyncio.run(friction_review.ainvoke({}))


# ── FrictionMiddleware auto-capture ──────────────────────────────────────────


def test_middleware_logs_real_error_and_reraises(ledger):
    mw = FrictionMiddleware()

    def boom(_req):
        raise ValueError("kaboom")

    with pytest.raises(ValueError):
        mw.wrap_tool_call(_Req("some_tool"), boom)
    assert any(r["source"] == "auto" and "raised" in r["summary"] for r in _recs(ledger))


def test_middleware_filters_control_flow(ledger):
    mw = FrictionMiddleware()
    interrupt = type("GraphInterrupt", (Exception,), {})  # name is in _CONTROL_FLOW

    def pause(_req):
        raise interrupt("approval needed")

    with pytest.raises(interrupt):
        mw.wrap_tool_call(_Req("run_command", {"command": "df -h"}), pause)
    recs = _recs(ledger)
    # the escape-hatch reach IS logged; the HITL interrupt is NOT logged as an error
    assert any("escape hatch" in r["summary"] for r in recs)
    assert not any("raised" in r["summary"] for r in recs)


def test_middleware_notes_escape_hatch(ledger):
    mw = FrictionMiddleware()

    def ok(_req):
        return "fine"

    assert mw.wrap_tool_call(_Req("run_command", {"command": "ls"}), ok) == "fine"
    recs = _recs(ledger)
    assert any(r.get("tool") == "run_command" and "escape hatch" in r["summary"] for r in recs)


def test_middleware_ignores_normal_tools(ledger):
    mw = FrictionMiddleware()

    def ok(_req):
        return "ok"

    assert mw.wrap_tool_call(_Req("gpu_status"), ok) == "ok"
    assert not ledger.exists() or _recs(ledger) == []
