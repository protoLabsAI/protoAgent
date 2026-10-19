"""Device token lifetime (ADR 0121 D5, #4336 S7.3): idle expiry, weekly rotation,
retire-on-first-use, reuse detection, and revocation reaching open streams.

Time is moved through ``security.devices._now`` (the registry's one clock seam).
"""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest

# Writes devices.json (chmod + os.replace) through security.devices.
pytestmark = pytest.mark.platform_sensitive

_SECRET = "op-secret"
DAY = 86400.0


@pytest.fixture
def devices(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "test-device-lifetime")

    import infra.paths

    infra.paths.reset_instance_paths()
    import security.devices as mod

    importlib.reload(mod)
    mod.cancel_pairings()
    yield mod
    infra.paths.reset_instance_paths()


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(devices, monkeypatch):
    c = Clock()
    monkeypatch.setattr(devices, "_now", c)
    return c


@pytest.fixture
def ttl(monkeypatch):
    """Set ``devices.idle_ttl_days`` the way the server does: on the live config."""
    from runtime.state import STATE

    def _set(days):
        monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(device_idle_ttl_days=days))

    _set(30)
    return _set


@pytest.fixture
def audit(tmp_path, monkeypatch):
    import observability.audit as audit_mod

    logger = audit_mod.AuditLogger(tmp_path / "audit.jsonl")
    monkeypatch.setattr(audit_mod, "audit_logger", logger)
    return logger


@pytest.fixture
def auth(devices):
    from a2a_impl import auth as mod

    mod.configure(bearer_token=_SECRET, api_key="", allowed_origins_raw="")
    yield mod


def _phone(devices, name="phone"):
    code, _ = devices.start_pairing()
    return devices.claim_pairing(code, name)


def _hub(devices):
    code, _ = devices.start_pairing(devices.KIND_AGENT)
    return devices.claim_pairing(code, "hub")


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _rows(devices):
    return json.loads(devices._registry_path().read_text("utf-8"))


# ── Idle expiry ─────────────────────────────────────────────────────────────────────────


def test_a_never_used_phone_expires_at_the_boundary_from_created_at(devices, clock, ttl):
    device, token = _phone(devices)
    clock.advance(30 * DAY - 1)
    assert devices.is_live(device["id"])
    clock.advance(1)  # exactly idle_ttl_days since the pairing
    assert devices.verify_token(token) is None
    assert not devices.is_live(device["id"])


def test_expiry_is_measured_from_last_seen(devices, clock, ttl):
    device, token = _phone(devices)
    clock.advance(20 * DAY)
    assert devices.verify_token(token) is not None  # records last_seen_at
    clock.advance(30 * DAY - 1)  # 50 days after pairing, 30 days less a second idle
    assert devices.verify_token(token) is not None
    clock.advance(30 * DAY)
    assert devices.verify_token(token) is None


def test_an_expired_device_stays_listed_as_expired_until_removed(devices, clock, ttl):
    device, token = _phone(devices)
    clock.advance(31 * DAY)
    assert devices.verify_token(token) is None
    (row,) = devices.list_devices()
    assert row["id"] == device["id"] and row["expired"] is True
    assert row["expires_at"] == pytest.approx(device["created_at"] + 30 * DAY)
    assert devices.revoke_device(device["id"])
    assert devices.list_devices() == []


def test_full_scope_and_legacy_rows_never_expire(devices, clock, ttl):
    hub, hub_token = _hub(devices)
    phone, phone_token = _phone(devices)
    devices.set_device_scope(phone["id"], "full")  # a promoted phone
    # A row written before ADR 0121: no scope, no rotation fields.
    rows = _rows(devices)
    rows.append({"id": "legacy1", "name": "old", "token_sha256": devices._hash("legacy-tok"), "created_at": clock.t})
    devices._registry_path().write_text(json.dumps(rows), "utf-8")
    clock.advance(400 * DAY)
    for token in (hub_token, phone_token, "legacy-tok"):
        assert devices.verify_token(token) is not None
    assert all(d["expired"] is False and d["expires_at"] is None for d in devices.list_devices())


def test_idle_ttl_zero_means_never(devices, clock, ttl):
    ttl(0)
    device, token = _phone(devices)
    clock.advance(10_000 * DAY)
    assert devices.verify_token(token) is not None
    assert devices.list_devices()[0]["expires_at"] is None


def test_the_ttl_is_read_live(devices, clock, ttl):
    device, token = _phone(devices)
    clock.advance(10 * DAY)
    ttl(7)
    assert devices.verify_token(token) is None
    ttl(30)  # lengthening it brings an expired-but-listed device back
    assert devices.verify_token(token) is not None


