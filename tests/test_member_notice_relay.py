"""A fleet member's notice reaches the operator's devices through its hub (#5614)."""
import asyncio
import json
import sqlite3
import time

import httpx
import pytest
from fastapi import FastAPI

from graph import push_transport
from graph.fleet import notice_relay, service_token
from graph.plugins import chat_surface
from graph.plugins.chat_surface import ChatNotice, notify_chat_surfaces
from graph.push_transport import PushResult
from infra.paths import reset_instance_paths
from operator_api.notice_routes import register_notice_routes
from security import devices
from server import maintenance_loops as loops

pytestmark = pytest.mark.platform_sensitive

MEMBER = "protoengineer-ba4c"
TOKEN = "fleet-token-for-tests"


class Provider:
    def __init__(self):
        self.calls = []

    async def send(self, binding, payload):
        self.calls.append(payload)
        return PushResult("accepted")


@pytest.fixture
def box(tmp_path, monkeypatch):
    """One instance root with a paired, push-enrolled phone and one known member."""
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    monkeypatch.setenv(service_token.ENV_VAR, TOKEN)
    monkeypatch.setattr(service_token, "_cached", [None])
    reset_instance_paths()
    monkeypatch.setattr("graph.plugins.host.HOST.publish", None)
    monkeypatch.setattr(
        "graph.workspaces.manager._find", lambda ident: {"id": MEMBER, "name": "protoEngineer"} if ident == MEMBER else None
    )
    device, token = devices._register("phone")
    devices.set_push_binding(
        device.id, token, endpoint="https://web.push.apple.com/phone",
        p256dh="client-key", auth="secret", vapid_version=1,
    )
    yield
    reset_instance_paths()


def notice_body(**over):
    now = time.time()
    return {"member": MEMBER, "notice_id": "decision-dec-1", "kind": "question", "importance": "high",
            "created_at": now, "expires_at": now + 86400, **over}


def rows(transport):
    with sqlite3.connect(transport.path) as db:
        return db.execute("SELECT notice_id, kind, importance, state FROM attempts").fetchall()


async def settle(check):
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("did not settle")


# ── hub side ─────────────────────────────────────────────────────────────────────────


async def test_hub_pushes_a_members_notice_and_remembers_who_raised_it(box):
    provider = Provider()
    transport = loops._start_push_delivery(provider)
    try:
        assert await notice_relay.accept_member_notice(notice_body()) is True
        await settle(lambda: rows(transport) == [("decision-dec-1", "question", "high", "accepted")])
        assert [call["notice_id"] for call in provider.calls] == ["decision-dec-1"]
        assert transport.origin_of("decision-dec-1") == MEMBER
        assert transport.origin_of("decision-of-the-hub") is None
        # The payload the phone gets still names no member (ADR 0135, "What leaves the machine").
        assert MEMBER not in push_transport.build_push_payload(provider.calls[0]).decode()
    finally:
        await loops._stop_push_delivery()
    assert push_transport.active_transport() is None


@pytest.mark.parametrize("over", [
    {"member": "someone-else"}, {"member": ""}, {"member": 7},
    {"notice_id": "../etc"}, {"notice_id": ""}, {"notice_id": ["x"]}, {"kind": "shout"}, {"kind": ["question"]},
    {"kind": {"a": 1}}, {"importance": "urgent"}, {"importance": ["high"]},
    {"expires_at": 5.0}, {"expires_at": "soon"}, {"created_at": float("nan")},
])
async def test_hub_refuses_what_is_not_its_members_well_formed_notice(box, over):
    transport = loops._start_push_delivery(Provider())
    try:
        with pytest.raises(notice_relay.NoticeRefused):
            await notice_relay.accept_member_notice(notice_body(**over))
        assert rows(transport) == []
    finally:
        await loops._stop_push_delivery()


async def test_a_member_cannot_extend_a_notices_life(box):
    transport = loops._start_push_delivery(Provider())
    try:
        now = time.time()
        await notice_relay.accept_member_notice(notice_body(kind="notify", expires_at=now + 10 * 86400))
        with sqlite3.connect(transport.path) as db:
            (expires_at,) = db.execute("SELECT expires_at FROM attempts").fetchone()
        assert expires_at <= now + 3600 + 5  # a notify lives an hour
    finally:
        await loops._stop_push_delivery()


async def test_origin_is_forgotten_when_the_notice_expires(box):
    transport = loops._start_push_delivery(Provider())
    try:
        transport.remember_origin("gone", MEMBER, time.time() - 1)
        assert transport.origin_of("gone") is None
    finally:
        await loops._stop_push_delivery()


async def test_no_push_sender_running_is_not_an_error(box):
    assert push_transport.active_transport() is None
    assert await notice_relay.accept_member_notice(notice_body()) is False


# ── the routes ───────────────────────────────────────────────────────────────────────


def client():
    app = FastAPI()
    register_notice_routes(app)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub")


