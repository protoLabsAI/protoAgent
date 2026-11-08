"""Decision needed (#4725, items 1 + 3).

The agent's ``request_decision`` tool parks a turn at a choice it may not make on its own —
even on an UNATTENDED autonomous turn, the one exception to the auto-answer policy — and the
server records a durable pending decision. The operator lists + answers it over
``/api/decisions``, which resumes the parked turn with the chosen option.

Driven with a fake model (a stand-in ``_run_turn_stream``), a fake clock, and an in-memory
``DecisionStore`` — no real graph, gateway, or HTTP server.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.decisions import (
    STATE_ANSWERED,
    STATE_PENDING,
    DecisionClosed,
    DecisionStore,
    normalize_options,
)
from graph.plugins import host
from operator_api import decision_routes
from runtime.state import STATE

# ``server.chat`` the attribute is shadowed by the re-exported ``chat`` function in
# server/__init__.py; resolve the actual submodules from sys.modules (as test_hitl_forms does).
chat_mod = importlib.import_module("server.chat")
turn_control = importlib.import_module("server.turn_control")
turn_stream = importlib.import_module("server.turn_stream")


def _fixed_clock():
    return datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(monkeypatch) -> DecisionStore:
    """A fresh in-memory decisions ledger, injected as the live ``STATE.decision_store`` so the
    turn-driver park path and the routes both resolve it."""
    s = DecisionStore(":memory:", clock=_fixed_clock)
    monkeypatch.setattr(STATE, "decision_store", s, raising=False)
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(STATE, "graph_config", None, raising=False)
    return s


async def _collect(agen):
    return [frame async for frame in agen]


async def _noop_resume(session_id, answer):
    """A resume that does nothing (no real A2A self-POST in a unit test)."""
    return True


class _FakeDecisionStream:
    """Stand-in for ``_run_turn_stream``: the fresh pass yields a ``kind: "decision"`` HITL
    interrupt; a resumed pass completes, echoing the resume value so the test can see the
    chosen option flow back to the tool. Records every pass's ``resume_value``."""

    def __init__(self, options):
        self.options = options
        self.resume_values: list = []

    def __call__(self, message, session_id, config, *, resume_value=None, **_kw):
        self.resume_values.append(resume_value)
        first = len(self.resume_values) == 1

        async def _gen():
            if first:
                yield (
                    "input_required",
                    {"kind": "decision", "question": "Which environment?", "options": self.options},
                )
            else:
                yield ("text", f"Deploying to {resume_value}.")
                yield ("__raw__", f"Deploying to {resume_value}.")

        return _gen()


class _FakeAskHumanStream:
    """Stand-in that asks a plain (non-decision) ``ask_human`` question, then answers once
    resumed — the pre-#4725 auto-answer behaviour on an autonomous turn."""

    def __init__(self):
        self.resume_values: list = []

    def __call__(self, message, session_id, config, *, resume_value=None, **_kw):
        self.resume_values.append(resume_value)
        first = len(self.resume_values) == 1

        async def _gen():
            if first:
                yield ("input_required", {"question": "Which environment?"})
            else:
                yield ("text", "Proceeding with staging.")
                yield ("__raw__", "Proceeding with staging.")

        return _gen()


def _run_turn(message, session_id, *, resume=False, origin):
    cfg = {"configurable": {"thread_id": f"t-{session_id}"}}
    return asyncio.run(
        _collect(
            chat_mod._run_native_turn(
                message,
                session_id,
                cfg,
                request_metadata={"origin": origin},
                resume=resume,
            )
        )
    )


def _client() -> TestClient:
    app = FastAPI()
    decision_routes.register_decision_routes(app)
    return TestClient(app)


# ── store ─────────────────────────────────────────────────────────────────────


def test_create_records_question_options_and_session(store):
    rec = store.create("s-1", "Which environment?", ["staging", "prod"], agent="protoEngineer")
    assert rec.session_id == "s-1"
    assert rec.question == "Which environment?"
    assert rec.state == STATE_PENDING
    assert rec.agent == "protoEngineer"
    assert rec.option_ids() == ["staging", "prod"]
    assert rec.created_at == "2026-11-08T12:00:00+00:00"  # the fake clock
    # Persisted and queryable as pending.
    assert [d.id for d in store.list(state="pending")] == [rec.id]


def test_normalize_options_shapes_strings_and_dicts_and_dedupes():
    opts = normalize_options(["yes", {"id": "no", "label": "No", "description": "stop"}, "yes"])
    assert opts[0] == {"id": "yes", "label": "yes"}
    assert opts[1] == {"id": "no", "label": "No", "description": "stop"}
    assert opts[2]["id"] == "yes-2"  # the clashing id is made unique


def test_answering_twice_is_refused(store):
    rec = store.create("s-1", "Go?", ["yes", "no"])
    store.answer(rec.id, "yes")
    assert store.get(rec.id).state == STATE_ANSWERED
    with pytest.raises(DecisionClosed):
        store.answer(rec.id, "no")  # a non-pending record refuses a second answer


