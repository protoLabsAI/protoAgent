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
    monkeypatch.setattr(supervisor, "signal_tree", lambda pid, *, force: alive.discard(int(pid)))

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


def test_bundle_archetype_resolves_soul_preset(client, monkeypatch):
    """A bundle's archetype: block can name a host `soul_preset` — the same soul /
    soul_preset pair the catalog supports (#2715). Before, only inline `soul` worked on
    the bundle path, and a preset-naming bundle silently fell back to the base persona.
    An unknown preset resolves to "" (warned in the log; the console then falls back)."""

    def fake_lock():
        return {
            "bundles": [
                {
                    "id": "presetful",
                    "source_url": "https://github.com/x/a",
                    "archetype": {"label": "P", "soul_preset": "base"},
                },
                {
                    "id": "inline",
                    "source_url": "https://github.com/x/b",
                    "archetype": {"label": "I", "soul": "# Inline"},
                },
                {
                    "id": "ghost",
                    "source_url": "https://github.com/x/c",
                    "archetype": {"label": "G", "soul_preset": "no-such-preset"},
                },
            ]
        }

    monkeypatch.setattr("graph.plugins.installer._read_lock", fake_lock)
    by_id = {a["id"]: a for a in client.get("/api/archetypes").json()["archetypes"]}
    assert by_id["presetful"]["soul"].strip()  # resolved to the real preset content
    assert by_id["inline"]["soul"] == "# Inline"  # inline still wins when present
    assert by_id["ghost"]["soul"] == ""  # unknown preset → empty, console falls back


def test_archetypes_dedupe_installed_bundle_against_catalog(client, monkeypatch):
    # An installed bundle whose id/URL already appears in the catalog must NOT produce a
    # duplicate RadioCard (duplicate React key + ambiguous radio value). Catalog wins.
    from operator_api import fleet_routes

    monkeypatch.setattr(
        fleet_routes,
        "_load_archetype_catalog",
        lambda: [
            {"id": "basic", "label": "Basic", "bundle": None, "soul_preset": "base"},
            {
                "id": "acme",
                "label": "Acme",
                "bundle": "https://github.com/acme/kit.git",
                # former URL of the since-renamed bundle repo (dedupe-only)
                "bundle_aliases": ["https://github.com/acme/oldkit"],
                "soul": "x",
            },
            {"id": "custom", "label": "Custom", "bundle": None, "soul_preset": "blank"},
        ],
    )

    def fake_lock():
        return {
            "bundles": [
                # same id as a catalog entry
                {"id": "acme", "source_url": "https://other/url", "archetype": {"label": "Dup id"}},
                # same URL (differing suffix) as the catalog's acme entry
                {"id": "acme2", "source_url": "https://github.com/acme/kit", "archetype": {"label": "Dup url"}},
                # installed under the repo's FORMER URL (pre-rename pin; GitHub redirect
                # keeps it resolving) — bundle_aliases must dedupe it too
                {"id": "acme3", "source_url": "https://github.com/acme/oldkit.git", "archetype": {"label": "Dup alias"}},
                # genuinely new → appended
                {"id": "fresh", "source_url": "https://github.com/x/y", "archetype": {"label": "Fresh"}},
            ]
        }

    monkeypatch.setattr("graph.plugins.installer._read_lock", fake_lock)
    ids = [a["id"] for a in client.get("/api/archetypes").json()["archetypes"]]
    assert ids.count("acme") == 1 and "acme2" not in ids  # both duplicates dropped
    assert "acme3" not in ids  # old-URL install deduped via bundle_aliases
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


