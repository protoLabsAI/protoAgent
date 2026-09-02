"""Tests for the orgchart plugin — roster assembly, the cached crawl, and the view contract."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from graph.plugins.testkit import FakeRegistry
from plugins.orgchart import register
from plugins.orgchart import topology as topo


@pytest.fixture(autouse=True)
def _fresh_caches():
    topo.reset()
    yield
    topo.reset()


# ── register() wiring ────────────────────────────────────────────────────────


def test_register_mounts_page_and_data_routers():
    reg = FakeRegistry(plugin_id="orgchart")
    register(reg)
    prefixes = {p for (p, _r) in reg.routers}
    assert prefixes == {"/plugins/orgchart", "/api/plugins/orgchart"}
    paths = {r.path for (_p, router) in reg.routers for r in router.routes}
    assert {"/view", "/topology"} <= paths


def test_view_page_is_self_contained_and_kit_backed():
    from plugins.orgchart.view import _VIEW_PAGE

    page = _VIEW_PAGE.read_text(encoding="utf-8")
    # Single-file on purpose: a split-out sub-resource would need public_paths (the
    # artifact plugin's documented fleet-wide 401 incident).
    assert "plugin-kit.js" in page and "initPluginView" in page
    assert '<script src="/plugins/' not in page
    assert "/api/plugins/orgchart/topology" in page


# ── helpers ──────────────────────────────────────────────────────────────────


def test_norm_strips_a2a_suffix_and_slashes():
    assert topo._norm("http://x:7870/a2a/") == "http://x:7870"
    assert topo._norm("http://x:7870/") == "http://x:7870"
    assert topo._norm("") == ""


def test_short_strips_label_and_caps():
    assert topo._short("Roxy — Ecosystem PM over three repos. More text.") == "Ecosystem PM over three repos"
    assert len(topo._short("x" * 100)) <= 34


def test_targets_scope_and_token_resolution(monkeypatch):
    monkeypatch.setenv("PEER_TOK", "sekrit")
    monkeypatch.delenv("UNSET_TOK", raising=False)
    dels = [
        {"type": "a2a", "name": "a", "url": "http://a:1/a2a", "auth": {"token": "inline"}, "scope": "host"},
        {"type": "a2a", "name": "b", "url": "http://b:1", "auth": {"credentialsEnv": "PEER_TOK"}},
        # both fields: the stored/overlaid token wins — the SAME order dispatch
        # (adapters._secret) uses, so the chart authenticates the way delegate_to would
        {"type": "a2a", "name": "both", "url": "http://e:1", "auth": {"credentialsEnv": "PEER_TOK", "token": "stored"}},
        # no stored value, env var named but unset: resolves to nothing (matches dispatch)
        {"type": "a2a", "name": "unset", "url": "http://f:1", "auth": {"credentialsEnv": "UNSET_TOK"}},
        {"type": "a2a", "name": "redacted", "url": "http://c:1"},  # peer-reported: no auth
        {"type": "a2a", "name": "nourl"},
    ]
    edges = topo._targets(dels, owner="http://self:1")
    assert [(e["name"], e["token"], e["scope"]) for e in edges] == [
        ("a", "inline", "host"),
        ("b", "sekrit", None),
        ("both", "stored", None),
        ("unset", "", None),
        ("redacted", "", None),
    ]


# ── acp / model leaves (#3325) ───────────────────────────────────────────────


def test_acp_is_owner_scoped_and_model_merges_by_endpoint():
    """A coder is a subprocess on ITS owner's box, so the same name on two agents is two
    nodes. A model endpoint is shared infrastructure, so the same url+model is one."""
    dels = [
        {"type": "acp", "name": "coder", "command": "proto", "workdir": "/w"},
        {"type": "openai", "name": "brain", "url": "https://gw.example/v1/", "model": "big"},
    ]
    mine = topo._targets(dels, owner="http://self:1")
    theirs = topo._targets(dels, owner="http://peer:1")
    assert mine[0]["id"] == "acp:http://self:1#coder" != theirs[0]["id"]
    assert mine[1]["id"] == theirs[1]["id"] == "model:gw.example/v1/big"
    assert (mine[0]["kind"], mine[1]["kind"]) == ("coder", "model")
    # A /v1 path must survive: _norm's trailing-/a2a strip is an A2A convention.
    assert mine[1]["endpoint"] == "https://gw.example/v1"


def test_incomplete_acp_and_model_entries_are_skipped():
    assert topo._targets([{"type": "acp", "name": "c"}], owner="o") == []  # no command
    assert topo._targets([{"type": "openai", "name": "m", "url": "u"}], owner="o") == []  # no model


def test_leaf_types_are_gated_by_config():
    dels = [
        {"type": "acp", "name": "coder", "command": "proto", "workdir": "/w"},
        {"type": "openai", "name": "brain", "url": "https://gw/v1", "model": "big"},
    ]
    assert len(topo._targets(dels, owner="o")) == 2  # both default on
    off = {"include_acp": False, "include_model_endpoints": False}
    assert topo._targets(dels, owner="o", cfg=off) == []
    only_acp = topo._targets(dels, owner="o", cfg={"include_model_endpoints": False})
    assert [t["kind"] for t in only_acp] == ["coder"]


def test_secret_bearing_fields_never_reach_the_payload(monkeypatch):
    """`_roster()` returns entries with secrets OVERLAID, so a node built from the raw
    dict would ship the api key to the browser. Every display field is copied by name."""
    calls: list[str] = []
    _wire(
        monkeypatch,
        calls,
        roster=[
            {"type": "openai", "name": "brain", "url": "https://gw/v1", "model": "big", "api_key": "sk-LEAK"},
            {"type": "acp", "name": "coder", "command": "proto", "workdir": "/w", "env": {"TOKEN": "sh-LEAK"}},
            {"type": "a2a", "name": "alpha", "url": ALPHA, "auth": {"token": "tok-alpha"}},
        ],
    )
    import json

    blob = json.dumps(asyncio.run(topo.get_topology({})))
    assert "LEAK" not in blob and "tok-alpha" not in blob


def test_leaves_are_never_probed_or_crawled(monkeypatch):
    """A coder has no listener and a model endpoint's /models is the delegates prober's
    job — a page refresh must not spawn a subprocess or bill a gateway."""
    calls: list[str] = []
    _wire(
        monkeypatch,
        calls,
        roster=[
            {"type": "acp", "name": "coder", "command": "proto", "workdir": "/w"},
            {"type": "openai", "name": "brain", "url": "https://gw/v1", "model": "big"},
        ],
    )
    data = asyncio.run(topo.get_topology({}))
    assert calls == []  # no card, no /healthz, no /models, no /api/delegates
    assert {n["kind"] for n in data["nodes"]} == {"self", "coder", "model"}
    assert data["include"] == {"acp": True, "model": True}


def test_leaf_liveness_is_the_owners_probe_result(monkeypatch):
    """Ours comes from the delegates health snapshot by NAME; a peer's comes from the
    `health` its own /api/delegates reports. No result at all is `up: None` — "unknown",
    which is not the claim "down"."""
    calls: list[str] = []
    _wire(
        monkeypatch,
        calls,
        roster=[
            {"type": "acp", "name": "coder", "command": "proto", "workdir": "/w"},
            {"type": "acp", "name": "ghost", "command": "nope", "workdir": "/w"},
            {"type": "openai", "name": "brain", "url": "https://gw/v1", "model": "big"},
        ],
        health={"coder": {"ok": True, "latency_ms": 12, "checked_at": 99.0}},
    )
    nodes = {n["id"]: n for n in asyncio.run(topo.get_topology({}))["nodes"]}
    SELF = "http://self:7870"
    assert nodes[f"acp:{SELF}#coder"]["up"] is True
    assert nodes[f"acp:{SELF}#coder"]["latency_ms"] == 12
    assert nodes[f"acp:{SELF}#ghost"]["up"] is None  # never probed ≠ down
    assert nodes["model:gw/v1/big"]["up"] is None


def test_peer_reported_health_carries_the_error_for_its_leaves(monkeypatch):
    """A peer's coder is invisible to our prober, so its reported health is the only
    source — and WHY it is red is actionable and otherwise unrecoverable from here."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "alpha":
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json={"name": "Alpha"})
            if request.url.path == "/api/delegates":
                return httpx.Response(
                    200,
                    json={
                        "delegates": [
                            {
                                "type": "acp",
                                "name": "coder",
                                "command": "claude",
                                "workdir": "/repo",
                                "health": {"ok": False, "error": "binary not on PATH: 'claude'"},
                            }
                        ]
                    },
                )
        raise httpx.ConnectError("down")

    monkeypatch.setattr(topo, "_make_client", lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(topo, "_roster", lambda: [{"type": "a2a", "name": "alpha", "url": ALPHA, "auth": {"token": "t"}}])
    # OUR prober also knows a `coder` — a different process. It must not badge the peer's.
    monkeypatch.setattr(topo, "_health", lambda: {"coder": {"ok": True, "latency_ms": 5}})
    monkeypatch.setattr(topo, "_fleet_remotes", lambda: [])
    monkeypatch.setattr(topo, "_self_identity", lambda: ("ava", "http://self:7870", "org head"))

    nodes = {n["id"]: n for n in asyncio.run(topo.get_topology({}))["nodes"]}
    peer_coder = nodes[f"acp:{ALPHA}#coder"]
    assert peer_coder["up"] is False and "not on PATH" in peer_coder["error"]
    assert peer_coder.get("latency_ms") is None  # our snapshot never leaked across


def test_a_peers_health_never_overrides_our_own_probe_of_an_agent(monkeypatch):
    """A peer's `/api/delegates` reports health for its a2a delegates too. Trusting it
    would paint a node GREEN that this agent cannot reach — the one claim the chart must
    never make. Only leaves, which nothing here can probe, take a peer's word."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "alpha":
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json={"name": "Alpha"})
            if request.url.path == "/api/delegates":
                # alpha reaches beta fine; we cannot (the handler refuses beta below).
                return httpx.Response(
                    200,
                    json={"delegates": [{"type": "a2a", "name": "beta", "url": BETA,
                                         "health": {"ok": True, "latency_ms": 3}}]},
                )
        raise httpx.ConnectError("down")

    monkeypatch.setattr(topo, "_make_client", lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(topo, "_roster", lambda: [{"type": "a2a", "name": "alpha", "url": ALPHA, "auth": {"token": "t"}}])
    monkeypatch.setattr(topo, "_health", lambda: {})
    monkeypatch.setattr(topo, "_fleet_remotes", lambda: [])
    monkeypatch.setattr(topo, "_self_identity", lambda: ("ava", "http://self:7870", "org head"))

    nodes = {n["id"]: n for n in asyncio.run(topo.get_topology({}))["nodes"]}
    assert nodes[BETA]["up"] is False  # unreachable from HERE, whatever alpha thinks
    assert nodes[BETA].get("latency_ms") is None


# ── the crawl ────────────────────────────────────────────────────────────────

ALPHA = "http://alpha:7870"
BETA = "http://beta:7870"
GAMMA = "http://gamma:7870"


def _wire(monkeypatch, calls, roster=None, health=None, remotes=None):
    """Point topology at a fake fleet: alpha up (with one delegate of its own), beta
    dead, gamma a leaf. Records every request URL in ``calls``."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "alpha":
            if path == "/.well-known/agent-card.json":
                return httpx.Response(200, json={"name": "Alpha", "version": "1.2", "description": "Alpha — planner. Etc."})
            if path == "/api/delegates":
                assert request.headers.get("Authorization") == "Bearer tok-alpha"
                return httpx.Response(200, json={"delegates": [{"type": "a2a", "name": "gamma", "url": GAMMA + "/a2a", "description": "leaf"}]})
        if host == "gamma" and path == "/.well-known/agent-card.json":
            return httpx.Response(200, json={"name": "Gamma"})
        raise httpx.ConnectError("down")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(topo, "_make_client", lambda cfg: httpx.AsyncClient(transport=transport))
    monkeypatch.setattr(
        topo,
        "_roster",
        lambda: roster
        if roster is not None
        else [
            {"type": "a2a", "name": "alpha", "url": ALPHA + "/a2a", "auth": {"token": "tok-alpha"}, "scope": "host"},
            {"type": "a2a", "name": "beta", "url": BETA, "description": "beta — QA agent. Etc."},
        ],
    )
    monkeypatch.setattr(topo, "_health", lambda: health or {})
    monkeypatch.setattr(topo, "_fleet_remotes", lambda: remotes or [])
    monkeypatch.setattr(topo, "_self_identity", lambda: ("ava", "http://self:7870", "org head"))


def test_crawl_walks_tokened_peers_and_marks_dead_ones(monkeypatch):
    calls: list[str] = []
    _wire(monkeypatch, calls)
    data = asyncio.run(topo.get_topology({}))
    nodes = {n["id"]: n for n in data["nodes"]}
    assert set(nodes) == {"http://self:7870", ALPHA, BETA, GAMMA}
    assert nodes[ALPHA]["up"] and nodes[ALPHA]["name"] == "Alpha" and nodes[ALPHA]["role"] == "planner"
    assert not nodes[BETA]["up"] and nodes[BETA]["role"] == "QA agent"
    assert nodes[GAMMA]["up"]  # discovered through alpha, probed, but not crawled (no token)
    edge_set = {(e["from"], e["to"]) for e in data["edges"]}
    assert edge_set == {("http://self:7870", ALPHA), ("http://self:7870", BETA), (ALPHA, GAMMA)}
    host_edge = next(e for e in data["edges"] if e["to"] == ALPHA)
    assert host_edge.get("scope") == "host"  # fleet-shared entries keep their layer
    assert data["stale"] is False and data["count"] == 4
    assert not any("gamma" in u and "delegates" in u for u in calls)  # tokenless → leaf


def test_snapshot_serves_from_cache_within_ttl(monkeypatch):
    calls: list[str] = []
    _wire(monkeypatch, calls)
    asyncio.run(topo.get_topology({}))
    n = len(calls)
    again = asyncio.run(topo.get_topology({}))
    assert len(calls) == n  # zero network within the TTL
    assert again["stale"] is False


def test_stale_snapshot_returns_immediately_and_revalidates(monkeypatch):
    calls: list[str] = []
    _wire(monkeypatch, calls)

    async def main():
        first = await topo.get_topology({})
        topo._snap["expires"] = 0.0  # age the snapshot out
        stale = await topo.get_topology({})
        assert stale["stale"] is True  # served instantly, from the old crawl
        assert stale["count"] == first["count"]
        await topo._refresh_task  # background single-flight rebuild
        fresh = await topo.get_topology({})
        assert fresh["stale"] is False

    asyncio.run(main())


def test_dead_peer_is_negatively_cached(monkeypatch):
    calls: list[str] = []
    _wire(monkeypatch, calls)

    async def main():
        await topo.get_topology({})
        beta_probes = len([u for u in calls if "beta" in u])
        topo._snap["expires"] = 0.0
        await topo.get_topology({})
        await topo._refresh_task
        # the re-crawl reused the negative card-cache entry instead of re-timing-out
        assert len([u for u in calls if "beta" in u]) == beta_probes

    asyncio.run(main())


def test_force_refresh_drops_all_caches(monkeypatch):
    calls: list[str] = []
    _wire(monkeypatch, calls)

    async def main():
        await topo.get_topology({})
        n = len(calls)
        forced = await topo.get_topology({}, force=True)
        assert len(calls) > n  # caches dropped → real re-crawl
        assert forced["stale"] is False

    asyncio.run(main())


def test_health_snapshot_supplies_liveness_without_reprobing(monkeypatch):
    calls: list[str] = []
    _wire(monkeypatch, calls, health={"beta": {"ok": True, "latency_ms": 42, "checked_at": 123.0}})
    data = asyncio.run(topo.get_topology({}))
    beta = next(n for n in data["nodes"] if n["id"] == BETA)
    # the card probe failed (beta answers nothing) but the prober's snapshot vouches
    assert beta["up"] and beta["latency_ms"] == 42 and beta["checked_at"] == 123.0


def test_fleet_members_join_with_member_edges_and_tokens(monkeypatch):
    calls: list[str] = []
    _wire(
        monkeypatch,
        calls,
        roster=[],
        remotes=[{"id": "alpha", "name": "alpha", "url": ALPHA, "token": "tok-alpha"}],
    )
    data = asyncio.run(topo.get_topology({}))
    edge = next(e for e in data["edges"] if e["to"] == ALPHA)
    assert edge["kind"] == "member"  # supervised, not delegated-to
    # the stored member token still widened the crawl to alpha's own delegates
    assert {(e["from"], e["to"]) for e in data["edges"]} >= {(ALPHA, GAMMA)}


def test_fleet_members_can_be_excluded(monkeypatch):
    calls: list[str] = []
    _wire(monkeypatch, calls, roster=[], remotes=[{"id": "alpha", "url": ALPHA, "token": "t"}])
    data = asyncio.run(topo.get_topology({"include_fleet_members": False}))
    assert [n["kind"] for n in data["nodes"]] == ["self"]


def test_max_nodes_caps_the_crawl(monkeypatch):
    calls: list[str] = []
    roster = [{"type": "a2a", "name": f"n{i}", "url": f"http://n{i}:1"} for i in range(10)]
    _wire(monkeypatch, calls, roster=roster)
    data = asyncio.run(topo.get_topology({"max_nodes": 4}))
    assert data["count"] == 4
