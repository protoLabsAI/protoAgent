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
    """A remote protoAgent's relevant routes: the claim, the card, the auth probe
    (``/api/devices``) and a device revoke. ``open=True`` models an instance with no auth
    configured (any request, token or not, is let through)."""

    def __init__(self, *, claim=None, card_name="Ava Agent", version="0.183.1", accept=(TOKEN,), auth_status=None):
        self.claim = (
            claim
            if claim is not None
            else _Resp(200, {"ok": True, "device": {"id": "d-1", "name": "x"}, "token": TOKEN})
        )
        self.card_name = card_name
        self.version = version
        self.accept = set(accept)
        self.auth_status = auth_status  # force a status on a TOKENED /api/devices (None = check the bearer)
        self.open = False
        self.delete_status = 200
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []
        self.deletes: list[tuple[str, dict]] = []
        self.kwargs: list[tuple[str, dict]] = []  # (url, every kwarg) — the follow_redirects pin

    def post(self, url, json=None, timeout=None, **kw):
        self.kwargs.append((url, dict(kw)))
        self.posts.append((url, dict(json or {})))
        if isinstance(self.claim, Exception):
            raise self.claim
        return self.claim

    def get(self, url, timeout=None, headers=None, **kw):
        self.kwargs.append((url, dict(kw)))
        self.gets.append((url, dict(headers or {})))
        if url.endswith("/.well-known/agent-card.json"):
            return _Resp(200, {"name": self.card_name, "version": self.version})
        if url.endswith(supervisor._AUTH_PROBE_PATH):
            bearer = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
            if self.open:
                return _Resp(200, {"devices": []})
            if not bearer:
                return _Resp(401, {"detail": "Unauthorized"})
            if self.auth_status is not None:
                return _Resp(self.auth_status, {})
            return _Resp(200, {"devices": []}) if bearer in self.accept else _Resp(401, {"detail": "Unauthorized"})
        return _Resp(404, {})

    def delete(self, url, timeout=None, headers=None, **kw):
        self.kwargs.append((url, dict(kw)))
        self.deletes.append((url, dict(headers or {})))
        if isinstance(self.delete_status, Exception):
            raise self.delete_status
        return _Resp(self.delete_status, {"ok": self.delete_status == 200})

    def auth_probes(self) -> int:
        """TOKENED auth probes (the unauthenticated 'is it open?' pre-check isn't counted)."""
        return sum(1 for u, h in self.gets if u.endswith(supervisor._AUTH_PROBE_PATH) and h)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    supervisor._probe_cache.clear()
    supervisor._auth_cache.clear()
    fake = FakeRemote()
    monkeypatch.setattr(httpx, "post", fake.post)
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(httpx, "delete", fake.delete)
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


# ── review fixes (ADR 0113 S2 review) ────────────────────────────────────────


# M1 — a token never follows a url to a new origin.


def test_url_move_to_a_new_origin_clears_the_token_and_never_sends_it_there(remote):
    rec = supervisor.add_remote("ava", URL, token=TOKEN)
    out = supervisor.update_remote(rec["id"], url="http://100.64.0.99:7870")
    assert out["token_cleared"] is True
    stored = supervisor.remote_for_slug(rec["id"])
    assert stored["token"] == "" and stored["url"] == "http://100.64.0.99:7870"
    # The immediate re-probe (and every later one) goes to the new host WITHOUT the old token.
    supervisor.probe_remote(rec["id"])
    supervisor.refresh_remote_probes()
    assert not [u for u, h in remote.gets if TOKEN in json.dumps(h)]
    assert supervisor.remote_auth(stored) == "none"


@pytest.mark.parametrize("new", ["http://100.64.0.5:9999", "https://100.64.0.5:7870", "http://100.64.0.6:7870"])
def test_any_origin_component_counts_as_a_move(remote, new):
    rec = supervisor.add_remote("ava", URL, token=TOKEN)
    assert supervisor.update_remote(rec["id"], url=new).get("token_cleared") is True


