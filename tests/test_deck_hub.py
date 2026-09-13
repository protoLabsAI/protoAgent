"""deck.hub — the fleet deck's hub client (#3467, epic #3466).

Covers the three jobs the module docstring promises: finding a hub by evidence (pidfile,
heartbeats across box roots, default), opening it (the credential chain, tried until the
roster reads), and talking to it (typed errors, and the 401 scoping that keeps a MEMBER's
credential problem from reading as the hub's).
"""

from __future__ import annotations

import json
import os

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


def test_normalize_url_strips_userinfo_and_rejects_garbage():
    # a secret in --hub must never reach an error message or a header
    assert hub.normalize_url("http://user:secret@ava.tail:7870/") == "http://ava.tail:7870"
    assert "secret" not in hub.normalize_url("https://user:secret@ava.tail:7870")
    for bad in ("http://[::1", "ftp://x:1", "http://"):
        with pytest.raises(ValueError):
            hub.normalize_url(bad)


def test_normalize_url_reject_path_never_echoes_userinfo():
    """Round-2 blocker: the reject path formatted the RAW input, so `--hub ftp://user:s3cret@host`
    printed the secret. Every ValueError message must be redacted."""
    for bad in ("ftp://user:s3cret@host", "http://user:s3cret@", "user:s3cret@:7870", "http://user:s3cret@[::1"):
        with pytest.raises(ValueError) as ei:
            hub.normalize_url(bad)
        assert "s3cret" not in str(ei.value), bad
    assert hub.redact_url("http://user:s3cret@host:1/x") == "http://***@host:1/x"
    assert hub.redact_url("user:s3cret@host") == "http://***@host"
    assert hub.redact_url("http://host:1") == "http://host:1"


def test_is_loopback():
    assert hub.is_loopback("http://127.0.0.1:7870")
    assert hub.is_loopback("http://localhost:7870")
    assert hub.is_loopback("http://[::1]:7870")
    assert not hub.is_loopback("http://ava.tail:7870")
    assert not hub.is_loopback("http://100.119.239.8:7870")


def test_infra_paths_are_late_bound(monkeypatch, tmp_path):
    """The suite's `_isolate_instance_roots` patches infra.paths at runtime; an import-time
    `from infra.paths import data_home` would keep pointing at the developer's real
    ~/.protoagent and the first un-patched test would read real heartbeats + tokens."""
    from infra import paths as real

    monkeypatch.setattr(real, "data_home", lambda: tmp_path / "isolated")
    assert hub.data_home() == tmp_path / "isolated"


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
    # the desktop heartbeat on 7870 is deduped into the pidfile row, and the default 7870
    # candidate is not repeated either
    assert [(c.url, c.source) for c in cands] == [
        ("http://127.0.0.1:7870", "pidfile"),
        ("http://127.0.0.1:7875", "heartbeat"),
    ]
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


def test_token_chain_sends_no_local_credential_off_box(tmp_path, monkeypatch):
    """`--hub evil:7870` must not harvest this box's fleet service token or operator bearer:
    a non-loopback hub gets the explicit/env credential and open mode, nothing else."""
    (tmp_path / "workspaces").mkdir()
    (tmp_path / "workspaces" / hub.FLEET_TOKEN_FILE).write_text("LOCAL-FLEET-SECRET")

    class _Paths:
        instance_root = tmp_path

    monkeypatch.setattr(hub, "instance_paths", lambda: _Paths())
    monkeypatch.setattr(hub, "known_box_roots", lambda: [tmp_path])
    monkeypatch.setenv(hub.ENV_OPERATOR_BEARER, "LOCAL-OPERATOR-BEARER")
    monkeypatch.setenv(hub.ENV_TOKEN, "from-env")
    remote = hub.HubCandidate("http://evil.example.com:7870", "flag")
    assert list(hub.token_chain(remote, explicit="explicit")) == ["explicit", "from-env", None]
    local = hub.HubCandidate("http://127.0.0.1:7870", "default")
    assert list(hub.token_chain(local)) == ["from-env", "LOCAL-FLEET-SECRET", "LOCAL-OPERATOR-BEARER", None]


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


def test_client_403_is_a_credential_rejection_too():
    """A federation-tier token in PROTOAGENT_HUB_TOKEN gets a 403 on /api — that is
    "wrong credential", so the chain must keep walking, not report a hub failure."""
    c = hub.HubClient("http://127.0.0.1:7870", "fed", transport=_transport(lambda r: httpx.Response(403)))
    with pytest.raises(hub.HubUnauthorized):
        c.fleet()


