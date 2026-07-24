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
        returncode = None

        def __init__(self, *a, **k):
            self.pid = 4242
            alive.add(4242)

        def poll(self):  # boot watch: still running
            return None

    monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(supervisor, "_is_our_agent", lambda pid: True)
    # Fake spawns never bind a port — short-circuit the boot watch to "it's up".
    monkeypatch.setattr(supervisor, "_port_listening", lambda port, timeout=0.25: True)
    monkeypatch.setattr(supervisor.os, "kill", lambda pid, sig: alive.discard(int(pid)))

    app = FastAPI()
    register_fleet_routes(app)
    return TestClient(app)


def test_archetypes_include_basic(client):
    arr = client.get("/api/archetypes").json()["archetypes"]
    assert any(a["id"] == "basic" and a["bundle"] is None for a in arr)


def test_archetypes_carry_base_soul(client):
    # Each archetype seeds the wizard's persona step with a base SOUL (ADR 0042) — the
    # catalog names a soul_preset file under config/soul-presets/, resolved server-side.
    arr = client.get("/api/archetypes").json()["archetypes"]
    by_id = {a["id"]: a for a in arr}
    assert "soul" in by_id["basic"] and by_id["basic"]["soul"].strip()
    # "Custom" is the catch-all write-your-own archetype, kept last with the
    # fill-in template SOUL.
    assert arr[-1]["id"] == "custom" and by_id["custom"]["soul"].strip()


def test_archetypes_fall_back_when_catalog_missing(client, monkeypatch):
    # A missing/unreadable archetype-catalog.json must still yield the two code-free
    # personas (Basic + Custom) so the picker never comes up empty (ADR 0042).
    from operator_api import fleet_routes

    monkeypatch.setattr(fleet_routes, "_load_archetype_catalog", lambda: fleet_routes._FALLBACK_ARCHETYPES)
    arr = client.get("/api/archetypes").json()["archetypes"]
    ids = [a["id"] for a in arr]
    assert ids[0] == "basic" and ids[-1] == "custom"
    assert all(a["soul"].strip() for a in arr)  # soul_preset resolved to real content


def test_archetypes_dedupe_installed_bundle_against_catalog(client, monkeypatch):
    # An installed bundle whose id/URL already appears in the catalog must NOT produce a
    # duplicate RadioCard (duplicate React key + ambiguous radio value). Catalog wins.
    from operator_api import fleet_routes

    monkeypatch.setattr(
        fleet_routes,
        "_load_archetype_catalog",
        lambda: [
            {"id": "basic", "label": "Basic", "bundle": None, "soul_preset": "base"},
            {"id": "acme", "label": "Acme", "bundle": "https://github.com/acme/stack.git", "soul": "x"},
            {"id": "custom", "label": "Custom", "bundle": None, "soul_preset": "blank"},
        ],
    )

    def fake_lock():
        return {
            "bundles": [
                # same id as a catalog entry
                {"id": "acme", "source_url": "https://other/url", "archetype": {"label": "Dup id"}},
                # same URL (differing suffix) as the catalog's acme entry
                {"id": "acme2", "source_url": "https://github.com/acme/stack", "archetype": {"label": "Dup url"}},
                # genuinely new → appended
                {"id": "fresh", "source_url": "https://github.com/x/y", "archetype": {"label": "Fresh"}},
            ]
        }

    monkeypatch.setattr("graph.plugins.installer._read_lock", fake_lock)
    ids = [a["id"] for a in client.get("/api/archetypes").json()["archetypes"]]
    assert ids.count("acme") == 1 and "acme2" not in ids  # both duplicates dropped
    assert "fresh" in ids
    assert ids[-1] == "custom"  # custom stays last even after bundle archetypes append


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


def test_create_writes_archetype_soul(client):
    # The picked archetype's persona is written into the workspace SOUL.md (ADR 0042),
    # so a created agent arrives WITH its persona, not just its tools.
    from pathlib import Path

    from graph.workspaces import manager

    r = client.post("/api/fleet", json={"name": "persona", "start": False, "soul": "# Persona\nBe bold."})
    assert r.status_code == 200
    ws = next(w for w in manager.list_workspaces() if w["name"] == "persona")
    assert (Path(ws["path"]) / "config" / "SOUL.md").read_text().startswith("# Persona")