@pytest.mark.parametrize("same", [URL + "/", "HTTP://100.64.0.5:7870", "  http://100.64.0.5:7870/  "])
def test_same_origin_spelled_differently_keeps_the_token(remote, same):
    rec = supervisor.add_remote("ava", URL, token=TOKEN)
    out = supervisor.update_remote(rec["id"], url=same)
    assert "token_cleared" not in out and supervisor.remote_for_slug(rec["id"])["token"] == TOKEN


def test_default_port_is_the_same_origin_even_for_a_legacy_record(remote, tmp_path):
    # A record written before canonicalization, spelled with the default port + caps.
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    (root / "remotes.json").write_text(
        json.dumps({"ava-1": {"id": "ava-1", "name": "ava", "url": "http://Ava.Tail:80/", "token": TOKEN}})
    )
    out = supervisor.update_remote("ava-1", url="http://ava.tail")
    assert "token_cleared" not in out and supervisor.remote_for_slug("ava-1")["token"] == TOKEN


def test_url_move_with_a_token_in_the_same_call_keeps_that_token(remote):
    rec = supervisor.add_remote("ava", URL, token="old")
    out = supervisor.update_remote(rec["id"], url="http://100.64.0.99:7870", token=TOKEN)
    assert "token_cleared" not in out and supervisor.remote_for_slug(rec["id"])["token"] == TOKEN


def test_url_move_without_a_stored_token_reports_nothing_cleared(remote):
    rec = supervisor.add_remote("ava", URL)
    assert "token_cleared" not in supervisor.update_remote(rec["id"], url="http://100.64.0.99:7870")


# M2 — remote-supplied text is bounded, printable, and a device id is strictly shaped.


def test_remote_error_text_is_stripped_of_control_chars_and_capped(remote):
    evil = "\x1b[2J\x1b[31mpwned\nFAKE LOG LINE " + "A" * 200_000
    remote.claim = _Resp(500, {"ok": False, "error": evil})
    with pytest.raises(supervisor.PairingError) as ei:
        supervisor.pair_remote(URL, "c")
    msg = str(ei.value)
    assert len(msg) < 400 and "\x1b" not in msg and "\n" not in msg and "pwned" in msg


def test_non_string_remote_error_is_not_echoed(remote):
    remote.claim = _Resp(500, {"ok": False, "error": {"nested": "x" * 1000}})
    with pytest.raises(supervisor.PairingError, match="no reason given"):
        supervisor.pair_remote(URL, "c")


@pytest.mark.parametrize("bad_id", ["d-1\n2026-09-26 INFO forged", "x" * 65, "../../etc", 12345, None, ""])
def test_malformed_device_id_is_dropped_not_logged_or_stored(remote, caplog, bad_id):
    caplog.set_level(logging.DEBUG)
    remote.claim = _Resp(200, {"ok": True, "device": {"id": bad_id}, "token": TOKEN})
    out = supervisor.pair_remote(URL, "c")
    assert "device_id" not in supervisor.remote_for_slug(out["agent"]["id"])
    assert "forged" not in caplog.text and "etc" not in caplog.text
    assert "(id not reported)" in caplog.text


def test_non_string_token_is_not_a_token(remote):
    remote.claim = _Resp(200, {"ok": True, "device": {"id": "d-1"}, "token": {"t": 1}})
    with pytest.raises(supervisor.PairingError, match="no token"):
        supervisor.pair_remote(URL, "c")


def test_card_name_is_capped(remote):
    remote.card_name = "n" * 5000
    assert len(supervisor.pair_remote(URL, "c")["agent"]["name"]) <= 48


# Minor 1 — canonical base URLs.


@pytest.mark.parametrize(
    "raw,canon",
    [
        ("HTTP://Ava.Tail:80/", "http://ava.tail"),
        ("https://ava.tail:443", "https://ava.tail"),
        ("https://ava.tail:8443/", "https://ava.tail:8443"),
        ("http://[FD7A::1]:7870", "http://[fd7a::1]:7870"),
    ],
)
def test_remote_urls_are_canonicalized(remote, raw, canon):
    assert supervisor._normalize_remote_url(raw) == canon