def test_create_forwards_config_inputs(client, monkeypatch):
    """POST /api/fleet threads operator `config_inputs` answers (#2934) into
    manager.create — values untouched (a toggle's bool stays a bool; the seed helper
    coerces per declared type), JSON nulls dropped, malformed/absent → None."""
    from graph.workspaces import manager

    captured: dict = {}

    def fake_create(name, **kwargs):
        captured.update(kwargs)
        return {"id": f"{name}-0", "name": name, "port": 7999, "path": "/tmp", "installed": []}

    monkeypatch.setattr(manager, "create", fake_create)
    r = client.post(
        "/api/fleet",
        json={
            "name": "pm",
            "start": False,
            "bundle": "https://github.com/x/stack",
            "config_inputs": {"board.repo": "org/repo", "board.auto_merge": True, "board.skip": None},
        },
    )
    assert r.status_code == 200
    assert captured["config_inputs"] == {"board.repo": "org/repo", "board.auto_merge": True}

    captured.clear()
    assert client.post("/api/fleet", json={"name": "pm2", "start": False, "config_inputs": "nope"}).status_code == 200
    assert captured["config_inputs"] is None


def test_create_forwards_requires_tools(client, monkeypatch):
    """POST /api/fleet threads the archetype's `requires_tools` contract into manager.create
    (#2277/#2713) — blank entries dropped, absent field → None so nothing is persisted."""
    from graph.workspaces import manager

    captured: dict = {}

    def fake_create(name, **kwargs):
        captured.update(kwargs)
        return {"id": f"{name}-0", "name": name, "port": 7999, "path": "/tmp", "installed": []}

    monkeypatch.setattr(manager, "create", fake_create)
    r = client.post(
        "/api/fleet",
        json={"name": "pm", "start": False, "requires_tools": ["github_create_issue", "  "]},
    )
    assert r.status_code == 200
    assert captured["requires_tools"] == ["github_create_issue"]

    captured.clear()
    assert client.post("/api/fleet", json={"name": "pm2", "start": False}).status_code == 200
    assert captured["requires_tools"] is None


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


def test_create_with_inheritance_disabled_never_supplies_an_oauth_transfer_source(client, monkeypatch):
    """`inherit_config: false` is the explicit blank-agent escape hatch: it must not
    copy model config or transfer a legacy OAuth store into box ownership."""
    from graph.workspaces import manager

    captured: dict = {}

    def fake_create(name, **kwargs):
        captured.update(kwargs)
        return {"id": f"{name}-0", "name": name, "port": 7999, "path": "/tmp", "installed": []}

    monkeypatch.setattr(manager, "create", fake_create)
    response = client.post("/api/fleet", json={"name": "blank", "start": False, "inherit_config": False})
    assert response.status_code == 200
    assert captured["inherit_model"] is None


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


# ── fleet roster order (ADR 0042 hub control-plane) ───────────────────────────


def test_put_fleet_order_persists_and_reorders(client):
    """PUT /api/fleet/order persists a complete id permutation; the next GET /api/fleet
    returns members in that order."""
    client.post("/api/fleet", json={"name": "alpha", "start": False})
    client.post("/api/fleet", json={"name": "bravo", "start": False})
    ids = [a["id"] for a in client.get("/api/fleet").json()["agents"]]  # [host, alpha, bravo]
    new_order = list(reversed(ids))

    r = client.put("/api/fleet/order", json={"order": new_order})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["order"] == new_order
    assert [a["id"] for a in client.get("/api/fleet").json()["agents"]] == new_order


def test_put_fleet_order_rejects_bad_payloads(client):
    """Duplicate / unknown / missing / malformed → 400, and the saved order is untouched."""
    client.post("/api/fleet", json={"name": "alpha", "start": False})
    ids = [a["id"] for a in client.get("/api/fleet").json()["agents"]]  # [host, alpha]

    assert client.put("/api/fleet/order", json={"order": ids}).status_code == 200  # baseline good
    assert client.put("/api/fleet/order", json={"order": ids + ids[:1]}).status_code == 400  # duplicate
    assert client.put("/api/fleet/order", json={"order": ids + ["nope-0000"]}).status_code == 400  # unknown
    assert client.put("/api/fleet/order", json={"order": ids[:1]}).status_code == 400  # missing member
    assert client.put("/api/fleet/order", json={"order": "nope"}).status_code == 400  # malformed
    # The baseline order is still in force — no rejected payload mutated it.
    assert [a["id"] for a in client.get("/api/fleet").json()["agents"]] == ids


