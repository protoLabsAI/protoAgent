"""Hub-side agent pairing + the authenticated remote probe (ADR 0113 D1/D5/D7-hub).

The hub redeems a code the remote's operator minted against the remote's EXISTING
``POST /api/pairing/claim`` (ADR 0087 D4), stores the per-device token it gets back as the
member's bearer, and then checks that token with one cheap operator-gated GET so a revoked
hub reads ``auth: rejected`` instead of a green dot that 401s. The wire to the remote is
faked at ``httpx.post`` / ``httpx.get`` — the same seam the existing probe tests use.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from graph.fleet import supervisor

URL = "http://100.64.0.5:7870"
TOKEN = "dev-tok-SECRET-9f8e7d"  # the minted device token — must never leak anywhere


class _Resp:
    def __init__(self, status: int, body=None, text: str | None = None):
        self.status_code = status
        self._body = body
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._body


class FakeRemote:
    """A remote protoAgent's three relevant routes: the claim, the card, the auth probe."""

    def __init__(self, *, claim=None, card_name="Ava Agent", version="0.183.1", accept=(TOKEN,), auth_status=None):
        self.claim = (
            claim
            if claim is not None
            else _Resp(200, {"ok": True, "device": {"id": "d-1", "name": "x"}, "token": TOKEN})
        )
        self.card_name = card_name
        self.version = version
        self.accept = set(accept)
        self.auth_status = auth_status  # force a status on /api/devices (None = check the bearer)
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []

    def post(self, url, json=None, timeout=None, **kw):
        self.posts.append((url, dict(json or {})))
        if isinstance(self.claim, Exception):
            raise self.claim
        return self.claim

    def get(self, url, timeout=None, headers=None, **kw):
        self.gets.append((url, dict(headers or {})))
        if url.endswith("/.well-known/agent-card.json"):
            return _Resp(200, {"name": self.card_name, "version": self.version})
        if url.endswith(supervisor._AUTH_PROBE_PATH):
            if self.auth_status is not None:
                return _Resp(self.auth_status, {})
            bearer = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
            return _Resp(200, {"devices": []}) if bearer in self.accept else _Resp(401, {"detail": "Unauthorized"})
        return _Resp(404, {})

    def auth_probes(self) -> int:
        return sum(1 for u, _ in self.gets if u.endswith(supervisor._AUTH_PROBE_PATH))


