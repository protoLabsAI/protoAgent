"""Fleet control-plane API (ADR 0042 slice 2) — list/create/start/stop + archetypes."""

from __future__ import annotations

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from graph.fleet import supervisor
    from operator_api.fleet_routes import register_fleet_routes

    alive: set[int] = set()
    monkeypatch.setattr(supervisor, "_alive", lambda pid: int(pid) in alive if pid else False)

    class FakeProc:
        def __init__(self, *a, **k):
            self.pid = 4242
            alive.add(4242)

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, sig: alive.discard(int(pid)))

    app = FastAPI()
    register_fleet_routes(app)
    return TestClient(app)


def test_archetypes_include_basic(client):
    arr = client.get("/api/archetypes").json()["archetypes"]
    assert any(a["id"] == "basic" and a["bundle"] is None for a in arr)


def test_create_list_start_stop_remove(client):
    # create (no bundle = Basic) + auto-start
    r = client.post("/api/fleet", json={"name": "alpha", "port": 7890})
    assert r.status_code == 200 and r.json()["agent"]["running"]

    fleet = client.get("/api/fleet").json()["agents"]
    a = next(x for x in fleet if x["name"] == "alpha")
    assert a["running"] and a["port"] == 7890

    assert client.post("/api/fleet/alpha/stop").json()["ok"]
    assert not next(x for x in client.get("/api/fleet").json()["agents"] if x["name"] == "alpha")["running"]

    assert client.delete("/api/fleet/alpha").json()["ok"]
    # The host (this instance) always self-registers, so only the peers are gone.
    assert not [a for a in client.get("/api/fleet").json()["agents"] if not a.get("host")]


def test_create_bad_name_is_400(client):
    assert client.post("/api/fleet", json={"name": "bad name"}).status_code == 400


def test_activate_unknown_400_and_proxy_409(client):
    assert client.post("/api/fleet/ghost/activate").status_code == 400  # no such workspace
    assert client.get("/active/whatever").status_code == 409            # nothing active


def test_activate_autostarts_a_stopped_agent(client):
    client.post("/api/fleet", json={"name": "delta", "start": False})  # created, not running
    r = client.post("/api/fleet/delta/activate")                       # switch → resume + activate
    assert r.status_code == 200 and r.json()["active"] == "delta"


def test_activate_running_sets_active(client):
    client.post("/api/fleet", json={"name": "gamma", "port": 7891})  # create + (mocked) start
    assert client.post("/api/fleet/gamma/activate").json()["active"] == "gamma"
    assert client.get("/api/fleet/active").json()["active"] == "gamma"
    assert client.get("/api/fleet").json()["active"] == "gamma"


def test_stop_entire_fleet(client):
    client.post("/api/fleet", json={"name": "x", "port": 7895})
    client.post("/api/fleet", json={"name": "y", "port": 7896})
    assert client.post("/api/fleet/down").json()["ok"]
    # The host can't stop itself; every peer is down.
    assert all(not a["running"] for a in client.get("/api/fleet").json()["agents"] if not a.get("host"))


def test_reserved_host_name_is_400(client):
    # `host` is the reserved slug for this instance — a peer named `host` would shadow it.
    assert client.post("/api/fleet", json={"name": "host"}).status_code == 400


def test_discover_endpoint(client, monkeypatch):
    # /api/fleet/discover returns OTHER protoAgents (mock the scan); the route's host self-exclusion
    # + supervisor scan run, discover() internals are unit-tested elsewhere.
    from graph.fleet import discovery

    async def fake_discover(**_kw):
        return [{"name": "remote", "url": "http://1.2.3.4:7899", "host": "1.2.3.4", "port": 7899}]

    monkeypatch.setattr(discovery, "discover", fake_discover)
    body = client.get("/api/fleet/discover").json()
    assert body["discovered"][0]["name"] == "remote"