def test_legacy_rows_load_without_rotation_fields(devices, clock):
    devices._registry_path().parent.mkdir(parents=True, exist_ok=True)
    devices._registry_path().write_text(
        json.dumps([{"id": "a", "name": "n", "token_sha256": devices._hash("t"), "created_at": 1.0}]), "utf-8"
    )
    (d,) = devices._load()
    assert d.prev_token_sha256 is None and d.rotated_at is None and d.prev_retired_at is None


# ── Config ──────────────────────────────────────────────────────────────────────────────


def test_config_key_parses_defaults_and_accepts_zero(tmp_path):
    from graph.config import LangGraphConfig

    def load(body: str):
        p = tmp_path / "langgraph-config.yaml"
        p.write_text(body, "utf-8")
        return LangGraphConfig.from_yaml(str(p)).device_idle_ttl_days

    assert load("model:\n  name: x\n") == 30
    assert load("devices:\n  idle_ttl_days: 0\n") == 0
    assert load("devices:\n  idle_ttl_days: 7\n") == 7
    assert load("devices:\n  idle_ttl_days: -3\n") == 30  # junk falls back, never "never"
    assert load("devices:\n  idle_ttl_days: soon\n") == 30


def test_the_key_is_in_the_schema_with_minimum_zero_and_on_the_credential_plane():
    from graph.settings_schema import FIELDS, is_credential_plane_key

    (f,) = [f for f in FIELDS if f.key == "devices.idle_ttl_days"]
    assert f.attr == "device_idle_ttl_days" and f.minimum == 0
    assert is_credential_plane_key("devices.idle_ttl_days")


# ── rotate_due ──────────────────────────────────────────────────────────────────────────


def test_rotate_due_after_seven_days_from_pairing_then_from_rotation(devices, clock, ttl):
    device, token = _phone(devices)
    assert devices.self_view(device["id"])["rotate_due"] is False
    clock.advance(7 * DAY - 1)
    assert devices.self_view(device["id"])["rotate_due"] is False
    clock.advance(1)
    assert devices.self_view(device["id"])["rotate_due"] is True
    view, _new = devices.rotate_device(device["id"], token)
    assert view["rotate_due"] is False and view["rotated_at"] == clock.t
    clock.advance(7 * DAY)
    assert devices.self_view(device["id"])["rotate_due"] is True


def test_full_scope_devices_are_never_rotate_due(devices, clock, ttl):
    hub, _ = _hub(devices)
    clock.advance(100 * DAY)
    assert devices.self_view(hub["id"])["rotate_due"] is False


# ── Rotation: retire on first use, grace, reuse revokes ────────────────────────────────


def test_rotation_lifecycle_and_reuse_revokes(devices, clock, ttl, audit):
    device, old = _phone(devices)
    _view, new = devices.rotate_device(device["id"], old)
    assert new != old

    # Until the new token is presented, the old one keeps working — however long it takes.
    clock.advance(3 * DAY)
    assert devices.verify_token(old) is not None
    assert devices.verify_token(old) is not None

    # First use of the new token starts the old one's 120 s grace.
    assert devices.verify_token(new) is not None
    clock.advance(devices.ROTATION_GRACE_SECONDS - 1)
    assert devices.verify_token(old) is not None  # an in-flight request
    clock.advance(1)

    # After the grace, the old token is a theft signal: the device is revoked...
    assert devices.verify_token(old) is None
    assert devices.list_devices() == []
    # ...so the NEW token stops working too.
    assert devices.verify_token(new) is None
    entries = [json.loads(line) for line in audit.path.read_text("utf-8").splitlines()]
    (entry,) = [e for e in entries if e["tool"] == "device.revoked_token_reuse"]
    assert entry["args"]["device_id"] == device["id"] and entry["success"] is False


def test_rotation_stores_hashes_only(devices, clock, ttl):
    device, old = _phone(devices)
    _view, new = devices.rotate_device(device["id"], old)
    text = devices._registry_path().read_text("utf-8")
    assert old not in text and new not in text
    (row,) = _rows(devices)
    assert row["token_sha256"] == devices._hash(new) and row["prev_token_sha256"] == devices._hash(old)


def test_a_failed_storage_write_leaves_the_old_token_working(devices, clock, ttl):
    """The browser couldn't store the new token: it never presents it, so the old one stays
    valid, and the next rotation (from the old token) replaces the unused one."""
    device, old = _phone(devices)
    devices.rotate_device(device["id"], old)  # response lost
    clock.advance(7 * DAY)
    assert devices.verify_token(old) is not None
    assert devices.self_view(device["id"])["rotate_due"] is True
    _view, newer = devices.rotate_device(device["id"], old)
    assert devices.verify_token(newer) is not None
    assert devices.verify_token(old) is not None  # inside the grace that just started