def test_put_fleet_order_reconciles_new_member(client):
    """A member added after an order is saved appears exactly once, after the ordered
    members, with the saved relative order retained."""
    client.post("/api/fleet", json={"name": "alpha", "start": False})
    ids = [a["id"] for a in client.get("/api/fleet").json()["agents"]]  # [host, alpha]
    client.put("/api/fleet/order", json={"order": list(reversed(ids))})  # [alpha, host]

    client.post("/api/fleet", json={"name": "bravo", "start": False})  # new, unordered
    out = [a["id"] for a in client.get("/api/fleet").json()["agents"]]
    assert out[:2] == list(reversed(ids))  # saved order kept
    assert len(out) == 3 and len(set(out)) == 3  # bravo included once, nothing lost
    assert out[2] not in ids  # the new member is appended last


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
    supervisor.add_remote("ava", "http://100.64.1.4:7871", token="sek")

    class FakeCard:
        status_code = 200

        def json(self):
            return {"name": "ava", "version": "0.30.0"}

    monkeypatch.setattr(httpx, "get", lambda url, timeout, **kw: FakeCard())
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

    monkeypatch.setattr(httpx, "get", lambda url, timeout, **kw: FakeCard())
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

    def boom(url, timeout, **kw):
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
    monkeypatch.setattr(httpx, "get", lambda url, timeout, **kw: type("C", (), {"status_code": 200, "json": lambda s: {}})())
    rid = client.post("/api/fleet/remotes", json={"name": "ava", "url": "http://100.64.1.4:7871"}).json()["agent"]["id"]

    r = client.patch(f"/api/fleet/remotes/{rid}", json={"url": "http://100.64.1.4:7999", "token": "sek"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["agent"]["url"] == "http://100.64.1.4:7999" and body["reachable"] is True
    assert "token" not in body["agent"]  # the bearer never comes back out...
    assert supervisor.remote_for_slug(rid)["token"] == "sek"  # ...but it WAS stored
    entry = next(a for a in client.get("/api/fleet").json()["agents"] if a.get("remote"))
    assert entry["id"] == rid and entry["url"] == "http://100.64.1.4:7999"  # same id, new url

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


def test_archetypes_carry_tier(client, monkeypatch):
    """`tier` (ADR 0042 picker placement) passes through from BOTH sources — the catalog
    entry and a bundle's archetype: block — normalized to exactly "standard"/"advanced".
    A missing (or bogus/differently-cased) tag files the card inline as "standard", so the
    console can split the picker without every catalog entry having to spell the field out."""
    from operator_api import fleet_routes

    monkeypatch.setattr(
        fleet_routes,
        "_load_archetype_catalog",
        lambda: [
            {"id": "basic", "label": "Basic", "bundle": None, "soul_preset": "base"},
            {
                "id": "pm",
                "label": "Project Manager",
                "bundle": "https://github.com/x/pm-stack",
                "soul": "x",
                "tier": "advanced",
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
                    "archetype": {"label": "Labsy", "tier": "ADVANCED"},
                },
                {
                    "id": "plain",
                    "source_url": "https://github.com/x/plain",
                    "archetype": {"label": "Plain", "tier": "bogus"},
                },
            ]
        },
    )
    by_id = {a["id"]: a for a in client.get("/api/archetypes").json()["archetypes"]}
    assert by_id["pm"]["tier"] == "advanced"  # catalog entry
    assert by_id["labsy"]["tier"] == "advanced"  # bundle block, case-normalized
    assert by_id["basic"]["tier"] == "standard"  # absent → standard
    assert by_id["plain"]["tier"] == "standard"  # unknown value → standard


def test_purge_that_cannot_delete_the_workspace_is_409_not_500(client, monkeypatch):
    """#2583: the endpoint used to let rmtree's OSError escape as a generic 500 *after* it
    had already stopped the member and cleared its record — a terminal-looking failure on a
    half-completed destructive op. It must say which half happened, and that retrying works."""
    from graph.workspaces import manager

    client.post("/api/fleet", json={"name": "alpha"})

    def always_locked(path, **kw):
        raise OSError(32, "The process cannot access the file because it is being used")

    monkeypatch.setattr(manager.shutil, "rmtree", always_locked)
    # Keep every retry attempt but skip the real ~2s backoff between them (the
    # schedule itself is pinned in test_workspaces).
    monkeypatch.setitem(manager._rmtree_resilient.__kwdefaults__, "delay", 0.0)

    resp = client.delete("/api/fleet/alpha?purge=true")

    assert resp.status_code == 409  # not 500, and not the 400 a rejected request gets
    detail = resp.json()["detail"]
    assert "IS stopped" in detail and "retry" in detail.lower()


def test_create_refusal_is_a_400_with_the_prompt_named(client, monkeypatch):
    """A required Configure answer missing (or a picked delegate the host lacks) is a
    WorkspaceError from manager.create → 400 carrying the message the panel toasts."""
    from graph.workspaces import manager

    def refuse(name, **kwargs):
        raise manager.WorkspaceError(
            "the bundle needs these Configure answers before the agent can work: Coder delegate (board.coder)"
        )

    monkeypatch.setattr(manager, "create", refuse)
    r = client.post("/api/fleet", json={"name": "pm", "start": False, "bundle": "https://github.com/x/stack"})
    assert r.status_code == 400
    assert "Coder delegate (board.coder)" in r.json()["detail"]


def test_fleet_list_reports_a_malformed_remote_instead_of_500ing(client):
    """#3018's sibling surface. The telemetry rollup was the reported symptom, but
    /api/fleet reads the SAME registry through the same ``refresh_remote_probes()``
    + ``status()`` pair, so one hand-edited remote record took the console's fleet
    list down too. Fixing it in the route would have left this one broken."""
    import json

    from graph.fleet import supervisor
    from graph.workspaces import manager

    root = manager.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "remotes.json").write_text(json.dumps({"ava-1a2b": {"id": "ava-1a2b", "name": "ava"}}))
    supervisor._probe_cache.clear()

    res = client.get("/api/fleet")

    assert res.status_code == 200
    row = next(a for a in res.json()["agents"] if a.get("remote"))
    # Reported for what it is: a member with no address, so never reachable —
    # and no ``a2a`` endpoint invented out of a missing url.
    assert row["id"] == "ava-1a2b" and row["name"] == "ava"
    assert row["running"] is False and row["url"] == "" and row["a2a"] is None