@pytest.mark.parametrize(
    "bad,why",
    [
        ("http://ava.tail:7870/app/#pair=abc", "phone pairing link"),
        ("http://ava.tail:7870/a2a", "base URL"),
        ("http://ava.tail:7870?x=1", "base URL"),
        ("http://ava.tail:7870/#frag", "base URL"),
        ("http://user:pw@ava.tail:7870", "credentials"),
        ("http://:7870", "no host"),
        ("http://ava.tail:notaport", "not a valid URL"),
    ],
)
def test_non_base_urls_are_refused_with_a_reason(remote, bad, why):
    with pytest.raises(supervisor.FleetError, match=why):
        supervisor.pair_remote(bad, "c")
    assert remote.posts == []


def test_add_refuses_a_duplicate_spelled_differently(remote):
    supervisor.add_remote("ava", "http://ava.tail:80")
    with pytest.raises(supervisor.FleetError, match="already in the fleet"):
        supervisor.add_remote("bo", "HTTP://AVA.TAIL/")


def test_pair_matches_a_legacy_record_spelled_differently(remote, tmp_path):
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    (root / "remotes.json").write_text(
        json.dumps({"ava-1": {"id": "ava-1", "name": "ava", "url": "http://100.64.0.5:7870/", "token": "old"}})
    )
    out = supervisor.pair_remote("HTTP://100.64.0.5:7870", "c")
    assert out["action"] == "retokened" and out["agent"]["id"] == "ava-1"
    assert len(supervisor.list_remotes()) == 1


# Minor 2 — the re-token path names the orphaned device too.


def test_retoken_failure_after_the_claim_names_the_device_to_revoke(remote, monkeypatch):
    supervisor.add_remote("ava", URL, token="old")

    def lost_race(*a, **k):
        raise supervisor.FleetError("no remote member 'ava-x'")

    monkeypatch.setattr(supervisor, "update_remote", lost_race)
    with pytest.raises(supervisor.PairingError, match=r"revoke device d-1 on the remote"):
        supervisor.pair_remote(URL, "c")


# Minor 3 — re-pairing retires the previous device instead of piling them up.


def test_pair_stores_the_device_id(remote):
    out = supervisor.pair_remote(URL, "c")
    assert supervisor.remote_for_slug(out["agent"]["id"])["device_id"] == "d-1"


def test_repair_revokes_the_previous_device_with_the_new_token(remote):
    first = supervisor.pair_remote(URL, "c1")
    remote.claim = _Resp(200, {"ok": True, "device": {"id": "d-2"}, "token": "NEW-TOKEN"})
    remote.accept = {TOKEN, "NEW-TOKEN"}  # the old token still authenticates
    out = supervisor.pair_remote(URL, "c2")
    assert out["action"] == "retokened" and out["agent"]["id"] == first["agent"]["id"]
    assert remote.deletes == [(f"{URL}/api/devices/d-1", {"Authorization": "Bearer NEW-TOKEN"})]
    assert supervisor.remote_for_slug(out["agent"]["id"])["device_id"] == "d-2"


def test_repair_leaves_the_old_device_alone_when_its_token_no_longer_works(remote):
    supervisor.pair_remote(URL, "c1")
    remote.claim = _Resp(200, {"ok": True, "device": {"id": "d-2"}, "token": "NEW-TOKEN"})
    remote.accept = {"NEW-TOKEN"}  # d-1 was already revoked on the remote
    supervisor.pair_remote(URL, "c2")
    assert remote.deletes == []


def test_repair_never_revokes_for_a_pasted_token(remote):
    supervisor.add_remote("ava", URL, token=TOKEN)  # no device_id: a shared bearer, not a device
    remote.claim = _Resp(200, {"ok": True, "device": {"id": "d-2"}, "token": "NEW-TOKEN"})
    supervisor.pair_remote(URL, "c")
    assert remote.deletes == []


