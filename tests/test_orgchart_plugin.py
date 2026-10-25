"""Tests for the orgchart plugin — roster assembly, the cached crawl, and the view contract."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from graph.plugins.testkit import FakeRegistry
from plugins.orgchart import register
from plugins.orgchart import topology as topo


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    # The topology overlays delegation rows from ``STATE.ledger_store``. A test that booted
    # the agent earlier on this xdist worker leaves a real ledger there, and anything that
    # delegated since (a background ``researcher`` job) then draws as extra ``subagent:*``
    # nodes — node counts and crawl caps here drift with test order (seen on #3939's CI).
    # Start every test with no ledger; a test of the work overlay sets its own.
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "ledger_store", None, raising=False)
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
    from plugins.orgchart.view import view_page

    page = view_page()
    # Authored as three files, served as ONE document: a split-out sub-resource would need
    # public_paths (the artifact plugin's documented fleet-wide 401 incident).
    assert "__ORGCHART_CSS__" not in page and "__ORGCHART_JS__" not in page
    assert "plugin-kit.js" in page and "initPluginView" in page
    assert '<script src="/plugins/' not in page and '<link rel="stylesheet" href="/plugins/' not in page
    assert "/api/plugins/orgchart/topology" in page
    # Live: it follows the ledger's in-flight edges on the bus, with a catch-up endpoint.
    assert '"delegation.#"' in page and "/api/ledger/live" in page


def test_action_buttons_use_the_ds_plugin_kit_classes():
    """protoContent#551 (card 5): ACTION buttons must be the DS button. This view is plain
    HTML backed by the kit's CSS, so Refresh and the drawer's Close carry `pl-btn` classes —
    the ds_audit hand-rolled-control rule flags a raw styled <button>, not these."""
    from plugins.orgchart.view import view_page

    page = view_page()
    assert '<button class="pl-btn pl-btn--sm pl-btn--ghost" type="button" id="refresh"' in page
    assert 'title="Drop caches and re-crawl the fleet"' in page
    assert '<button class="pl-btn pl-btn--ghost pl-btn--icon pl-btn--sm" type="button" id="d-close" aria-label="Close"' in page
    # The script binds both controls by id.
    assert '$("refresh")' in page and '$("d-close")' in page


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
        # (delegates base._secret) uses, so the chart authenticates the way delegate_to would
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


def test_delegate_through_the_hub_proxy_is_the_fleet_member_not_a_twin(monkeypatch):
    """The "Add as delegate" button on a paired remote writes the hub-proxy URL (ADR 0113 D4). That
    delegate and the member are one agent: one node, a delegate edge, crawled with the
    member's stored token."""
    calls: list[str] = []
    health = {"alpha": {"ok": True, "latency_ms": 7, "checked_at": 1.0}}
    _wire(
        monkeypatch,
        calls,
        roster=[{"type": "a2a", "name": "alpha", "url": "http://127.0.0.1:7870/agents/alpha-b8bb/a2a"}],
        health=health,
        remotes=[{"id": "alpha-b8bb", "name": "alpha", "url": ALPHA, "token": "tok-alpha"}],
    )
    data = asyncio.run(topo.get_topology({}))
    nodes = {n["id"]: n for n in data["nodes"]}
    assert set(nodes) == {"http://self:7870", ALPHA, GAMMA}
    assert nodes[ALPHA]["latency_ms"] == 7  # our prober's health still applies by name
    edges = [(e["from"], e["to"], e["kind"]) for e in data["edges"]]
    assert ("http://self:7870", ALPHA, "delegate") in edges
    assert not any(k == "member" for _, _, k in edges)
    assert (ALPHA, GAMMA, "delegate") in edges  # crawled with the member's token


def test_a_direct_delegate_outranks_a_proxied_one_for_the_same_members_health(monkeypatch):
    calls: list[str] = []
    _wire(
        monkeypatch,
        calls,
        # the proxied entry FIRST, so roster order can't be what picks the winner
        roster=[
            {"type": "a2a", "name": "alpha-proxy", "url": "http://127.0.0.1:7870/agents/alpha-b8bb"},
            {"type": "a2a", "name": "alpha-direct", "url": ALPHA, "auth": {"token": "tok-alpha"}},
        ],
        health={
            "alpha-proxy": {"ok": False, "latency_ms": 999, "error": "x"},
            "alpha-direct": {"ok": True, "latency_ms": 1},
        },
        remotes=[{"id": "alpha-b8bb", "name": "alpha", "url": ALPHA, "token": "tok-alpha"}],
    )
    data = asyncio.run(topo.get_topology({}))
    alpha = [n for n in data["nodes"] if n["id"] == ALPHA]
    assert len(alpha) == 1 and alpha[0]["latency_ms"] == 1 and "error" not in alpha[0]