def test_decision_changed_published_on_create_and_answer(store, monkeypatch):
    events: list = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))
    rec = store.create("s-1", "Go?", ["yes", "no"])
    store.answer(rec.id, "yes")
    topics = [t for t, _ in events]
    assert topics == ["decision.changed", "decision.changed"]
    assert events[0][1] == {"id": rec.id, "session_id": "s-1", "state": STATE_PENDING}
    assert events[1][1] == {"id": rec.id, "session_id": "s-1", "state": STATE_ANSWERED}


# ── turn driver: park vs auto-answer ────────────────────────────────────────────


def test_unattended_decision_turn_parks_and_records(store, monkeypatch):
    # An unattended scheduled turn calling request_decision must PARK (not auto-answer) and
    # leave a pending record holding the question, options and session link.
    fake = _FakeDecisionStream(["staging", "prod"])
    monkeypatch.setattr(turn_stream, "_run_turn_stream", fake)

    frames = _run_turn("deploy the app", "s-dec", origin="scheduler")

    kinds = [k for k, _ in frames]
    assert "input_required" in kinds  # parked for a human
    assert "done" not in kinds  # a parked turn yields no terminal answer
    assert fake.resume_values == [None]  # never auto-resumed with the sentinel

    pending = store.list(state="pending")
    assert len(pending) == 1
    rec = pending[0]
    assert rec.session_id == "s-dec"
    assert rec.question == "Which environment?"
    assert rec.option_ids() == ["staging", "prod"]


def test_unattended_ask_human_still_auto_answers(store, monkeypatch):
    # Regression: a NON-blocking ask_human on the same unattended origin keeps the existing
    # no-deadlock auto-answer — only a kind: "decision" interrupt parks.
    fake = _FakeAskHumanStream()
    monkeypatch.setattr(turn_stream, "_run_turn_stream", fake)

    frames = _run_turn("run the deploy", "s-ask", origin="scheduler")

    kinds = [k for k, _ in frames]
    assert "input_required" not in kinds  # never parks an autonomous ask_human
    assert ("done", "Proceeding with staging.") in frames
    assert turn_control._AUTONOMOUS_HITL_SENTINEL in fake.resume_values  # auto-answered
    assert store.list(state="pending") == []  # and it wrote no decision record


# ── routes: answer, resume, validation, listing ─────────────────────────────────


def test_answer_route_resumes_parked_turn_with_chosen_option(store, monkeypatch):
    fake = _FakeDecisionStream(["staging", "prod"])
    monkeypatch.setattr(turn_stream, "_run_turn_stream", fake)
    # Park an unattended turn first.
    _run_turn("deploy the app", "s-dec", origin="scheduler")
    rec = store.list(state="pending")[0]

    # The answer route resumes through the HITL-resume entry point — re-drive the turn with
    # the chosen option so the tool sees it come back (the production path self-POSTs to A2A).
    async def _fake_resume(session_id, answer):
        await _collect(
            chat_mod._run_native_turn(
                answer,
                session_id,
                {"configurable": {"thread_id": "t-s-dec"}},
                request_metadata={},
                resume=True,
            )
        )
        return True

    monkeypatch.setattr(decision_routes, "_resume_parked_turn", _fake_resume)

    r = _client().post(f"/api/decisions/{rec.id}/answer", json={"option_id": "staging"})
    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["state"] == STATE_ANSWERED
    assert body["decision"]["answer"] == "staging"
    assert body["resumed"] is True
    # The turn resumed and the tool received the chosen option as its return value.
    assert "staging" in fake.resume_values
    assert store.get(rec.id).state == STATE_ANSWERED


def test_answer_route_rejects_an_unknown_option(store):
    # Resume is never reached on a validation failure, so no self-POST fires.
    rec = store.create("s-1", "Which environment?", ["staging", "prod"])

    r = _client().post(f"/api/decisions/{rec.id}/answer", json={"option_id": "qa"})
    assert r.status_code == 400
    assert "qa" in r.json()["detail"]
    assert store.get(rec.id).state == STATE_PENDING  # untouched


def test_answer_route_refuses_a_second_answer(store, monkeypatch):
    rec = store.create("s-1", "Go?", ["yes", "no"])
    monkeypatch.setattr(decision_routes, "_resume_parked_turn", _noop_resume)
    c = _client()
    assert c.post(f"/api/decisions/{rec.id}/answer", json={"option_id": "yes"}).status_code == 200
    assert c.post(f"/api/decisions/{rec.id}/answer", json={"option_id": "no"}).status_code == 409


def test_list_route_returns_pending_decisions(store):
    a = store.create("s-1", "Deploy?", ["yes", "no"])
    b = store.create("s-2", "Merge?", ["yes", "no"])
    store.answer(b.id, "yes")  # answered → not pending

    r = _client().get("/api/decisions?state=pending")
    assert r.status_code == 200
    body = r.json()
    ids = [d["id"] for d in body["decisions"]]
    assert ids == [a.id]  # only the pending one
    assert body["pending"] == 1