def test_two_consecutive_rotates_from_the_switched_browser(devices, clock, ttl):
    """Rotate, switch to the new token, rotate again: the browser ends on the last token."""
    device, t0 = _phone(devices)
    _v, t1 = devices.rotate_device(device["id"], t0)
    assert devices.verify_token(t1) is not None  # the browser switched
    _v, t2 = devices.rotate_device(device["id"], t1)
    assert devices.verify_token(t2) is not None
    assert devices.verify_token(t1) is not None  # in its grace
    assert devices.verify_token(t0) is None  # dropped (a 401, not a revoke)
    assert devices.is_live(device["id"])


def test_two_racing_rotates_from_the_same_old_token_do_not_strand(devices, clock, ttl):
    """Two tabs both see rotate_due and both rotate with the stored token. The second is
    refused (409), so storage can only ever hold the one minted token — and the old one
    keeps working until that token is used."""
    device, t0 = _phone(devices)
    _v, t1 = devices.rotate_device(device["id"], t0)
    with pytest.raises(devices.RotationInProgress):
        devices.rotate_device(device["id"], t0)
    assert devices.verify_token(t0) is not None
    assert devices.verify_token(t1) is not None


def test_a_retired_token_cannot_rotate(devices, clock, ttl):
    device, t0 = _phone(devices)
    _v, t1 = devices.rotate_device(device["id"], t0)
    devices.verify_token(t1)
    assert devices.rotate_device(device["id"], t0) is None  # in its grace: valid, but can't rotate
    assert devices.verify_token(t1) is not None


def test_rotate_refuses_a_token_that_is_not_the_devices_own(devices, clock, ttl):
    a, ta = _phone(devices, "a")
    b, _tb = _phone(devices, "b")
    assert devices.rotate_device(b["id"], ta) is None
    assert devices.rotate_device("nope", ta) is None


def test_full_access_tokens_do_not_rotate(devices, clock, ttl):
    hub, hub_token = _hub(devices)
    assert devices.rotate_device(hub["id"], hub_token) is None
    phone, phone_token = _phone(devices)
    devices.set_device_scope(phone["id"], "full")
    assert devices.rotate_device(phone["id"], phone_token) is None
    assert devices.verify_token(hub_token) is not None and devices.verify_token(phone_token) is not None


def test_an_expired_device_cannot_rotate(devices, clock, ttl):
    device, t0 = _phone(devices)
    clock.advance(31 * DAY)
    assert devices.rotate_device(device["id"], t0) is None


# ── Routes: /api/devices/self and /self/rotate through the real middleware ──────────────