@pytest.mark.parametrize("failure", [500, httpx.ConnectError("gone")])
def test_repair_revoke_failure_is_logged_without_secrets_and_swallowed(remote, caplog, failure):
    caplog.set_level(logging.DEBUG)
    supervisor.pair_remote(URL, "c1")
    remote.claim = _Resp(200, {"ok": True, "device": {"id": "d-2"}, "token": "NEW-TOKEN"})
    remote.accept = {TOKEN, "NEW-TOKEN"}
    remote.delete_status = failure
    assert supervisor.pair_remote(URL, "c2")["action"] == "retokened"
    assert "could not revoke the previous device d-1" in caplog.text
    assert TOKEN not in caplog.text and "NEW-TOKEN" not in caplog.text


def test_a_manual_token_change_forgets_the_device_id(remote):
    out = supervisor.pair_remote(URL, "c")
    supervisor.update_remote(out["agent"]["id"], token="pasted")
    assert "device_id" not in supervisor.remote_for_slug(out["agent"]["id"])


# Minor 4 — only a protoAgent-shaped 403 means "bad code"; an open remote is "open".


@pytest.mark.parametrize(
    "resp",
    [_Resp(403, text="<html>Forbidden by WAF</html>"), _Resp(403, {"detail": "Forbidden"}), _Resp(403, ["x"])],
)
def test_a_foreign_403_is_not_blamed_on_the_code(remote, resp):
    remote.claim = resp
    with pytest.raises(supervisor.PairingError, match="not a protoAgent") as ei:
        supervisor.pair_remote(URL, "c")
    assert ei.value.status == 502


def test_an_open_remote_reports_open_not_ok(remote):
    remote.open = True
    out = supervisor.pair_remote(URL, "c")
    assert out["auth"] == "open"
    assert remote.auth_probes() == 0  # an open remote can't verify a token, so none is sent


# Minor 5 — no request the hub makes follows a redirect (a 3xx would carry the bearer on).


def test_no_request_follows_redirects(remote):
    supervisor.pair_remote(URL, "c1")
    remote.claim = _Resp(200, {"ok": True, "device": {"id": "d-2"}, "token": "NEW-TOKEN"})
    remote.accept = {TOKEN, "NEW-TOKEN"}
    supervisor.pair_remote(URL, "c2")  # exercises the revoke DELETE too
    supervisor._auth_cache.clear()
    supervisor._probe_cache.clear()
    supervisor.refresh_remote_probes()
    kinds = {u.rsplit("/", 1)[-1] for u, _ in remote.kwargs}
    assert {"claim", "agent-card.json", "devices", "d-1"} <= kinds
    for url, kw in remote.kwargs:
        assert "follow_redirects" in kw and not kw["follow_redirects"], url


def test_route_patch_reports_token_cleared(client, remote):
    rid = client.post("/api/fleet/remotes", json={"name": "ava", "url": URL, "token": TOKEN}).json()["agent"]["id"]
    body = client.patch(f"/api/fleet/remotes/{rid}", json={"url": "http://100.64.0.99:7870"}).json()
    assert body["token_cleared"] is True and body["auth"] == "none"
    body = client.patch(f"/api/fleet/remotes/{rid}", json={"name": "ava2"}).json()
    assert "token_cleared" not in body


def test_route_rejects_a_phone_pairing_link(client, remote):
    r = client.post("/api/fleet/remotes/pair", json={"url": URL + "/app/#pair=abc", "code": "x"})
    assert r.status_code == 400 and "phone pairing link" in r.json()["detail"]


# D10 — a credential crosses plain http only to loopback or a tailnet, unless opted in.

LAN = "http://192.168.1.20:7870"


