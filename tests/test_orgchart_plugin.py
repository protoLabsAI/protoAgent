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


def test_a2a_edges_scope_and_token_resolution(monkeypatch):
    monkeypatch.setenv("PEER_TOK", "sekrit")
    dels = [
        {"type": "a2a", "name": "a", "url": "http://a:1/a2a", "auth": {"token": "inline"}, "scope": "host"},
        {"type": "a2a", "name": "b", "url": "http://b:1", "auth": {"credentialsEnv": "PEER_TOK"}},
        {"type": "a2a", "name": "redacted", "url": "http://c:1"},  # peer-reported: no auth
        {"type": "acp", "name": "coder", "url": "http://d:1"},  # non-a2a: skipped
        {"type": "a2a", "name": "nourl"},
    ]
    edges = topo._a2a_edges(dels)
    assert [(e["name"], e["token"], e["scope"]) for e in edges] == [
        ("a", "inline", "host"),
        ("b", "sekrit", None),
        ("redacted", "", None),
    ]


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
