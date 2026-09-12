"""deck.hub — the fleet deck's hub client (#3467, epic #3466).

Covers the three jobs the module docstring promises: finding a hub by evidence (pidfile,
heartbeats across box roots, default), opening it (the credential chain, tried until the
roster reads), and talking to it (typed errors, and the 401 scoping that keeps a MEMBER's
credential problem from reading as the hub's).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from deck import hub


# ── helpers ───────────────────────────────────────────────────────────────────


def _card_ok(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/.well-known/agent-card.json":
        return httpx.Response(200, json={"name": "hub-under-test"})
    return None


def _bearer(request: httpx.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    return auth.removeprefix("Bearer ").strip() or None


def _transport(handler):
    return httpx.MockTransport(handler)


# ── candidates ────────────────────────────────────────────────────────────────


def test_normalize_url_adds_scheme_and_strips_slash():
    assert hub.normalize_url("127.0.0.1:7870/") == "http://127.0.0.1:7870"
    assert hub.normalize_url("https://ava.tail:7870") == "https://ava.tail:7870"
    with pytest.raises(ValueError):
        hub.normalize_url("   ")


def test_known_box_roots_dedupes_and_includes_existing_desktop_root(tmp_path, monkeypatch):
    box = tmp_path / "box"
    box.mkdir()
    desktop = tmp_path / "desktop-appdata"
    desktop.mkdir()
    monkeypatch.setattr(hub, "box_root", lambda: box)
    monkeypatch.setattr(hub, "data_home", lambda: box)  # same as box_root → deduped
    monkeypatch.setattr(hub, "desktop_box_roots", lambda: [desktop, tmp_path / "missing"])
    roots = hub.known_box_roots()
    assert roots == [box.resolve(), desktop.resolve()]


def test_desktop_box_root_is_the_tauri_app_data_dir(monkeypatch):
    roots = hub.desktop_box_roots()
    assert len(roots) == 1
    assert roots[0].name == hub.DESKTOP_APP_ID


def test_read_heartbeats_skips_dead_and_own_pid_and_never_unlinks(tmp_path, monkeypatch):
    d = tmp_path / ".instances"
    d.mkdir()
    live, dead = 4242, 4343
    (d / f"{live}.json").write_text(json.dumps({"pid": live, "port": 7901, "identity": "ava", "instance_root": "/x/ava"}))
    (d / f"{dead}.json").write_text(json.dumps({"pid": dead, "port": 7902}))
    (d / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "port": 7903}))
    (d / "junk.json").write_text("{}")
    monkeypatch.setattr(hub, "pid_alive", lambda pid: pid == live)
    rows = hub.read_heartbeats(tmp_path)
    assert rows == [{"pid": live, "port": 7901, "identity": "ava", "instance_root": "/x/ava"}]
    # read-only: the stale record is still on disk (pruning is the owning server's job)
    assert (d / f"{dead}.json").exists()


def test_discover_hubs_orders_pidfile_heartbeats_default_and_dedupes(tmp_path, monkeypatch):
    own_root = tmp_path / "own"
    own_root.mkdir()
    (own_root / "server.pid").write_text(json.dumps({"pid": 111, "port": 7870, "version": "0.1"}))

    class _Paths:
        instance_root = own_root

    monkeypatch.setattr(hub, "instance_paths", lambda: _Paths())
    monkeypatch.setattr(hub, "pid_alive", lambda pid: True)
    desktop = tmp_path / "desktop"
    (desktop / ".instances").mkdir(parents=True)
    (desktop / ".instances" / "222.json").write_text(
        json.dumps({"pid": 222, "port": 7870, "identity": "protoagent", "instance_root": str(desktop)})
    )
    (desktop / ".instances" / "333.json").write_text(
        json.dumps({"pid": 333, "port": 7875, "identity": "protoEngineer", "instance_root": str(desktop / "ws")})
    )
    monkeypatch.setattr(hub, "known_box_roots", lambda: [desktop])

    cands = hub.discover_hubs()
    assert [(c.url, c.source) for c in cands] == [
        ("http://127.0.0.1:7870", "pidfile"),  # the desktop heartbeat on 7870 is deduped away
        ("http://127.0.0.1:7875", "heartbeat"),
    ] + [("http://127.0.0.1:7870", "default")][:0]  # default 7870 already present → not repeated
    assert cands[1].identity == "protoEngineer"
    assert cands[1].instance_root == desktop / "ws"


def test_discover_hubs_skips_member_heartbeats(tmp_path, monkeypatch):
    """A member is a full server: it writes a heartbeat and serves /api/fleet. Its
    instance root carries the supervisor's workspace.yaml — that is the tell."""

    class _Paths:
        instance_root = tmp_path / "nowhere"

    monkeypatch.setattr(hub, "instance_paths", lambda: _Paths())
    monkeypatch.setattr(hub, "pid_alive", lambda pid: True)
    box = tmp_path / "box"
    member_root = box / "workspaces" / "killteamCoach-0416"
    member_root.mkdir(parents=True)
    (member_root / hub.WORKSPACE_MARKER).write_text("name: killteamCoach\n")
    (box / ".instances").mkdir()
    (box / ".instances" / "50009.json").write_text(
        json.dumps({"pid": 50009, "port": 7871, "identity": "killteamCoach", "instance_root": str(member_root)})
    )
    (box / ".instances" / "14971.json").write_text(
        json.dumps({"pid": 14971, "port": 7870, "identity": "protoagent", "instance_root": str(box)})
    )
    monkeypatch.setattr(hub, "known_box_roots", lambda: [box])
    cands = hub.discover_hubs()
    assert [(c.url, c.source) for c in cands] == [("http://127.0.0.1:7870", "heartbeat")]