@pytest.fixture
def fake_dns(monkeypatch):
    """Names resolve from this table (a name not in it doesn't resolve)."""
    import socket

    table = {
        "localhost": ["127.0.0.1"],
        "ava.lan": ["192.168.1.20"],
        "ava.tail-alias": ["100.101.1.2"],
        "split.example": ["100.101.1.2", "203.0.113.9"],  # one tailnet, one public: not safe
    }

    def getaddrinfo(host, *a, **k):
        if host not in table:
            raise socket.gaierror("nope")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, 1, 6, "", (ip, 0)) for ip in table[host]]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize(
    "url",
    [
        "http://100.64.0.5:7870",  # tailnet v4 (100.64.0.0/10)
        "http://100.127.255.1:7870",
        "http://[fd7a:115c:a1e0::1]:7870",  # tailnet v6
        "http://ava.tailnet-name.ts.net:7870",  # MagicDNS
        "http://ava.tail-alias:7870",  # a name that resolves to a tailnet address
        "http://127.0.0.1:7870",
        "http://localhost:7870",
        "https://192.168.1.20:7870",  # TLS
        "https://ava.example.com",
    ],
)
def test_d10_safe_transports_pair_without_asking(remote, fake_dns, url):
    assert supervisor.pair_remote(url, "c")["action"] == "added"
    assert len(remote.posts) == 1


def test_d10_ipv6_loopback_counts_as_loopback():
    # (The egress guard refuses [::1] as a remote url before D10 is consulted — so the
    # transport rule is pinned directly.)
    assert supervisor._cleartext_host("http://[::1]:7870") is None


@pytest.mark.parametrize(
    "url,shown",
    [
        (LAN, "192.168.1.20"),
        ("http://ava.lan:7870", "ava.lan"),  # judged by what it resolves to
        ("http://split.example:7870", "split.example"),  # every address must be safe
        ("http://100.128.0.1:7870", "100.128.0.1"),  # just outside 100.64.0.0/10
        ("http://unresolvable.example:7870", "unresolvable.example"),  # can't judge → not safe
        ("http://8.8.8.8:7870", "8.8.8.8"),
    ],
)
def test_d10_plain_http_elsewhere_is_refused_without_contacting_it(remote, fake_dns, url, shown):
    with pytest.raises(supervisor.PairingError) as ei:
        supervisor.pair_remote(url, "c")
    assert ei.value.status == 400
    msg = str(ei.value)
    assert f"plain http to {shown} would send the pairing code and token in cleartext" in msg
    assert "tailnet" in msg and "allow_insecure/--insecure-http" in msg
    assert remote.posts == [] and remote.gets == [] and supervisor.list_remotes() == []


def test_d10_opt_in_pairs_over_lan_http(remote, fake_dns):
    out = supervisor.pair_remote(LAN, "c", allow_insecure=True)
    assert out["action"] == "added" and len(remote.posts) == 1


def test_d10_repair_over_lan_http_needs_the_opt_in_too(remote, fake_dns):
    supervisor.add_remote("ava", LAN)  # no token: registering an address sends nothing
    with pytest.raises(supervisor.PairingError, match="cleartext"):
        supervisor.pair_remote(LAN, "c")
    assert remote.posts == []
    assert supervisor.pair_remote(LAN, "c", allow_insecure=True)["action"] == "retokened"


def test_d10_storing_a_token_for_lan_http_needs_the_opt_in(remote, fake_dns):
    with pytest.raises(supervisor.InsecureTransport, match="stored token"):
        supervisor.add_remote("ava", LAN, token=TOKEN)
    assert supervisor.list_remotes() == []
    rec = supervisor.add_remote("ava", LAN)  # a tokenless add is fine
    with pytest.raises(supervisor.InsecureTransport):
        supervisor.update_remote(rec["id"], token=TOKEN)
    assert supervisor.remote_for_slug(rec["id"])["token"] == ""
    supervisor.update_remote(rec["id"], token=TOKEN, allow_insecure=True)
    assert supervisor.remote_for_slug(rec["id"])["token"] == TOKEN
    # Clearing a token is never gated; nor is moving a tokened remote TO a safe address.
    supervisor.update_remote(rec["id"], token="")
    rec2 = supervisor.add_remote("bo", "http://100.64.0.7:7870", token=TOKEN)
    with pytest.raises(supervisor.InsecureTransport):
        # moving to LAN with a new token in the same call is judged against the NEW url
        supervisor.update_remote(rec2["id"], url="http://192.168.1.21:7870", token="t2")
    assert supervisor.add_remote("cy", "http://192.168.1.22:7870", token=TOKEN, allow_insecure=True)["name"] == "cy"