def test_rename_with_a_null_name_is_400_not_the_string_none(client):
    client.post("/api/fleet", json={"name": "alpha", "start": False})
    for body in ({"name": None}, {"name": "   "}, {}):
        r = client.patch("/api/fleet/alpha", json=body)
        assert r.status_code == 400 and "name is required" in r.json()["detail"], body
    assert next(a for a in client.get("/api/fleet").json()["agents"] if a["id"].startswith("alpha"))["name"] == "alpha"


# ── POST /api/fleet/remotes/pair (ADR 0113 D1/D10) ────────────────────────────────────────
# The route's contract at the HTTP layer, under this module's full fleet fixture: the status
# the console and `protoagent fleet pair` branch on. The wire to the remote is faked at
# httpx.post/get, the same seam the probe tests above use; supervisor-level pairing
# behaviour (re-token, naming, redirects) is pinned in test_fleet_pairing.py.

_PAIR_TOKEN = "dev-tok-ROUTE-5a4b3c"


class _PairResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def pair_wire(client, monkeypatch):
    """A remote at a tailnet address whose claim answer each test sets. Returns the claim
    posts so a test can assert what was (or wasn't) sent."""
    import httpx
    from graph.fleet import supervisor

    supervisor._probe_cache.clear()
    supervisor._auth_cache.clear()
    wire = {"claim": _PairResp(200, {"ok": True, "device": {"id": "ab" * 8}, "token": _PAIR_TOKEN}), "posts": []}

    def post(url, json=None, timeout=None, **kw):
        wire["posts"].append((url, dict(json or {})))
        if isinstance(wire["claim"], Exception):
            raise wire["claim"]
        return wire["claim"]

    def get(url, timeout=None, headers=None, **kw):
        if url.endswith("/.well-known/agent-card.json"):
            return _PairResp(200, {"name": "ava", "version": "0.1.0"})
        bearer = (headers or {}).get("Authorization", "")
        return _PairResp(200, {"devices": []}) if bearer == f"Bearer {_PAIR_TOKEN}" else _PairResp(401, {})

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    yield wire
    supervisor._probe_cache.clear()
    supervisor._auth_cache.clear()