def test_discover_hubs_explicit_url_short_circuits(monkeypatch):
    monkeypatch.setattr(hub, "known_box_roots", lambda: (_ for _ in ()).throw(AssertionError("must not scan")))
    cands = hub.discover_hubs(explicit_url="ava.tail:7870")
    assert [(c.url, c.source) for c in cands] == [("http://ava.tail:7870", "flag")]


def test_discover_hubs_falls_back_to_default_port(tmp_path, monkeypatch):
    class _Paths:
        instance_root = tmp_path

    monkeypatch.setattr(hub, "instance_paths", lambda: _Paths())
    monkeypatch.setattr(hub, "known_box_roots", lambda: [])
    cands = hub.discover_hubs()
    assert [(c.url, c.source) for c in cands] == [(f"http://127.0.0.1:{hub.DEFAULT_PORT}", "default")]


# ── credentials ───────────────────────────────────────────────────────────────


def test_token_chain_order_and_dedupe(tmp_path, monkeypatch):
    cand_root = tmp_path / "cand"
    own_root = tmp_path / "own"
    box = tmp_path / "box"
    for r, tok in ((cand_root, "fleet-cand"), (own_root, "fleet-own"), (box, "fleet-box")):
        (r / "workspaces").mkdir(parents=True)
        (r / "workspaces" / hub.FLEET_TOKEN_FILE).write_text(tok + "\n")

    class _Paths:
        instance_root = own_root

    monkeypatch.setattr(hub, "instance_paths", lambda: _Paths())
    monkeypatch.setattr(hub, "known_box_roots", lambda: [box, own_root])  # own_root file deduped
    monkeypatch.setenv(hub.ENV_TOKEN, "from-env")
    monkeypatch.setenv(hub.ENV_OPERATOR_BEARER, "operator-bearer")
    cand = hub.HubCandidate("http://127.0.0.1:7870", "heartbeat", instance_root=cand_root)

    chain = list(hub.token_chain(cand, explicit="explicit"))
    assert chain == ["explicit", "from-env", "fleet-cand", "fleet-own", "fleet-box", "operator-bearer", None]

    # same value from two sources is yielded once
    monkeypatch.setenv(hub.ENV_TOKEN, "fleet-cand")
    chain = list(hub.token_chain(cand))
    assert chain == ["fleet-cand", "fleet-own", "fleet-box", "operator-bearer", None]