def test_client_4xx_without_detail_falls_back_to_body_text():
    c = hub.HubClient("http://127.0.0.1:7870", transport=_transport(lambda r: httpx.Response(500, json={"error": "boom"})))
    with pytest.raises(hub.HubRequestError) as ei:
        c.fleet()
    assert ei.value.detail and ei.value.detail != "None"
    c2 = hub.HubClient("http://127.0.0.1:7870", transport=_transport(lambda r: httpx.Response(502, text="bad gateway")))
    with pytest.raises(hub.HubRequestError) as ei2:
        c2.fleet()
    assert ei2.value.detail == "bad gateway"


def test_client_torn_connection_is_unreachable_not_a_traceback():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected", request=request)

    c = hub.HubClient("http://127.0.0.1:7870", transport=_transport(handler))
    with pytest.raises(hub.HubUnreachable):
        c.fleet()


def test_lifecycle_calls_get_a_long_read_budget():
    """The hub boot-watches a start for 10 s, busy-waits a stop for 10 s, and /down does
    that for every running member in sequence — a 5 s read timeout reported a working hub
    as unreachable (and dropped the results collected so far)."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.extensions.get("timeout", {}).get("read")
        if request.url.path == "/api/fleet":
            return httpx.Response(200, json={"agents": []})
        return httpx.Response(200, json={"ok": True, "stopped": []})

    c = hub.HubClient("http://127.0.0.1:7870", transport=_transport(handler))
    c.start("a")
    c.stop("a")
    c.down(running=5)
    c.fleet()
    assert seen["/api/fleet/a/start"] >= 60
    assert seen["/api/fleet/a/stop"] >= 60
    assert seen["/api/fleet/down"] >= 5 * hub._DOWN_PER_MEMBER_S
    assert seen["/api/fleet"] == 5.0


def test_credential_over_plain_http_off_box_is_refused_unless_opted_in():
    """CWE-319: a bearer must not travel in cleartext to another host. Loopback http and
    any https are fine; --insecure-http is the operator's explicit opt-in (a tailnet)."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_bearer(request))
        return httpx.Response(200, json={"agents": []})

    with pytest.raises(hub.InsecureHub) as ei:
        hub.HubClient("http://ava.tail:7870", "tok", transport=_transport(handler))
    assert "tok" not in str(ei.value) and "plain http" in str(ei.value)
    assert seen == []  # nothing was sent at all
    hub.HubClient("https://ava.tail:7870", "tok", transport=_transport(handler)).fleet()
    hub.HubClient("http://127.0.0.1:7870", "tok", transport=_transport(handler)).fleet()
    hub.HubClient("http://ava.tail:7870", None, transport=_transport(handler)).fleet()  # no credential → fine
    hub.HubClient("http://ava.tail:7870", "tok", transport=_transport(handler), insecure_http=True).fleet()
    assert seen == ["tok", "tok", None, "tok"]


def test_connect_reports_the_insecure_refusal_as_answered(tmp_path, monkeypatch):
    _no_disk_tokens(monkeypatch, tmp_path)
    cand = hub.HubCandidate("http://ava.tail:7870", "flag")
    with pytest.raises(hub.NoHub) as ei:
        hub.connect(url="ava.tail:7870", token="tok", candidates=[cand], transport=_transport(lambda r: _card_ok(r) or httpx.Response(200, json={"agents": []})))
    assert ei.value.answered and "plain http" in str(ei.value)
    conn = hub.connect(url="ava.tail:7870", token="tok", candidates=[cand], transport=_transport(lambda r: _card_ok(r) or httpx.Response(200, json={"agents": []})), insecure_http=True)
    conn.client.close()


def test_client_never_follows_a_redirect_with_the_bearer():
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://evil.example.com/api/fleet"})

    c = hub.HubClient("http://127.0.0.1:7870", "tok", transport=_transport(handler))
    with pytest.raises(hub.HubRequestError) as ei:
        c.fleet()
    assert ei.value.status == 302 and hops == ["http://127.0.0.1:7870/api/fleet"]


def test_malformed_2xx_lifecycle_bodies_are_hub_errors():
    """A proxy's HTML or an empty body on 200 must not read as "started" (CodeRabbit)."""
    html = hub.HubClient("http://127.0.0.1:7870", transport=_transport(lambda r: httpx.Response(200, text="<html>proxy</html>")))
    for call in (lambda: html.start("a"), lambda: html.stop("a"), lambda: html.down(), lambda: html.fleet(), lambda: html.runtime_status()):
        with pytest.raises(hub.HubError) as ei:
            call()
        assert "malformed" in str(ei.value)
    empty = hub.HubClient("http://127.0.0.1:7870", transport=_transport(lambda r: httpx.Response(200)))
    with pytest.raises(hub.HubError):
        empty.start("a")
    bad_roster = hub.HubClient("http://127.0.0.1:7870", transport=_transport(lambda r: httpx.Response(200, json={"agents": "nope"})))
    with pytest.raises(hub.HubError):
        bad_roster.fleet()


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


