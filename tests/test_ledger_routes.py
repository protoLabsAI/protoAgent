"""The delegation-ledger read API.

Sits beside the telemetry routes because it answers the question telemetry cannot: the
`turns` table has no actor column and no edge, so "how much did this agent spend" and
"who asked whom to do what" are different reads over different stores.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability.ledger_store import LedgerStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    from operator_api import telemetry_routes
    from runtime.state import STATE

    store = LedgerStore(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(STATE, "ledger_store", store, raising=False)
    app = FastAPI()
    telemetry_routes.register_telemetry_routes(app)
    return TestClient(app), store


def _edge(store, **over):
    base = dict(from_agent="gina", to_kind="acp", to_name="coder", what="build", session_id="s1")
    base.update(over)
    store.record(**base)


def test_recent_edges_come_back_newest_first(client):
    c, store = client
    _edge(store, what="first")
    _edge(store, what="second")

    body = c.get("/api/ledger").json()
    assert body["enabled"] is True
    assert [e["what"] for e in body["edges"]] == ["second", "first"]


def test_recent_can_scope_to_one_session(client):
    c, store = client
    _edge(store, session_id="s1")
    _edge(store, session_id="s2")

    assert len(c.get("/api/ledger?session=s1").json()["edges"]) == 1


def test_the_aggregate_groups_by_target_and_reports_how_many_were_priced(client):
    # Without `priced`, a partial sum reads as a total and "cheap" is indistinguishable
    # from "mostly unmeasured".
    c, store = client
    _edge(store, to_name="coder", cost_usd=1.0)
    _edge(store, to_name="coder", cost_usd=None)

    edge = c.get("/api/ledger/edges").json()["edges"][0]
    assert edge["dispatches"] == 2
    assert edge["priced"] == 1
    assert edge["cost_usd"] == pytest.approx(1.0)


def test_an_instance_with_no_ledger_reports_disabled_rather_than_failing(tmp_path, monkeypatch):
    # A host that never wired a store must degrade to "nothing to show", not a 500 that
    # takes the console panel down with it.
    from operator_api import telemetry_routes
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "ledger_store", None, raising=False)
    app = FastAPI()
    telemetry_routes.register_telemetry_routes(app)
    c = TestClient(app)

    for path in ("/api/ledger", "/api/ledger/edges"):
        body = c.get(path).json()
        assert body["enabled"] is False
        assert body["edges"] == []


def test_the_limit_is_bounded_so_one_read_cannot_pull_the_whole_table(client):
    c, store = client
    for _ in range(5):
        _edge(store)

    assert c.get("/api/ledger?limit=99999").status_code == 200
    assert c.get("/api/ledger?limit=0").status_code == 200