@pytest.fixture
def remote(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    supervisor._probe_cache.clear()
    supervisor._auth_cache.clear()
    fake = FakeRemote()
    monkeypatch.setattr(httpx, "post", fake.post)
    monkeypatch.setattr(httpx, "get", fake.get)
    yield fake
    supervisor._probe_cache.clear()
    supervisor._auth_cache.clear()


# ── pair_remote ──────────────────────────────────────────────────────────────


def test_pair_adds_a_new_remote_named_after_its_card(remote, caplog):
    caplog.set_level(logging.DEBUG)
    out = supervisor.pair_remote(URL + "/", "ABCDE-12345")

    # The claim went to the remote's existing route with the code and a name that says
    # which hub this is (what the remote's Devices list will show).
    ((claim_url, body),) = remote.posts
    assert claim_url == f"{URL}/api/pairing/claim"
    assert body["code"] == "ABCDE-12345" and body["name"].endswith("(fleet hub)")

    assert out["action"] == "added"
    agent = out["agent"]
    assert agent["name"] == "Ava_Agent" and agent["url"] == URL  # card name coerced to the member charset
    assert out["reachable"] is True and out["version"] == "0.183.1" and out["auth"] == "ok"
    # The token is stored for the proxy — and returned nowhere.
    assert supervisor.remote_for_slug(agent["id"])["token"] == TOKEN
    assert TOKEN not in json.dumps(out)
    row = next(a for a in supervisor.status() if a.get("remote"))
    assert row["auth"] == "ok" and "token" not in row and TOKEN not in json.dumps(supervisor.status())
    # …nor in any log line (nor the code).
    assert TOKEN not in caplog.text and "ABCDE-12345" not in caplog.text
    assert "d-1" in caplog.text  # the device id IS logged — it's what the remote's operator revokes


def test_pair_retokens_an_existing_member_at_that_url(remote):
    rec = supervisor.add_remote("ava", URL, token="old-shared-bearer")
    supervisor._auth_cache[rec["id"]] = ("rejected", 0.0)  # e.g. the old device was revoked

    out = supervisor.pair_remote(URL, "ABCDE-12345")

    assert out["action"] == "retokened"
    assert out["agent"]["id"] == rec["id"] and out["agent"]["name"] == "ava"  # slug + windows kept
    assert supervisor.remote_for_slug(rec["id"])["token"] == TOKEN
    assert out["auth"] == "ok"
    assert len(supervisor.list_remotes()) == 1


def test_pair_retoken_with_a_name_renames(remote):
    rec = supervisor.add_remote("ava", URL, token="old")
    out = supervisor.pair_remote(URL, "c", name="ava-lab")
    assert out["agent"]["id"] == rec["id"] and out["agent"]["name"] == "ava-lab"


def test_pair_defaulted_name_is_suffixed_on_collision(remote):
    remote.card_name = "ava"
    supervisor.add_remote("ava", "http://100.64.0.9:7870")
    supervisor.add_remote("ava-2", "http://100.64.0.10:7870")
    out = supervisor.pair_remote(URL, "c")
    assert out["agent"]["name"] == "ava-3"


def test_pair_reserved_or_empty_card_name_falls_back(remote):
    remote.card_name = "host"
    assert supervisor.pair_remote(URL, "c")["agent"]["name"] == "host-2"
    remote.card_name = "!!!"
    assert supervisor.pair_remote("http://100.64.0.6:7870", "c")["agent"]["name"] == "remote"


def test_pair_explicit_name_collision_is_refused_before_the_code_is_spent(remote):
    supervisor.add_remote("ava", "http://100.64.0.9:7870")
    with pytest.raises(supervisor.PairingError, match="already exists") as ei:
        supervisor.pair_remote(URL, "c", name="ava")
    assert ei.value.status == 400
    with pytest.raises(supervisor.PairingError, match="reserved"):
        supervisor.pair_remote(URL, "c", name="host")
    with pytest.raises(supervisor.PairingError):
        supervisor.pair_remote(URL, "c", name="has spaces")
    assert remote.posts == []  # nothing claimed — the single-use code is still good


def test_pair_invalid_or_expired_code(remote):
    remote.claim = _Resp(403, {"ok": False, "error": "invalid or expired pairing code"})
    with pytest.raises(supervisor.PairingError, match="invalid or expired — generate a new one on the remote") as ei:
        supervisor.pair_remote(URL, "WRONG")
    assert ei.value.status == 400
    assert supervisor.list_remotes() == []


def test_pair_unreachable(remote):
    remote.claim = httpx.ConnectError("refused")
    with pytest.raises(supervisor.PairingError, match="unreachable") as ei:
        supervisor.pair_remote(URL, "c")
    assert ei.value.status == 502
    assert supervisor.list_remotes() == []


@pytest.mark.parametrize(
    "resp",
    [
        _Resp(404, {"detail": "Not Found"}),  # a protoAgent before ADR 0087, or not one at all
        _Resp(200, text="<html>hello</html>"),  # some other web server
        _Resp(200, ["not", "an", "object"]),
        _Resp(200, {"ok": True}),  # no token in the reply
    ],
)
def test_pair_not_a_protoagent(remote, resp):
    remote.claim = resp
    with pytest.raises(supervisor.PairingError, match="protoAgent") as ei:
        supervisor.pair_remote(URL, "c")
    assert ei.value.status == 502
    assert supervisor.list_remotes() == []


def test_pair_other_refusal_is_reported_with_its_status(remote):
    remote.claim = _Resp(500, {"ok": False, "error": "boom"})
    with pytest.raises(supervisor.PairingError, match=r"HTTP 500.*boom"):
        supervisor.pair_remote(URL, "c")


def test_pair_ssrf_blocked_url_is_never_contacted(remote):
    for bad in ("http://169.254.169.254/latest/meta-data", "ftp://100.64.0.5"):
        with pytest.raises(supervisor.FleetError):
            supervisor.pair_remote(bad, "c")
    assert remote.posts == [] and remote.gets == []


def test_pair_requires_a_code(remote):
    with pytest.raises(supervisor.PairingError, match="code is required"):
        supervisor.pair_remote(URL, "   ")
    assert remote.posts == []


# ── the authenticated probe (D5) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,verdict", [(200, "ok"), (401, "rejected"), (403, "rejected"), (404, "unknown"), (500, "unknown")]
)
def test_auth_probe_states(remote, status, verdict):
    remote.auth_status = status
    rec = supervisor.add_remote("ava", URL, token=TOKEN)
    full = supervisor.remote_for_slug(rec["id"])
    assert supervisor._auth_probe_one(full, 1.0) == verdict
    assert supervisor.remote_auth(full) == verdict
    # The bearer rides the header — and only the header.
    url, headers = remote.gets[-1]
    assert url == f"{URL}/api/devices" and headers == {"Authorization": f"Bearer {TOKEN}"}


def test_auth_probe_transport_error_is_unknown(remote, monkeypatch):
    rec = supervisor.add_remote("ava", URL, token=TOKEN)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
    assert supervisor._auth_probe_one(supervisor.remote_for_slug(rec["id"]), 1.0) == "unknown"


def test_no_token_is_none_and_never_probed(remote):
    supervisor.add_remote("ava", URL)
    supervisor.refresh_remote_probes()
    assert remote.auth_probes() == 0
    assert next(a for a in supervisor.status() if a.get("remote"))["auth"] == "none"