async def test_route_takes_a_notice_only_from_the_fleet_token(box):
    transport = loops._start_push_delivery(Provider())
    try:
        async with client() as http:
            for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}):
                refused = await http.post("/api/fleet/notices", json=notice_body(), headers=headers)
                assert refused.status_code == 403
            assert rows(transport) == []
            ok = await http.post("/api/fleet/notices", json=notice_body(), headers={"Authorization": f"Bearer {TOKEN}"})
            assert ok.status_code == 200 and ok.json() == {"ok": True, "queued": True}
            bad = await http.post(
                "/api/fleet/notices", json=notice_body(member="stranger"), headers={"Authorization": f"Bearer {TOKEN}"}
            )
            assert bad.status_code == 400 and bad.json()["error"] == "invalid_notice"
            origin = await http.get("/api/notices/decision-dec-1")
            assert origin.json() == {"ok": True, "agent": MEMBER}
            assert origin.headers["cache-control"] == "no-store"
            assert (await http.get("/api/notices/unknown-notice")).json() == {"ok": True, "agent": None}
    finally:
        await loops._stop_push_delivery()


# ── member side ──────────────────────────────────────────────────────────────────────


def a_notice(notice_id="decision-dec-9"):
    now = time.time()
    return ChatNotice("question", "high", "PRIVATE question text", "http://x/app/?session=s", notice_id, now, now + 60)


@pytest.fixture
def member(box, monkeypatch):
    monkeypatch.setenv(notice_relay.HUB_URL_ENV, "http://127.0.0.1:7870")
    monkeypatch.setattr(notice_relay, "member_id", lambda: MEMBER)
    monkeypatch.setattr(notice_relay, "_RETRY_DELAYS", (0.0, 0.0))


async def test_member_hands_identity_only_to_its_hub(member):
    seen = []

    def hub(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "queued": True})

    assert await notice_relay.hand_to_hub(a_notice(), transport=httpx.MockTransport(hub)) is True
    (request,) = seen
    assert str(request.url) == "http://127.0.0.1:7870/api/fleet/notices"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert set(body) == {"member", "notice_id", "kind", "importance", "created_at", "expires_at"}
    assert body["member"] == MEMBER and body["notice_id"] == "decision-dec-9"
    assert b"PRIVATE" not in request.content and b"session" not in request.content


async def test_member_retries_a_restarting_hub_but_not_a_refusal(member):
    answers = [httpx.Response(503), httpx.ConnectError("down"), httpx.Response(200, json={"ok": True})]
    calls = []

    def flaky(request):
        calls.append(1)
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert await notice_relay.hand_to_hub(a_notice(), transport=httpx.MockTransport(flaky)) is True
    assert len(calls) == 3

    refusals = []

    def refuse(request):
        refusals.append(1)
        return httpx.Response(403, json={"ok": False})

    assert await notice_relay.hand_to_hub(a_notice(), transport=httpx.MockTransport(refuse)) is False
    assert len(refusals) == 1


@pytest.mark.parametrize("value", [
    "", "https://127.0.0.1:7870", "http://100.119.239.8:7870", "http://example.com:7870",
    "http://127.0.0.1", "http://127.0.0.1:7870/agents/x", "http://user@127.0.0.1:7870", "not a url",
])
async def test_member_only_talks_to_a_loopback_hub(member, monkeypatch, value):
    monkeypatch.setenv(notice_relay.HUB_URL_ENV, value)
    assert notice_relay.hub_url() == ""

    def never(request):
        raise AssertionError("no request may be made")

    assert await notice_relay.hand_to_hub(a_notice(), transport=httpx.MockTransport(never)) is False


async def test_member_startup_joins_the_notice_seam_with_the_handoff(member, monkeypatch):
    handed = []

    async def record(notice):
        handed.append(notice.notice_id)

    monkeypatch.setattr("graph.workspaces.manager.is_workspace_member", lambda: True)
    monkeypatch.setattr(notice_relay, "hand_to_hub", record)
    assert loops._start_push_delivery(Provider()) is None
    try:
        assert list(chat_surface._notifiers) == [notice_relay.ADAPTER_KEY]
        notice_id = notify_chat_surfaces("needs a decision", "", kind="question", importance="high")
        await settle(lambda: handed == [notice_id])
    finally:
        await loops._stop_push_delivery()
    assert notice_relay.ADAPTER_KEY not in chat_surface._notifiers


async def test_member_without_a_hub_address_registers_nothing(member, monkeypatch):
    monkeypatch.delenv(notice_relay.HUB_URL_ENV)
    monkeypatch.setattr("graph.workspaces.manager.is_workspace_member", lambda: True)
    assert loops._start_push_delivery(Provider()) is None
    assert chat_surface._notifiers == {}
    await loops._stop_push_delivery()


def test_hub_address_is_loopback_on_its_own_port():
    assert notice_relay.loopback_hub_url(7870) == "http://127.0.0.1:7870"