@pytest.fixture
def client(auth, devices, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import operator_api.pairing_routes as pr

    monkeypatch.delenv("PROTOAGENT_UI", raising=False)
    app = FastAPI()
    pr.register_pairing_routes(app, agent_name=lambda: "box")
    auth.install(app, bearer_token=_SECRET, api_key="", allowed_origins_raw="")
    return TestClient(app)


def test_self_for_a_device(client, devices, clock, ttl):
    device, token = _phone(devices)
    r = client.get("/api/devices/self", headers=_hdr(token))
    assert r.status_code == 200
    me = r.json()["device"]
    assert me["id"] == device["id"] and me["name"] == "phone" and me["scope"] == "device"
    assert me["rotate_due"] is False
    assert me["expires_at"] == pytest.approx(device["created_at"] + 30 * DAY)


def test_self_with_the_shared_bearer_is_device_null(client):
    r = client.get("/api/devices/self", headers=_hdr(_SECRET))
    assert r.status_code == 200 and r.json() == {"device": None}


def test_rotate_with_the_shared_bearer_is_404_with_no_token(client):
    r = client.post("/api/devices/self/rotate", headers=_hdr(_SECRET))
    assert r.status_code == 404 and "token" not in r.json()


def test_a_device_scope_phone_may_rotate_itself(client, devices, clock, ttl):
    device, old = _phone(devices)
    r = client.post("/api/devices/self/rotate", headers=_hdr(old))
    assert r.status_code == 200, r.text
    body = r.json()
    new = body["token"]
    assert body["device"]["id"] == device["id"] and r.headers["cache-control"] == "no-store"
    # The next request uses the new token; the old one rides the grace, then revokes.
    assert client.get("/api/devices/self", headers=_hdr(new)).json()["device"]["id"] == device["id"]
    clock.advance(devices.ROTATION_GRACE_SECONDS)
    assert client.get("/api/devices", headers=_hdr(old)).status_code == 401
    assert client.get("/api/devices", headers=_hdr(new)).status_code == 401


def test_a_racing_second_rotate_is_409(client, devices, clock, ttl):
    _device, old = _phone(devices)
    assert client.post("/api/devices/self/rotate", headers=_hdr(old)).status_code == 200
    r = client.post("/api/devices/self/rotate", headers=_hdr(old))
    assert r.status_code == 409 and r.json()["error"] == "rotation_in_progress"


def test_self_routes_are_not_on_the_ceiling():
    from a2a_impl.credential_scope import ceiling_rule

    assert ceiling_rule("GET", "/api/devices/self") is None
    assert ceiling_rule("POST", "/api/devices/self/rotate") is None


def test_sse_token_carries_the_device_and_dies_with_it(auth, devices, clock, ttl):
    device, _token = _phone(devices)
    tok = auth.generate_sse_token(scope="device", device_id=device["id"])
    assert auth._sse_token_claims(tok) == ("device", device["id"])
    devices.revoke_device(device["id"])
    assert auth._sse_token_claims(tok) is None


# ── Streams close within one keepalive of a revoke ──────────────────────────────────────


def _stream_app(auth, monkeypatch, *, keepalive: float):
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    import a2a_impl.stream_guard as guard
    from operator_api.routes import _sse_event_stream
    from server.turn_control import attendance_stream  # imported up front: it's slow to load

    monkeypatch.setattr(guard, "STREAM_RECHECK_SECONDS", keepalive * 0.6)
    app = FastAPI()

    async def _never_publishes(*_a):
        await asyncio.Event().wait()
        yield {}  # pragma: no cover

    @app.get("/api/events")
    async def _events():
        return StreamingResponse(
            _sse_event_stream(_never_publishes, keepalive_s=keepalive), media_type="text/event-stream"
        )

    @app.get("/api/chat/attend")
    async def _attend():
        return StreamingResponse(attendance_stream("s1", keepalive_s=keepalive), media_type="text/event-stream")

    @app.post("/a2a")
    async def _a2a():
        from sse_starlette.sse import EventSourceResponse

        async def _quiet():
            await asyncio.Event().wait()
            yield {"data": "x"}  # pragma: no cover

        return EventSourceResponse(_quiet(), ping=keepalive)

    auth.install(app, bearer_token=_SECRET, api_key="", allowed_origins_raw="")
    return app


async def _open_then_revoke(app, devices, method, url, headers, *, keepalive, device_id):
    import httpx

    async def revoke_later():
        await asyncio.sleep(keepalive * 3)
        devices.revoke_device(device_id)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        task = asyncio.create_task(revoke_later())
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        # ASGITransport buffers the whole body: the request only returns once the guard has
        # ended the stream. The timeout turns "never closes" into a clean failure.
        resp = await asyncio.wait_for(c.request(method, url, headers=headers), timeout=10)
        elapsed = loop.time() - t0
        await task
    return resp, elapsed


@pytest.mark.parametrize("path", ["/api/events", "/api/chat/attend"])
async def test_a_query_token_stream_closes_within_one_keepalive_of_a_revoke(auth, devices, monkeypatch, path):
    keepalive = 0.1
    app = _stream_app(auth, monkeypatch, keepalive=keepalive)
    device, _token = _phone(devices)
    sse = auth.generate_sse_token(scope="device", device_id=device["id"])
    resp, elapsed = await _open_then_revoke(
        app, devices, "GET", f"{path}?token={sse}", {}, keepalive=keepalive, device_id=device["id"]
    )
    assert resp.status_code == 200
    assert b"keepalive" in resp.content
    # Revoked at 3 keepalives; closed by the next tick (plus scheduling slack).
    assert elapsed < keepalive * 3 + keepalive * 2.5


async def test_a_bearer_a2a_stream_closes_on_revoke(auth, devices, monkeypatch):
    keepalive = 0.1
    app = _stream_app(auth, monkeypatch, keepalive=keepalive)
    device, token = _phone(devices)
    resp, elapsed = await _open_then_revoke(
        app, devices, "POST", "/a2a", _hdr(token), keepalive=keepalive, device_id=device["id"]
    )
    assert resp.status_code == 200 and b"ping" in resp.content
    assert elapsed < keepalive * 3 + keepalive * 2.5


async def test_the_shared_bearers_stream_is_never_closed_by_the_guard(auth, devices, monkeypatch):
    import httpx

    keepalive = 0.05
    app = _stream_app(auth, monkeypatch, keepalive=keepalive)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(c.get("/api/events", headers=_hdr(_SECRET)), timeout=keepalive * 8)


async def test_a_revoked_devices_sse_token_cannot_open_a_stream(auth, devices, monkeypatch):
    import httpx

    app = _stream_app(auth, monkeypatch, keepalive=0.1)
    device, _token = _phone(devices)
    sse = auth.generate_sse_token(scope="device", device_id=device["id"])
    devices.revoke_device(device["id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await asyncio.wait_for(c.get(f"/api/events?token={sse}"), timeout=5)
    assert r.status_code == 401