def test_wrong_token_reads_rejected_in_status(remote):
    supervisor.add_remote("ava", URL, token="not-the-right-one")
    supervisor.refresh_remote_probes()
    row = next(a for a in supervisor.status() if a.get("remote"))
    assert row["running"] is True and row["auth"] == "rejected"


def test_auth_probe_has_its_own_slower_ttl(remote, monkeypatch):
    assert supervisor._AUTH_TTL > supervisor._PROBE_TTL
    rec = supervisor.add_remote("ava", URL, token=TOKEN)
    supervisor.refresh_remote_probes()
    assert remote.auth_probes() == 1
    # Reachability goes stale (3s) — the card is re-probed, the auth verdict is NOT.
    supervisor._probe_cache[rec["id"]] = (True, 0.0)
    supervisor.refresh_remote_probes()
    assert remote.auth_probes() == 1
    # Once the auth TTL passes it re-checks — and a revoke on the remote shows up.
    remote.accept = set()
    verdict, _ = supervisor._auth_cache[rec["id"]]
    supervisor._auth_cache[rec["id"]] = (verdict, supervisor.time.monotonic() - supervisor._AUTH_TTL - 1)
    supervisor.refresh_remote_probes()
    assert remote.auth_probes() == 2
    assert next(a for a in supervisor.status() if a.get("remote"))["auth"] == "rejected"


def test_auth_probe_skipped_while_unreachable(remote, monkeypatch):
    rec = supervisor.add_remote("ava", URL, token=TOKEN)
    supervisor._auth_cache[rec["id"]] = ("rejected", 0.0)  # stale verdict
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **k: (
            (_ for _ in ()).throw(httpx.ConnectError("down"))
            if "agent-card" in url
            else pytest.fail("auth probe while down")
        ),
    )
    supervisor.refresh_remote_probes()
    assert next(a for a in supervisor.status() if a.get("remote"))["auth"] == "rejected"  # last verdict kept


def test_status_never_probes(remote):
    supervisor.add_remote("ava", URL, token=TOKEN)
    supervisor.status()
    assert remote.gets == []  # status() only READS the caches; the route refreshes off-loop


def test_update_remote_forgets_the_old_verdict_and_probe_remote_refreshes_it(remote):
    rec = supervisor.add_remote("ava", URL, token="wrong")
    supervisor.probe_remote(rec["id"])
    assert supervisor.remote_auth(supervisor.remote_for_slug(rec["id"])) == "rejected"
    supervisor.update_remote(rec["id"], token=TOKEN)
    assert rec["id"] not in supervisor._auth_cache
    supervisor.probe_remote(rec["id"])
    assert supervisor.remote_auth(supervisor.remote_for_slug(rec["id"])) == "ok"
    supervisor.update_remote(rec["id"], token="")
    assert supervisor.remote_auth(supervisor.remote_for_slug(rec["id"])) == "none"


# ── the route ────────────────────────────────────────────────────────────────


@pytest.fixture
def client(remote):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from operator_api.fleet_routes import register_fleet_routes

    app = FastAPI()
    register_fleet_routes(app)
    return TestClient(app)


def test_route_pairs_and_answers_the_sanitized_record(client, remote):
    r = client.post("/api/fleet/remotes/pair", json={"url": URL, "code": "ABCDE-12345", "name": "ava"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["action"] == "added" and body["auth"] == "ok" and body["reachable"] is True
    assert body["agent"]["name"] == "ava" and TOKEN not in r.text
    fleet = client.get("/api/fleet")
    row = next(a for a in fleet.json()["agents"] if a.get("remote"))
    assert row["auth"] == "ok" and TOKEN not in fleet.text


def test_route_maps_errors_to_400_and_502(client, remote):
    remote.claim = _Resp(403, {"ok": False, "error": "invalid or expired pairing code"})
    r = client.post("/api/fleet/remotes/pair", json={"url": URL, "code": "x"})
    assert r.status_code == 400 and "invalid or expired" in r.json()["detail"]
    remote.claim = httpx.ConnectError("refused")
    r = client.post("/api/fleet/remotes/pair", json={"url": URL, "code": "x"})
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]
    remote.claim = _Resp(404, {})
    assert client.post("/api/fleet/remotes/pair", json={"url": URL, "code": "x"}).status_code == 502
    r = client.post("/api/fleet/remotes/pair", json={"url": "http://169.254.169.254", "code": "x"})
    assert r.status_code == 400 and "egress" in r.json()["detail"]
    assert client.post("/api/fleet/remotes/pair", json={"url": URL}).status_code == 400  # no code


def test_register_and_edit_responses_carry_auth(client, remote):
    r = client.post("/api/fleet/remotes", json={"name": "ava", "url": URL, "token": "wrong"}).json()
    assert r["auth"] == "rejected"
    r = client.patch(f"/api/fleet/remotes/{r['agent']['id']}", json={"token": TOKEN}).json()
    assert r["auth"] == "ok"
    r = client.post("/api/fleet/remotes", json={"name": "bo", "url": "http://100.64.0.7:7870"}).json()
    assert r["auth"] == "none"