def test_pair_route_success_registers_the_member_without_echoing_the_token(client, pair_wire):
    r = client.post("/api/fleet/remotes/pair", json={"url": "http://100.64.0.5:7870", "code": "ABCDE-12345"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["action"] == "added" and body["agent"]["name"] == "ava"
    assert body["reachable"] is True and body["auth"] == "ok"
    assert _PAIR_TOKEN not in r.text
    ((claim_url, sent),) = pair_wire["posts"]
    assert claim_url == "http://100.64.0.5:7870/api/pairing/claim" and sent["code"] == "ABCDE-12345"
    fleet = client.get("/api/fleet")
    row = next(a for a in fleet.json()["agents"] if a.get("remote"))
    assert row["name"] == "ava" and row["auth"] == "ok" and _PAIR_TOKEN not in fleet.text


def test_pair_route_invalid_code_is_400_and_registers_nothing(client, pair_wire):
    pair_wire["claim"] = _PairResp(403, {"ok": False, "error": "invalid or expired pairing code"})
    r = client.post("/api/fleet/remotes/pair", json={"url": "http://100.64.0.5:7870", "code": "WRONG-CODE0"})
    assert r.status_code == 400 and "invalid or expired" in r.json()["detail"]
    assert not any(a.get("remote") for a in client.get("/api/fleet").json()["agents"])


def test_pair_route_unreachable_remote_is_502(client, pair_wire):
    import httpx

    pair_wire["claim"] = httpx.ConnectError("connection refused")
    r = client.post("/api/fleet/remotes/pair", json={"url": "http://100.64.0.5:7870", "code": "ABCDE-12345"})
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]
    assert not any(a.get("remote") for a in client.get("/api/fleet").json()["agents"])


def test_pair_route_refuses_plain_http_on_a_lan_before_dialling(client, pair_wire):
    """ADR 0113 D10: the code and the token it turns into would cross the LAN in cleartext.
    Refused with a 400 that names the fix, and nothing is sent; the explicit opt-in pairs."""
    lan = "http://192.168.1.20:7870"
    r = client.post("/api/fleet/remotes/pair", json={"url": lan, "code": "ABCDE-12345"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "cleartext" in detail and "tailnet" in detail
    assert pair_wire["posts"] == []
    r = client.post("/api/fleet/remotes/pair", json={"url": lan, "code": "ABCDE-12345", "allow_insecure": True})
    assert r.status_code == 200, r.text
    assert len(pair_wire["posts"]) == 1


# ── Held (preview) archetypes + "From a bundle URL" (new-agent sources) ─────────────


def _with_held(monkeypatch, held):
    from operator_api import fleet_routes

    monkeypatch.setattr(fleet_routes, "_load_held_archetypes", lambda: held)
    monkeypatch.setattr("graph.plugins.installer._read_lock", lambda: {})


_HELD = [
    {
        "_held": "Josh tests first.",
        "id": "analyst",
        "label": "Analyst",
        "icon": "ChartColumn",
        "bundle": "https://github.com/protoLabsAI/analyst-archetype",
        "blurb": "Answers questions from your data files.",
        "soul": "# Analyst",
        "requires_tools": ["data_query"],
    }
]


def test_held_archetypes_hidden_by_default(client, monkeypatch):
    _with_held(monkeypatch, _HELD)
    arr = client.get("/api/archetypes").json()["archetypes"]
    assert "analyst" not in [a["id"] for a in arr]
    assert not any(a.get("held") for a in arr)


def test_held_archetypes_only_on_explicit_include_held(client, monkeypatch):
    _with_held(monkeypatch, _HELD)
    arr = client.get("/api/archetypes?include_held=1").json()["archetypes"]
    by_id = {a["id"]: a for a in arr}
    assert by_id["analyst"]["held"] is True
    assert by_id["analyst"]["requires_tools"] == ["data_query"]
    assert "_held" not in by_id["analyst"]  # the curator's note is never served
    assert not any(a.get("held") for a in arr if a["id"] != "analyst")  # only held rows are flagged
    assert arr[-1]["id"] == "custom"  # Custom stays LAST


def test_held_archetype_dedupes_against_installed_bundle(client, monkeypatch):
    from operator_api import fleet_routes

    monkeypatch.setattr(fleet_routes, "_load_held_archetypes", lambda: _HELD)
    monkeypatch.setattr(
        "graph.plugins.installer._read_lock",
        lambda: {
            "bundles": [
                {
                    "id": "analyst-archetype",
                    "source_url": "https://github.com/protoLabsAI/analyst-archetype.git",
                    "archetype": {"label": "Analyst"},
                }
            ]
        },
    )
    arr = client.get("/api/archetypes?include_held=1").json()["archetypes"]
    assert [a["id"] for a in arr if a["label"] == "Analyst"] == ["analyst-archetype"]  # installed row wins
    assert not any(a.get("held") for a in arr)


def test_held_archetype_preview_resolves(client, monkeypatch):
    _with_held(monkeypatch, _HELD)
    import ops.plugins as plugin_ops

    async def _fake_peek(url, ref=None):
        return {"kind": "bundle", "id": "analyst-archetype", "members": []}

    monkeypatch.setattr(plugin_ops, "peek_bundle", _fake_peek)
    assert client.get("/api/archetypes/analyst/preview").json()["bundle"]["id"] == "analyst-archetype"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "https://github.com/onlyowner",
        "http://github.com/a/b",
        "file:///etc/passwd",
        "/tmp/local/repo",
        "https://github.com/a/../b",
        "https://github.com/a/b?x=1",
        "--upload-pack=evil",
    ],
)
def test_from_url_rejects_non_git_urls(client, monkeypatch, url):
    import ops.plugins as plugin_ops

    async def _never(*a, **k):
        raise AssertionError("must not fetch an invalid URL")

    monkeypatch.setattr(plugin_ops, "peek_bundle", _never)
    r = client.get("/api/archetypes/from-url", params={"url": url})
    assert r.status_code == 400


def test_from_url_rejects_bad_ref(client):
    r = client.get("/api/archetypes/from-url", params={"url": "https://github.com/a/b", "ref": "-x;rm"})
    assert r.status_code == 400


def test_from_url_peeks_and_shapes_an_archetype(client, monkeypatch):
    import ops.plugins as plugin_ops
    from runtime.state import STATE

    seen = {}

    async def _fake_peek(url, ref=None):
        seen.update(url=url, ref=ref)
        return {
            "kind": "bundle",
            "id": "analyst-archetype",
            "name": "Analyst bundle",
            "description": "Data analysis.",
            "members": [{"id": "data", "builtin": False, "ref": "v0.1.0"}, {"id": "notes", "builtin": True}],
            "config_inputs": [{"key": "data.data_dirs", "label": "Data folders", "type": "string"}],
            "archetype": {"label": "Analyst", "icon": "ChartColumn", "blurb": "Answers from data.", "soul": "# A"},
        }

    monkeypatch.setattr(plugin_ops, "peek_bundle", _fake_peek)
    cfg = type("C", (), {"plugins_sources_official": ["github.com/protoLabsAI/*"], "plugins_sources_acked": []})()
    monkeypatch.setattr(STATE, "graph_config", cfg, raising=False)
    r = client.get(
        "/api/archetypes/from-url", params={"url": " https://github.com/protoLabsAI/analyst-archetype ", "ref": "v0.1.0"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert seen == {"url": "https://github.com/protoLabsAI/analyst-archetype", "ref": "v0.1.0"}
    arch = body["archetype"]
    assert body["id"] == arch["id"] == "analyst-archetype"
    assert arch["label"] == "Analyst" and arch["blurb"] == "Answers from data." and arch["soul"] == "# A"
    assert arch["bundle"] == "https://github.com/protoLabsAI/analyst-archetype"
    assert arch["ref"] == "v0.1.0"
    assert body["bundle"]["members"][0]["ref"] == "v0.1.0"  # the full peek rides along
    assert body["trusted"] is True and body["source"] == "github.com/protoLabsAI/analyst-archetype"


def test_from_url_untrusted_source_and_no_archetype_block(client, monkeypatch):
    import ops.plugins as plugin_ops
    from runtime.state import STATE

    async def _fake_peek(url, ref=None):
        return {"kind": "plugin", "members": [{"id": "thing", "name": "Thing", "description": "A plugin."}]}

    monkeypatch.setattr(plugin_ops, "peek_bundle", _fake_peek)
    cfg = type("C", (), {"plugins_sources_official": ["github.com/protoLabsAI/*"], "plugins_sources_acked": []})()
    monkeypatch.setattr(STATE, "graph_config", cfg, raising=False)
    body = client.get("/api/archetypes/from-url", params={"url": "git@github.com:acme/thing.git"}).json()
    assert body["trusted"] is False
    assert body["archetype"]["label"] == "Thing" and body["archetype"]["blurb"] == "A plugin."
    assert "ref" not in body["archetype"]


def test_from_url_fetch_failure_is_502(client, monkeypatch):
    import ops.plugins as plugin_ops

    async def _boom(url, ref=None):
        raise RuntimeError("repo not found")

    monkeypatch.setattr(plugin_ops, "peek_bundle", _boom)
    r = client.get("/api/archetypes/from-url", params={"url": "https://github.com/a/b"})
    assert r.status_code == 502 and "repo not found" in r.json()["detail"]


def test_create_forwards_bundle_ref(client, monkeypatch):
    from graph.workspaces import manager

    captured: dict = {}

    def fake_create(name, **kwargs):
        captured.update(kwargs)
        return {"id": f"{name}-0000", "name": name, "port": 7999, "path": "/tmp/x", "installed": []}

    monkeypatch.setattr(manager, "create", fake_create)
    r = client.post(
        "/api/fleet",
        json={"name": "pinned", "start": False, "bundle": "https://github.com/x/stack", "ref": "v0.1.0"},
    )
    assert r.status_code == 200
    assert captured["bundle_ref"] == "v0.1.0"

    captured.clear()
    client.post("/api/fleet", json={"name": "unpinned", "start": False, "bundle": "https://github.com/x/stack"})
    assert captured["bundle_ref"] is None


@pytest.mark.parametrize("body", [{"ref": "v1"}, {"bundle": "https://github.com/x/stack", "ref": "--evil"}])
def test_create_rejects_bad_ref(client, monkeypatch, body):
    from graph.workspaces import manager

    monkeypatch.setattr(manager, "create", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create")))
    assert client.post("/api/fleet", json={"name": "x", "start": False, **body}).status_code == 400
