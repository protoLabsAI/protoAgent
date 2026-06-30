"""Fleet control-plane API (ADR 0042 slice 2) — list/create/start/stop + archetypes."""

from __future__ import annotations

import asyncio
import threading

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


def test_archetypes_carry_base_soul(client):
    # Each archetype seeds the wizard's persona step with a base SOUL (ADR 0042) —
    # the built-in Basic + PM read theirs from config/soul-presets/.
    arr = client.get("/api/archetypes").json()["archetypes"]
    by_id = {a["id"]: a for a in arr}
    assert "soul" in by_id["basic"] and by_id["basic"]["soul"].strip()
    assert by_id["pm-stack"]["soul"].strip()
    # "Custom" is the catch-all write-your-own archetype, kept last with the
    # fill-in template SOUL.
    assert arr[-1]["id"] == "custom" and by_id["custom"]["soul"].strip()


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
    assert client.get("/agents/ghost/whatever").status_code == 409  # slug not running


def test_activate_autostarts_a_stopped_agent(client):
    client.post("/api/fleet", json={"name": "delta", "start": False})  # created, not running
    # activate = ensure-running + keep-warm (no server 'active' pointer — slug routing).
    r = client.post("/api/fleet/delta/activate")
    assert r.status_code == 200 and r.json()["ok"]
    assert next(x for x in client.get("/api/fleet").json()["agents"] if x["name"] == "delta")["running"]


def test_activate_ensures_running_and_keeps_warm(client):
    client.post("/api/fleet", json={"name": "gamma", "port": 7891})  # create + (mocked) start
    r = client.post("/api/fleet/gamma/activate").json()
    assert r["ok"] and "evicted" in r
    # no server-side active pointer anymore — the focused agent is the URL slug
    assert "active" not in client.get("/api/fleet").json()


def test_stop_entire_fleet(client):
    client.post("/api/fleet", json={"name": "x", "port": 7895})
    client.post("/api/fleet", json={"name": "y", "port": 7896})
    assert client.post("/api/fleet/down").json()["ok"]
    # The host can't stop itself; every peer is down.
    assert all(not a["running"] for a in client.get("/api/fleet").json()["agents"] if not a.get("host"))


def test_reserved_host_name_is_400(client):
    # `host` is the reserved slug for this instance — a peer named `host` would shadow it.
    assert client.post("/api/fleet", json={"name": "host"}).status_code == 400


def test_fleet_list_carries_versions(client, monkeypatch):
    """Hub↔remote version handshake over /api/fleet: the host entry carries the hub's
    own version, a remote member carries its last-probed one (never its token) —
    that's what the console compares to flag skew."""
    import httpx
    from graph.fleet import supervisor

    supervisor._probe_cache.clear()
    supervisor.add_remote("ava", "http://1.2.3.4:7871", token="sek")

    class FakeCard:
        status_code = 200

        def json(self):
            return {"name": "ava", "version": "0.30.0"}

    monkeypatch.setattr(httpx, "get", lambda url, timeout: FakeCard())
    agents = client.get("/api/fleet").json()["agents"]
    host = next(a for a in agents if a.get("host"))
    assert host["version"]  # the hub always knows its own version
    remote = next(a for a in agents if a.get("remote"))
    assert remote["version"] == "0.30.0"
    assert "token" not in remote and "sek" not in str(agents)


def test_add_remote_probes_on_register_reachable(client, monkeypatch):
    """POST /api/fleet/remotes probes the new peer immediately and returns
    reachable+version, so the console/CLI can confirm at register time."""
    import httpx
    from graph.fleet import supervisor

    supervisor._probe_cache.clear()

    class FakeCard:
        status_code = 200

        def json(self):
            return {"name": "ava", "version": "0.31.0"}

    monkeypatch.setattr(httpx, "get", lambda url, timeout: FakeCard())
    body = client.post("/api/fleet/remotes", json={"name": "ava", "url": "http://1.2.3.4:7871"}).json()
    assert body["ok"] is True and body["agent"]["name"] == "ava"
    assert body["reachable"] is True and body["version"] == "0.31.0"
    assert "token" not in body["agent"]


def test_add_remote_unreachable_is_registered_not_rejected(client, monkeypatch):
    """An unreachable peer is STILL registered (deferred registration is intentional) —
    the response just reports reachable:false so the caller can warn."""
    import httpx
    from graph.fleet import supervisor

    supervisor._probe_cache.clear()

    def boom(url, timeout):
        raise httpx.HTTPError("connection refused")

    monkeypatch.setattr(httpx, "get", boom)
    r = client.post("/api/fleet/remotes", json={"name": "ghosty", "url": "http://1.2.3.4:7999"})
    assert r.status_code == 200  # NOT a hard reject
    body = r.json()
    assert body["ok"] is True and body["reachable"] is False and body["version"] == ""
    # it's actually in the fleet, just shown not-running
    entry = next(a for a in client.get("/api/fleet").json()["agents"] if a.get("remote"))
    assert entry["name"] == "ghosty" and entry["running"] is False


def test_discover_endpoint(client, monkeypatch):
    # /api/fleet/discover returns OTHER protoAgents (mock the scan); the route's host self-exclusion
    # + supervisor scan run, discover() internals are unit-tested elsewhere.
    from graph.fleet import discovery

    async def fake_discover(**_kw):
        return [{"name": "remote", "url": "http://1.2.3.4:7899", "host": "1.2.3.4", "port": 7899}]

    monkeypatch.setattr(discovery, "discover", fake_discover)
    body = client.get("/api/fleet/discover").json()
    assert body["discovered"][0]["name"] == "remote"


def test_fleet_list_offloads_status_to_thread(client, monkeypatch):
    """supervisor.status() must run off the event loop via asyncio.to_thread (#875)."""
    from graph.fleet import supervisor

    recorded = []
    orig_to_thread = asyncio.to_thread

    async def wrapped_to_thread(func, /, *args, **kwargs):
        recorded.append(func)
        return await orig_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", wrapped_to_thread)
    client.get("/api/fleet")
    assert supervisor.status in recorded, "supervisor.status was not passed to asyncio.to_thread"


def test_fleet_list_status_not_on_event_loop_thread(client, monkeypatch):
    """supervisor.status must not run on the main thread — confirming to_thread off-loads it."""
    from graph.fleet import supervisor

    original_status = supervisor.status

    def checked_status():
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError("supervisor.status called on main thread — not offloaded")
        return original_status()

    monkeypatch.setattr(supervisor, "status", checked_status)
    # If status() ran on the main thread, RuntimeError propagates.
    # Offloaded via to_thread → runs on a thread-pool worker → no error.
    r = client.get("/api/fleet")
    assert r.status_code == 200
