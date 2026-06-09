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
    assert not client.get("/api/fleet").json()["agents"]


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
    assert all(not a["running"] for a in client.get("/api/fleet").json()["agents"])