def test_token_chain_without_any_source_is_open_mode_only(tmp_path, monkeypatch):
    class _Paths:
        instance_root = tmp_path

    monkeypatch.setattr(hub, "instance_paths", lambda: _Paths())
    monkeypatch.setattr(hub, "known_box_roots", lambda: [])
    monkeypatch.delenv(hub.ENV_TOKEN, raising=False)
    monkeypatch.delenv(hub.ENV_OPERATOR_BEARER, raising=False)
    assert list(hub.token_chain(hub.HubCandidate("http://127.0.0.1:7870", "default"))) == [None]


# ── the client ────────────────────────────────────────────────────────────────


def test_client_sends_bearer_and_parses_roster():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = _bearer(request)
        seen["path"] = request.url.path
        return httpx.Response(200, json={"agents": [{"name": "a", "running": True}]})

    c = hub.HubClient("http://127.0.0.1:7870", "tok", transport=_transport(handler))
    assert c.fleet() == [{"name": "a", "running": True}]
    assert seen == {"auth": "tok", "path": "/api/fleet"}
    assert c.authenticated


def test_client_401_on_hub_path_is_hub_unauthorized():
    c = hub.HubClient("http://127.0.0.1:7870", "bad", transport=_transport(lambda r: httpx.Response(401)))
    with pytest.raises(hub.HubUnauthorized) as ei:
        c.fleet()
    assert "bad" not in str(ei.value)  # the credential never leaks into the message


def test_client_401_on_agents_path_is_member_unauthorized_with_slug():
    c = hub.HubClient("http://127.0.0.1:7870", "tok", transport=_transport(lambda r: httpx.Response(401)))
    with pytest.raises(hub.MemberUnauthorized) as ei:
        c._request("GET", "/agents/roxy-e815/api/runtime/status")
    assert ei.value.slug == "roxy-e815"
    assert not isinstance(ei.value, hub.HubUnauthorized)


def test_client_4xx_carries_detail_and_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "workspace busy — repeat to finish"})

    c = hub.HubClient("http://127.0.0.1:7870", transport=_transport(handler))
    with pytest.raises(hub.HubRequestError) as ei:
        c.stop("roxy")
    assert ei.value.status == 409
    assert ei.value.detail == "workspace busy — repeat to finish"


def test_client_connect_failure_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    c = hub.HubClient("http://127.0.0.1:7999", transport=_transport(handler))
    with pytest.raises(hub.HubUnreachable):
        c.fleet()
    assert c.agent_card() is None


def test_client_lifecycle_routes_quote_names():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.method} {request.url.raw_path.decode()}")
        return httpx.Response(200, json={"ok": True})

    c = hub.HubClient("http://127.0.0.1:7870", transport=_transport(handler))
    c.start("job coach")
    c.stop("roxy")
    c.down()
    assert paths == ["POST /api/fleet/job%20coach/start", "POST /api/fleet/roxy/stop", "POST /api/fleet/down"]


def test_agent_card_probe_sends_no_credential():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return _card_ok(request) or httpx.Response(404)

    c = hub.HubClient("http://127.0.0.1:7870", "tok", transport=_transport(handler))
    assert c.agent_card() == {"name": "hub-under-test"}
    assert seen["auth"] is None


# ── connect ───────────────────────────────────────────────────────────────────


def _no_disk_tokens(monkeypatch, tmp_path):
    class _Paths:
        instance_root = tmp_path

    monkeypatch.setattr(hub, "instance_paths", lambda: _Paths())
    monkeypatch.setattr(hub, "known_box_roots", lambda: [])
    monkeypatch.delenv(hub.ENV_TOKEN, raising=False)
    monkeypatch.delenv(hub.ENV_OPERATOR_BEARER, raising=False)