def test_connect_records_a_hub_that_answered_but_could_not_be_read(tmp_path, monkeypatch):
    """The HIGH finding of the S0 review: card 200 + /api/fleet ReadTimeout used to read as
    "no hub answered", and the CLI then drove the supervisor beside the running hub."""
    _no_disk_tokens(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if (r := _card_ok(request)) is not None:
            return r
        raise httpx.ReadTimeout("slow", request=request)

    cand = hub.HubCandidate("http://127.0.0.1:7870", "default")
    with pytest.raises(hub.NoHub) as ei:
        hub.connect(candidates=[cand], transport=_transport(handler))
    assert ei.value.answered is True
    assert list(ei.value.failed) == ["http://127.0.0.1:7870"]
    assert "could not be read" in str(ei.value)

    def handler_500(request: httpx.Request) -> httpx.Response:
        return _card_ok(request) or httpx.Response(500, json={"detail": "task store exploded"})

    with pytest.raises(hub.NoHub) as ei:
        hub.connect(candidates=[cand], transport=_transport(handler_500))
    assert ei.value.answered is True
    assert "task store exploded" in str(ei.value)


def test_connect_counts_a_live_pid_with_no_card_as_answered(tmp_path, monkeypatch):
    """A pidfile/heartbeat candidate whose server is booting or stalled (probe times out)
    is a RUNNING hub — the CLI must not read it as "nothing answered" and go to disk."""
    _no_disk_tokens(monkeypatch, tmp_path)
    monkeypatch.setattr(hub, "pid_alive", lambda pid: pid == 4242)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("stalled", request=request)

    live = hub.HubCandidate("http://127.0.0.1:7871", "heartbeat", pid=4242)
    dead = hub.HubCandidate("http://127.0.0.1:7872", "heartbeat", pid=4343)  # pid gone → genuinely nothing
    with pytest.raises(hub.NoHub) as ei:
        hub.connect(candidates=[live, dead], transport=_transport(handler))
    assert ei.value.answered is True
    assert list(ei.value.failed) == ["http://127.0.0.1:7871"]
    assert "pid 4242" in str(ei.value)
    with pytest.raises(hub.NoHub) as ei:
        hub.connect(candidates=[dead], transport=_transport(handler))
    assert ei.value.answered is False


def test_connect_treats_403_as_rejected_credential_and_keeps_walking(tmp_path, monkeypatch):
    _no_disk_tokens(monkeypatch, tmp_path)
    monkeypatch.setenv(hub.ENV_OPERATOR_BEARER, "operator")
    tried: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (r := _card_ok(request)) is not None:
            return r
        tok = _bearer(request)
        tried.append(tok)
        return httpx.Response(200, json={"agents": []}) if tok == "operator" else httpx.Response(403)

    cand = hub.HubCandidate("http://127.0.0.1:7870", "default")
    conn = hub.connect(token="federation-token", candidates=[cand], transport=_transport(handler))
    assert tried == ["federation-token", "operator"]
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

    # ...even when the operator NAMED it: `fleet down x --hub <member>` would drive
    # lifecycle through a server that has no such member. Point at the hub instead.
    with pytest.raises(hub.NoHub) as ei:
        hub.connect(url="127.0.0.1:7871", candidates=[member], transport=_transport(handler))
    assert ei.value.members == ["http://127.0.0.1:7871"] and ei.value.answered


def test_connect_no_candidates_answering_says_so(tmp_path, monkeypatch):
    _no_disk_tokens(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(hub.NoHub) as ei:
        hub.connect(candidates=[hub.HubCandidate("http://127.0.0.1:7870", "default")], transport=_transport(handler))
    assert ei.value.unauthorized == []
    assert ei.value.answered is False  # the only case the CLI may fall back to disk on
    assert "no hub answered at http://127.0.0.1:7870" in str(ei.value)


# ── layering ──────────────────────────────────────────────────────────────────


def test_every_manage_path_encodes_its_id_as_one_segment_dots_included():
    """CodeRabbit (S2) for turns; the same for every id the manage calls put in a path —
    httpx collapses `.`/`..` segments, so a bare quote() would DELETE /api/fleet for `..`."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(200, json={"ok": True, "agent": {}, "archetypes": [], "order": []})

    c = hub.HubClient("http://127.0.0.1:7870", "tok", transport=httpx.MockTransport(handler))
    c.remove("..", purge=True)
    c.rename(".", "x")
    c.remote_remove("../..")
    c.remote_update("a.b", url="u")
    c.start("..")
    paths = [p for _, p in seen]
    assert paths == ["/api/fleet/%2E%2E?purge=true", "/api/fleet/%2E", "/api/fleet/remotes/%2E%2E%2F%2E%2E", "/api/fleet/remotes/a%2Eb", "/api/fleet/%2E%2E/start"]
    assert hub.segment("chat-1.2/3") == "chat-1%2E2%2F3"