def _wire_local_peer(monkeypatch, peer: str, peer_delegates: list[dict]):
    """This agent delegates to one tokened peer at ``peer``, which reports
    ``peer_delegates``; alpha is a paired remote of this hub."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(peer) and request.url.path == "/api/delegates":
            return httpx.Response(200, json={"delegates": peer_delegates})
        if request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json={"name": request.url.host})
        raise httpx.ConnectError("down")

    monkeypatch.setattr(topo, "_make_client", lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(topo, "_roster", lambda: [{"type": "a2a", "name": "peer", "url": peer, "auth": {"token": "t"}}])
    monkeypatch.setattr(topo, "_health", lambda: {})
    monkeypatch.setattr(
        topo, "_fleet_remotes", lambda: [{"id": "alpha-b8bb", "name": "alpha", "url": ALPHA, "token": "tok-alpha"}]
    )
    monkeypatch.setattr(topo, "_self_identity", lambda: ("hub", "http://127.0.0.1:7870", "org head"))


def test_a_local_peers_proxied_delegate_is_the_member_too(monkeypatch):
    peer = "http://127.0.0.1:7881"  # a local fleet member on the hub's box
    _wire_local_peer(monkeypatch, peer, [{"type": "a2a", "name": "alpha", "url": "http://127.0.0.1:7870/agents/alpha-b8bb/a2a"}])
    data = asyncio.run(topo.get_topology({}))
    ids = {n["id"] for n in data["nodes"]}
    assert "http://127.0.0.1:7870/agents/alpha-b8bb" not in ids
    assert (peer, ALPHA) in {(e["from"], e["to"]) for e in data["edges"]}


def test_a_remote_peers_loopback_url_is_its_own_box_not_ours(monkeypatch):
    peer = "http://peer:7870"  # another machine: its 127.0.0.1 is itself
    proxied = "http://127.0.0.1:7870/agents/alpha-b8bb"
    _wire_local_peer(monkeypatch, peer, [{"type": "a2a", "name": "x", "url": proxied + "/a2a"}])
    data = asyncio.run(topo.get_topology({}))
    assert (peer, proxied) in {(e["from"], e["to"]) for e in data["edges"]}


def test_hub_proxy_rid_only_matches_loopback_agent_paths():
    assert topo._hub_proxy_rid("http://[::1]:7870/agents/v") == "v"
    assert topo._hub_proxy_rid("http://127.0.0.1:7870/agents/vera-b8bb") == "vera-b8bb"
    assert topo._hub_proxy_rid("http://localhost:7870/agents/x") == "x"
    assert topo._hub_proxy_rid("http://100.64.1.2:7870/agents/x") == ""
    assert topo._hub_proxy_rid("http://127.0.0.1:7870/agents/x/sub") == ""
    assert topo._hub_proxy_rid("http://127.0.0.1:7870") == ""


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


def test_leaves_do_not_starve_the_agent_graph_under_max_nodes(monkeypatch):
    """Leaves spend the same node budget as agents, so roster ORDER could decide whether
    the chart shows the delegation graph at all. Agents are taken first within a layer."""
    calls: list[str] = []
    _wire(
        monkeypatch,
        calls,
        roster=[
            {"type": "acp", "name": "c1", "command": "proto", "workdir": "/w"},
            {"type": "acp", "name": "c2", "command": "proto", "workdir": "/w"},
            {"type": "acp", "name": "c3", "command": "proto", "workdir": "/w"},
            {"type": "a2a", "name": "alpha", "url": ALPHA, "auth": {"token": "tok-alpha"}},
        ],
    )
    # self + 2 more: the peer must be one of them, whatever order the roster listed.
    nodes = {n["id"]: n for n in asyncio.run(topo.get_topology({"max_nodes": 3}))["nodes"]}
    assert ALPHA in nodes, "a peer lost its slot to coders listed ahead of it"
    assert len(nodes) == 3


# ── live work across the fleet ───────────────────────────────────────────────


def _wire_fleet(monkeypatch, calls):
    """alpha (tokened, readable) reports live work, a ledger and its aggregates; beta is
    dead. Every ledger read must carry alpha's bearer — the crawl's own, never a new one."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "alpha":
            if path == "/.well-known/agent-card.json":
                return httpx.Response(200, json={"name": "Alpha"})
            assert request.headers.get("Authorization") == "Bearer tok-alpha"
            if path == "/api/delegates":
                return httpx.Response(200, json={"delegates": [{"type": "acp", "name": "coder", "command": "claude"}]})
            if path == "/api/ledger/live":
                return httpx.Response(200, json={"edges": [{"id": "e1", "to_kind": "acp", "to_name": "coder", "what": "fix it", "started_at": 1.0}]})
            if path == "/api/ledger":
                return httpx.Response(200, json={"edges": [{"edge_id": 9, "to_kind": "acp", "to_name": "coder", "outcome": "ok"}]})
            if path == "/api/ledger/edges":
                return httpx.Response(200, json={"edges": [{"to_kind": "acp", "to_name": "coder", "dispatches": 4, "ok": 4}]})
        raise httpx.ConnectError("down")

    monkeypatch.setattr(topo, "_make_client", lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(
        topo,
        "_roster",
        lambda: [
            {"type": "a2a", "name": "alpha", "url": ALPHA + "/a2a", "auth": {"token": "tok-alpha"}},
            {"type": "a2a", "name": "beta", "url": BETA},
        ],
    )
    monkeypatch.setattr(topo, "_health", lambda: {})
    monkeypatch.setattr(topo, "_fleet_remotes", lambda: [])
    monkeypatch.setattr(topo, "_self_identity", lambda: ("ava", "http://self:7870", "org head"))
    monkeypatch.setattr(topo, "_ledger_edges", lambda: [])


def test_live_fans_in_each_readable_peers_work_tagged_with_its_owner(monkeypatch):
    from graph import ledger

    calls: list[str] = []
    _wire_fleet(monkeypatch, calls)
    monkeypatch.setattr(ledger, "live_edges", lambda: [{"id": "mine", "to_kind": "subagent", "to_name": "researcher"}])
    data = asyncio.run(topo.get_live({}))
    owners = {e["id"]: e["owner"] for e in data["edges"]}
    assert owners == {"mine": "http://self:7870", "e1": ALPHA}
    assert data["peers"] == 1  # beta is dead and tokenless: never asked
    assert not any("beta" in u and "/api/ledger" in u for u in calls)


def test_a_peers_coder_wears_the_peers_record_and_its_history_is_readable(monkeypatch):
    calls: list[str] = []
    _wire_fleet(monkeypatch, calls)
    data = asyncio.run(topo.get_topology({}))
    coder = next(n for n in data["nodes"] if n["id"] == f"acp:{ALPHA}#coder")
    assert coder["work"]["dispatches"] == 4
    hist = asyncio.run(topo.get_history({}, ALPHA + "/a2a"))
    assert hist["readable"] is True and hist["edges"][0]["edge_id"] == 9
    # A node the crawl couldn't read has no history to give — and no token is invented.
    assert asyncio.run(topo.get_history({}, BETA)) == {"owner": BETA, "edges": [], "readable": False}


def test_a_peers_live_edges_are_whitelisted_and_typed():
    """A peer's payload is untrusted input to every console that renders it: unknown keys
    are dropped and numbers are numbers — a string `started_at` can't smuggle markup."""
    dirty = {
        "id": "e1", "to_kind": "acp", "to_name": "coder", "started_at": '"><img src=x onerror=alert(1)>',
        "evil": "<script>", "progress": {"tool_count": "<b>9</b>", "plan": [{"content": "x", "status": "pending", "x": 1}],
                                         "current_tool": {"name": "Edit", "kind": "edit", "locations": [{"path": "a.py", "line": "<i>"}]}},
    }
    e = topo._clean_live(dirty, "http://peer")
    assert "evil" not in e and e["owner"] == "http://peer"
    assert isinstance(e["started_at"], float)
    assert e["progress"]["tool_count"] == 0 and e["progress"]["plan"] == [{"content": "x", "status": "pending"}]
    assert e["progress"]["current_tool"]["locations"] == [{"path": "a.py"}]
    assert topo._clean_live({"to_name": "no id"}, "http://peer") is None


def test_a_missed_live_read_is_retried_within_seconds(monkeypatch):
    """One slow answer from a busy member must not blank its work for half a minute —
    the topology's 30 s negative cache is for crawling, not for watching."""
    assert topo._LIVE_NEG_TTL <= 5 < topo._NEG_TTL


# ── local members: crawled with the fleet service token, like dispatch ──────────────


def _local_member_wire(monkeypatch, roster, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization", "")))
        if request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json={"name": "projectManager"})
        if request.url.path == "/api/delegates":
            if request.headers.get("authorization") != "Bearer fleet-tok":
                return httpx.Response(401)
            return httpx.Response(200, json={"delegates": [{"type": "acp", "name": "codex", "command": "codex-acp"}]})
        raise httpx.ConnectError("down")

    monkeypatch.setattr(topo, "_make_client", lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(topo, "_roster", lambda: roster)
    monkeypatch.setattr(topo, "_health", lambda: {})
    monkeypatch.setattr(topo, "_fleet_remotes", lambda: [])
    monkeypatch.setattr(topo, "_fleet_token", lambda: "fleet-tok")
    monkeypatch.setattr(topo, "_self_identity", lambda: ("protoagent", "http://127.0.0.1:7870", "org head"))


def test_a_tokenless_local_member_is_crawled_with_the_fleet_token(monkeypatch):
    # The hub's "Add as delegate" row for a local member has no auth: dispatch presents the
    # fleet service token (ADR 0089 D4). The crawl didn't, so projectManager's coders never drew.
    seen: list = []
    pm = "http://127.0.0.1:7875"
    _local_member_wire(monkeypatch, [{"type": "a2a", "name": "projectManager", "url": pm + "/a2a"}], seen)
    topo_ = asyncio.run(topo.get_topology({}))
    nodes = {n["id"]: n for n in topo_["nodes"]}
    assert f"acp:{pm}#codex" in nodes
    assert {"from": pm, "to": f"acp:{pm}#codex", "kind": "delegate"} in topo_["edges"]


def test_the_fleet_token_never_leaves_the_box(monkeypatch):
    seen: list = []
    _local_member_wire(monkeypatch, [{"type": "a2a", "name": "far", "url": "http://10.0.0.9:7870/a2a"}], seen)
    asyncio.run(topo.get_topology({}))
    assert seen and all("fleet-tok" not in auth for _url, auth in seen)
    assert not any(url.endswith("/api/delegates") for url, _auth in seen)  # tokenless off-box peer: not crawled


def test_empty_overlay_can_actually_hide():
    """The empty-state overlay sets ``display: grid``, which beats the ``hidden`` attribute —
    so once shown (every node type filtered off) it stayed over the graph until a refresh."""
    from pathlib import Path

    view = Path(__file__).resolve().parents[1] / "plugins" / "orgchart" / "view"
    css = (view / "orgchart.css").read_text()
    js = (view / "orgchart.js").read_text()
    assert ".empty[hidden] { display: none; }" in css
    # Hiding it also drops the stale text, and a filter-emptied chart says so.
    assert '$("empty").innerHTML = !empty ? ""' in js
    assert "Everything is filtered out" in js


def test_leaves_group_under_their_owner_and_fold_but_work_never_hides():
    # Josh 10-25: one shared leaf column fanned every agent's coders into one stack; with
    # projectManager's eight coders crawled it became unreadable. The view lays out a tree
    # (each agent's leaves beside it) and folds an agent's leaves into one chip.
    from pathlib import Path

    js = (Path(topo.__file__).parent / "view" / "orgchart.js").read_text()
    assert "function treeParents(g)" in js and "function fold(g)" in js
    assert "GRAPH = fold(buildGraph());" in js
    # Working, just-finished and selected leaves stay out of the fold.
    assert "for (const f of g.flows) { active.add(f.to); active.add(f.from); }" in js
    assert "if (SEL) active.add(SEL);" in js
    # The operator's unfold choice is remembered; a feed click still reaches a folded leaf.
    assert 'const OPEN_KEY = "orgchart.unfolded";' in js
    assert "GRAPH.folded?.has(n.id)" in js


def test_the_canvas_zooms_and_pans_like_a_map():
    # Josh 10-25: with the tree laid out, a big fleet must be navigable, not scrolled.
    from pathlib import Path

    view = Path(topo.__file__).parent / "view"
    js, css, html = ((view / f).read_text() for f in ("orgchart.js", "orgchart.css", "orgchart.html"))
    assert "const CAM = { x: 0, y: 0, k: 1, user: false };" in js
    assert "CAM.user ? applyCam() : fitCam();" in js  # a poll never yanks a view the operator moved
    assert 'c.addEventListener("wheel"' in js and "zoomAt(Math.exp(-e.deltaY * px * rate), ...at(e));" in js
    assert 'c.addEventListener("gesturechange"' in js  # the desktop webview's pinch
    assert 'e.target.closest(".node, .cam")' in js  # a press on a node stays a click
    assert 'id="zoom-in"' in html and 'id="zoom-out"' in html and 'id="zoom-fit"' in html
    assert ".canvas { overflow: hidden; cursor: grab; touch-action: none; }" in css