def test_connect_walks_the_token_chain_until_the_roster_reads(tmp_path, monkeypatch):
    _no_disk_tokens(monkeypatch, tmp_path)
    monkeypatch.setenv(hub.ENV_OPERATOR_BEARER, "the-right-one")
    tried: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (r := _card_ok(request)) is not None:
            return r
        tok = _bearer(request)
        tried.append(tok)
        if tok == "the-right-one":
            return httpx.Response(200, json={"agents": []})
        return httpx.Response(401)

    cand = hub.HubCandidate("http://127.0.0.1:7870", "default")
    conn = hub.connect(token="wrong-flag", candidates=[cand], transport=_transport(handler))
    assert tried == ["wrong-flag", "the-right-one"]
    assert conn.card == {"name": "hub-under-test"}
    assert conn.candidate is cand
    assert conn.client.authenticated
    conn.client.close()


def test_connect_skips_non_hubs_and_reports_unauthorized_separately(tmp_path, monkeypatch):
    _no_disk_tokens(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        port = request.url.port
        if port == 7001:  # nothing protoAgent-shaped here
            return httpx.Response(404)
        if port == 7002:  # a hub that rejects everything
            return _card_ok(request) or httpx.Response(401)
        raise httpx.ConnectError("refused", request=request)

    cands = [
        hub.HubCandidate("http://127.0.0.1:7001", "heartbeat"),
        hub.HubCandidate("http://127.0.0.1:7002", "heartbeat"),
        hub.HubCandidate("http://127.0.0.1:7003", "default"),
    ]
    with pytest.raises(hub.NoHub) as ei:
        hub.connect(candidates=cands, transport=_transport(handler))
    assert ei.value.tried == [c.url for c in cands]
    assert ei.value.unauthorized == ["http://127.0.0.1:7002"]
    assert "rejected every credential" in str(ei.value)
    assert hub.ENV_TOKEN in str(ei.value)


def test_connect_skips_a_member_that_answers_as_a_fleet_of_itself(tmp_path, monkeypatch):
    """Seen live 2026-09-12: `fleet down killteamCoach` landed on killteamCoach ITSELF
    (:7871), whose /api/fleet is just its own host row — "'killteamCoach' is not running".
    The host row of a member is stamped `member: True`; connect() must skip it."""
    _no_disk_tokens(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if (r := _card_ok(request)) is not None:
            return r
        if request.url.port == 7871:
            return httpx.Response(200, json={"agents": [{"name": "killteamCoach", "host": True, "member": True, "running": True}]})
        return httpx.Response(200, json={"agents": [{"name": "protoagent", "host": True, "running": True}]})

    member = hub.HubCandidate("http://127.0.0.1:7871", "heartbeat")
    real = hub.HubCandidate("http://127.0.0.1:7870", "default")
    conn = hub.connect(candidates=[member, real], transport=_transport(handler))
    assert conn.candidate is real
    conn.client.close()

    with pytest.raises(hub.NoHub) as ei:
        hub.connect(candidates=[member], transport=_transport(handler))
    assert ei.value.members == ["http://127.0.0.1:7871"]
    assert "only fleet MEMBERS answered" in str(ei.value)

    # ...unless the operator NAMED it: --hub is explicit, and they may want the member's own view.
    conn = hub.connect(url="127.0.0.1:7871", candidates=[member], transport=_transport(handler))
    assert conn.candidate is member
    conn.client.close()


def test_connect_no_candidates_answering_says_so(tmp_path, monkeypatch):
    _no_disk_tokens(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(hub.NoHub) as ei:
        hub.connect(candidates=[hub.HubCandidate("http://127.0.0.1:7870", "default")], transport=_transport(handler))
    assert ei.value.unauthorized == []
    assert "no hub answered at http://127.0.0.1:7870" in str(ei.value)


# ── layering ──────────────────────────────────────────────────────────────────


def test_deck_imports_nothing_from_the_runtime_it_manages():
    """The deck is a separate process talking HTTP: no server/, operator_api/, or graph/.
    (lint-imports guards the first two; graph is a design rule this test keeps honest.)"""
    src = Path(hub.__file__).read_text(encoding="utf-8")
    for forbidden in ("import server", "from server", "import operator_api", "from operator_api", "import graph", "from graph"):
        assert forbidden not in src, forbidden