def test_create_without_soul_leaves_default(client):
    # No/blank soul → no SOUL.md written, so the agent stays on the default persona.
    from pathlib import Path

    from graph.workspaces import manager

    client.post("/api/fleet", json={"name": "plain", "start": False})
    ws = next(w for w in manager.list_workspaces() if w["name"] == "plain")
    assert not (Path(ws["path"]) / "config" / "SOUL.md").exists()


def test_create_forwards_inputs_and_secrets(client, monkeypatch):
    """POST /api/fleet threads operator `inputs` (MCP template values) and `secrets`
    (bundle secret values) into manager.create so they seed the member after install (#2041)."""
    from graph.workspaces import manager

    captured: dict = {}

    def fake_create(name, **kwargs):
        captured.update(name=name, **kwargs)
        return {"id": f"{name}-0000", "name": name, "port": 7999, "path": "/tmp/x", "installed": []}

    monkeypatch.setattr(manager, "create", fake_create)
    r = client.post(
        "/api/fleet",
        json={
            "name": "seeded",
            "start": False,
            "bundle": "https://github.com/x/stack",
            "inputs": {"token": "ghp_x"},
            "secrets": [{"key": "openai_api_key", "value": "sk-1"}],
        },
    )
    assert r.status_code == 200
    assert captured["inputs"] == {"token": "ghp_x"}
    assert captured["secrets"] == [{"key": "openai_api_key", "value": "sk-1"}]


def test_create_ignores_malformed_inputs_and_secrets(client, monkeypatch):
    """A malformed `inputs`/`secrets` field degrades to None (env-only fallback), never a 500."""
    from graph.workspaces import manager

    captured: dict = {}

    def fake_create(name, **kwargs):
        captured.update(kwargs)
        return {"id": f"{name}-0", "name": name, "port": 7999, "path": "/tmp", "installed": []}

    monkeypatch.setattr(manager, "create", fake_create)
    r = client.post(
        "/api/fleet",
        json={"name": "x", "start": False, "inputs": ["not", "a", "map"], "secrets": "nope"},
    )
    assert r.status_code == 200
    assert captured["inputs"] is None and captured["secrets"] is None


def test_create_without_inputs_or_secrets_forwards_none(client, monkeypatch):
    """No inputs/secrets in the body → None flows through (the seed phase is a pure no-op)."""
    from graph.workspaces import manager

    captured: dict = {}

    def fake_create(name, **kwargs):
        captured.update(kwargs)
        return {"id": f"{name}-0", "name": name, "port": 7999, "path": "/tmp", "installed": []}

    monkeypatch.setattr(manager, "create", fake_create)
    assert client.post("/api/fleet", json={"name": "plain", "start": False}).status_code == 200
    assert captured["inputs"] is None and captured["secrets"] is None


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