def test_d10_routes_take_allow_insecure(client, remote, fake_dns):
    r = client.post("/api/fleet/remotes/pair", json={"url": LAN, "code": "c"})
    assert r.status_code == 400 and "cleartext" in r.json()["detail"] and remote.posts == []
    r = client.post("/api/fleet/remotes/pair", json={"url": LAN, "code": "c", "allow_insecure": True})
    assert r.status_code == 200, r.text
    rid = r.json()["agent"]["id"]
    assert client.patch(f"/api/fleet/remotes/{rid}", json={"token": "x"}).status_code == 400
    assert client.patch(f"/api/fleet/remotes/{rid}", json={"token": "x", "allow_insecure": True}).status_code == 200
    r = client.post("/api/fleet/remotes", json={"name": "bo", "url": "http://192.168.1.30:7870", "token": "x"})
    assert r.status_code == 400 and "cleartext" in r.json()["detail"]
    r = client.post(
        "/api/fleet/remotes",
        json={"name": "bo", "url": "http://192.168.1.30:7870", "token": "x", "allow_insecure": True},
    )
    assert r.status_code == 200
    # Only a JSON true opts in — a truthy string doesn't.
    r = client.post(
        "/api/fleet/remotes",
        json={"name": "cy", "url": "http://192.168.1.31:7870", "token": "x", "allow_insecure": "yes"},
    )
    assert r.status_code == 400


async def test_m1_after_a_url_move_nothing_reaches_the_new_host_with_a_credential(remote, monkeypatch):
    """End to end through the REAL proxy + telemetry resolution (ADR 0113 S3's tokenless-
    remote handling): once a url move has cleared the token, the member is a tokenless
    remote, and neither the proxied console call (an operator caller carrying its own hub
    bearer), nor the telemetry rollup read, nor the auth probe presents ANY credential to
    the new host — not the old device token, not the fleet service token, not the caller's."""
    from types import SimpleNamespace

    from graph.fleet import proxy, service_token
    from operator_api import telemetry_routes as tr

    rec = supervisor.add_remote("ava", URL, token=TOKEN)
    rid = rec["id"]
    assert supervisor.update_remote(rid, url="http://100.64.0.99:7870")["token_cleared"] is True
    monkeypatch.setattr(service_token, "resolve_service_token", lambda: "FLEET-SECRET")
    proxy._slug_cache.clear()
    proxy._remote_slugs.clear()

    sent: list[tuple[str, dict]] = []

    class _Up:
        status_code = 200
        headers: dict = {}

        async def aiter_raw(self):
            yield b"{}"

        async def aclose(self):
            pass

        def json(self):
            return {"turns": 0}

    class _Client:
        def build_request(self, method, url, headers=None, **kw):
            sent.append((url, dict(headers or {})))
            return object()

        async def send(self, req, stream=True):
            return _Up()

        async def get(self, url, headers=None, timeout=None):
            sent.append((url, dict(headers or {})))
            return _Up()

    class _Req:
        method = "GET"
        headers = {"Authorization": "Bearer HUB-CALLER-SECRET"}
        query_params: dict = {}
        state = SimpleNamespace(trust_tier="operator")

        async def body(self):
            return b""

    monkeypatch.setattr(proxy, "_get_client", lambda: _Client())
    try:
        await proxy.forward_to(rid, _Req(), "api/devices")
        await tr._fetch_member_json(rid, "api/telemetry/summary")
    finally:
        proxy._slug_cache.clear()
        proxy._remote_slugs.clear()
    supervisor.refresh_remote_probes()

    assert [u for u, _ in sent] == [
        "http://100.64.0.99:7870/api/devices",
        "http://100.64.0.99:7870/api/telemetry/summary",
    ]
    everything = json.dumps([h for _, h in sent] + [h for _, h in remote.gets])
    for secret in (TOKEN, "FLEET-SECRET", "HUB-CALLER-SECRET"):
        assert secret not in everything
    assert not any(k.lower() == "authorization" for _, h in sent for k in h)