def test_patch_remote_edits_and_reprobes(client, monkeypatch):
    """PATCH /api/fleet/remotes/{ident} edits url/token/name in place (id/slug stable) and
    re-probes so the response carries fresh reachability. A bad url is a 400, not a 500."""
    import httpx
    from graph.fleet import supervisor

    supervisor._probe_cache.clear()
    monkeypatch.setattr(httpx, "get", lambda url, timeout: type("C", (), {"status_code": 200, "json": lambda s: {}})())
    rid = client.post("/api/fleet/remotes", json={"name": "ava", "url": "http://1.2.3.4:7871"}).json()["agent"]["id"]

    r = client.patch(f"/api/fleet/remotes/{rid}", json={"url": "http://1.2.3.4:7999", "token": "sek"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["agent"]["url"] == "http://1.2.3.4:7999" and body["reachable"] is True
    assert "token" not in body["agent"]  # the bearer never comes back out
    entry = next(a for a in client.get("/api/fleet").json()["agents"] if a.get("remote"))
    assert entry["id"] == rid and entry["url"] == "http://1.2.3.4:7999"  # same id, new url

    assert client.patch(f"/api/fleet/remotes/{rid}", json={"url": "ftp://nope"}).status_code == 400
    assert client.patch("/api/fleet/remotes/ghost", json={"token": "x"}).status_code == 400


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


# ── Archetype preview (peek without install) ─────────────────────────────────


def test_archetype_preview_code_free_returns_null_bundle(client):
    arr = client.get("/api/archetypes").json()["archetypes"]
    code_free = next(a["id"] for a in arr if not a.get("bundle"))
    body = client.get(f"/api/archetypes/{code_free}/preview").json()
    assert body == {"id": code_free, "bundle": None}


def test_archetype_preview_unknown_id_404s(client):
    assert client.get("/api/archetypes/nope/preview").status_code == 404


def test_archetype_preview_peeks_bundle(client, monkeypatch):
    from operator_api import fleet_routes

    monkeypatch.setattr(
        fleet_routes,
        "_load_archetype_catalog",
        lambda: [
            {"id": "stacked", "label": "Stacked", "bundle": "https://github.com/x/stack", "soul": "S"},
            *fleet_routes._FALLBACK_ARCHETYPES,
        ],
    )
    import ops.plugins as plugin_ops

    async def _fake_peek(url, ref=None):
        assert url == "https://github.com/x/stack"
        return {"kind": "bundle", "id": "stack", "members": [{"id": "m1", "builtin": False}]}

    monkeypatch.setattr(plugin_ops, "peek_bundle", _fake_peek)
    body = client.get("/api/archetypes/stacked/preview").json()
    assert body["bundle"]["id"] == "stack"
    assert body["bundle"]["members"][0]["id"] == "m1"


def test_archetype_preview_fetch_failure_is_502(client, monkeypatch):
    from operator_api import fleet_routes

    monkeypatch.setattr(
        fleet_routes,
        "_load_archetype_catalog",
        lambda: [
            {"id": "stacked", "label": "Stacked", "bundle": "https://github.com/x/stack", "soul": "S"},
            *fleet_routes._FALLBACK_ARCHETYPES,
        ],
    )
    import ops.plugins as plugin_ops

    async def _boom(url, ref=None):
        raise RuntimeError("offline")

    monkeypatch.setattr(plugin_ops, "peek_bundle", _boom)
    assert client.get("/api/archetypes/stacked/preview").status_code == 502


def test_create_drops_null_input_values(client, monkeypatch):
    """A JSON null input value means "not provided": it is dropped BEFORE str() coercion —
    str(None) is the truthy literal "None", which would bypass resolve_bundle_mcp_item's
    env/default fallthrough and fill templates with a garbage token (QA panel, #2125)."""
    from graph.workspaces import manager

    captured: dict = {}

    def fake_create(name, **kwargs):
        captured.update(kwargs)
        return {"id": f"{name}-0", "name": name, "port": 7999, "path": "/tmp", "installed": []}

    monkeypatch.setattr(manager, "create", fake_create)
    r = client.post(
        "/api/fleet",
        json={"name": "x", "start": False, "inputs": {"token": None, "host": "hq"}},
    )
    assert r.status_code == 200
    assert captured["inputs"] == {"host": "hq"}  # null dropped, never the string "None"


def test_archetypes_carry_requires(client, monkeypatch):
    """`requires` (#2186 follow-on) passes through from BOTH sources — the catalog
    entry and a bundle's archetype: block — and degrades to [] when absent, so the
    picker can warn at choose-time about unprovisioned host capabilities."""
    from operator_api import fleet_routes

    monkeypatch.setattr(
        fleet_routes,
        "_load_archetype_catalog",
        lambda: [
            {"id": "basic", "label": "Basic", "bundle": None, "soul_preset": "base"},
            {
                "id": "docsy",
                "label": "Docsy",
                "bundle": "https://github.com/x/docsy-stack",
                "soul": "x",
                "requires": ["python_runtime"],
            },
            {"id": "custom", "label": "Custom", "bundle": None, "soul_preset": "blank"},
        ],
    )
    monkeypatch.setattr(
        "graph.plugins.installer._read_lock",
        lambda: {
            "bundles": [
                {
                    "id": "labsy",
                    "source_url": "https://github.com/x/labsy",
                    "archetype": {"label": "Labsy", "requires": ["python_runtime"]},
                },
                {"id": "plain", "source_url": "https://github.com/x/plain", "archetype": {"label": "Plain"}},
            ]
        },
    )
    by_id = {a["id"]: a for a in client.get("/api/archetypes").json()["archetypes"]}
    assert by_id["docsy"]["requires"] == ["python_runtime"]  # catalog entry
    assert by_id["labsy"]["requires"] == ["python_runtime"]  # bundle archetype: block
    assert by_id["basic"]["requires"] == []  # absent → [] (older entries)
    assert by_id["plain"]["requires"] == []
