"""A fleet member's notice reaches the operator's devices through its hub (#5614)."""
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from graph import notice_hold, push_transport
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
        await settle(lambda: rows(transport))  # the seam queues the push adapter; it does not await it
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
            assert origin.json() == {"ok": True, "agent": MEMBER, "link": None}
            assert origin.headers["cache-control"] == "no-store"
            assert (await http.get("/api/notices/unknown-notice")).json() == {"ok": True, "agent": None, "link": None}
    finally:
        await loops._stop_push_delivery()


# ── the hub's push policy (#5648) ────────────────────────────────────────────────────


@pytest.fixture
def default_policy(box, monkeypatch):
    """The hub's live config as shipped: ``push.all_notices`` off, no quiet hours (so a
    missing ledger row is the push policy's doing, not a hold)."""
    config = SimpleNamespace(push_all_notices=False, quiet_hours_enabled=False)
    monkeypatch.setattr("graph.plugins.host.HOST.config", lambda: config)
    return config


async def relay(http, **over):
    return await http.post("/api/fleet/notices", json=notice_body(**over), headers={"Authorization": f"Bearer {TOKEN}"})


async def test_a_relayed_routine_notice_is_taken_but_never_reaches_the_push_ledger(default_policy):
    transport = loops._start_push_delivery(Provider())
    try:
        async with client() as http:
            taken = await relay(http, notice_id="job-result-1", kind="review", importance="normal")
        # 2xx, so the member does not retry a notice the hub's policy keeps off push.
        assert taken.status_code == 200 and taken.json() == {"ok": True, "queued": True}
        assert rows(transport) == []
        assert transport.provider.calls == []
        # The hub still knows who raised it.
        assert transport.origin_of("job-result-1") == MEMBER
    finally:
        await loops._stop_push_delivery()


async def test_a_relayed_question_reaches_the_push_ledger_at_normal_importance(default_policy):
    transport = loops._start_push_delivery(Provider())
    try:
        async with client() as http:
            taken = await relay(http, notice_id="decision-dec-2", kind="question", importance="normal")
        assert taken.status_code == 200 and taken.json() == {"ok": True, "queued": True}
        await settle(lambda: rows(transport) == [("decision-dec-2", "question", "normal", "accepted")])
        assert transport.origin_of("decision-dec-2") == MEMBER
    finally:
        await loops._stop_push_delivery()


async def test_all_notices_on_sends_a_relayed_routine_notice_to_the_push_ledger(default_policy):
    default_policy.push_all_notices = True
    transport = loops._start_push_delivery(Provider())
    try:
        async with client() as http:
            taken = await relay(http, notice_id="job-result-2", kind="review", importance="normal")
        assert taken.status_code == 200 and taken.json() == {"ok": True, "queued": True}
        await settle(lambda: rows(transport) == [("job-result-2", "review", "normal", "accepted")])
    finally:
        await loops._stop_push_delivery()


# ── the hub's quiet hours (#5648) ────────────────────────────────────────────────────


def window_around_now(hours=2):
    """A (start, end) HH:MM pair in UTC with the current time well inside it."""
    now = datetime.now(timezone.utc)
    return tuple((now + timedelta(hours=h)).strftime("%H:%M") for h in (-hours, hours))


@pytest.fixture
def hub_hold(box, monkeypatch):
    """The hub's live config (every notice pushes, quiet hours covering now) and its hold."""
    start, end = window_around_now()
    config = SimpleNamespace(
        push_all_notices=True, quiet_hours_enabled=True,
        quiet_hours_start=start, quiet_hours_end=end, quiet_hours_timezone="UTC",
    )
    monkeypatch.setattr("graph.plugins.host.HOST.config", lambda: config)
    delivery = notice_hold.QuietHoursDelivery()
    monkeypatch.setattr(notice_hold, "DELIVERY", delivery)
    return SimpleNamespace(config=config, held=lambda: [text for _, text in delivery._store().rows()])


async def test_hub_quiet_hours_hold_a_members_routine_notice(hub_hold):
    transport = loops._start_push_delivery(Provider())
    try:
        async with client() as http:
            body = notice_body(notice_id="job-1", kind="notify", importance="normal")
            held = await http.post("/api/fleet/notices", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
        # 2xx, so the member does not retry a notice the hub is holding.
        assert held.status_code == 200 and held.json() == {"ok": True, "queued": True}
        # The held text is the hub's wording and the member's name: nothing the member sent.
        assert hub_hold.held() == ["A notice from protoEngineer"]
        assert rows(transport) == []
        assert transport.origin_of("job-1") == MEMBER
    finally:
        await loops._stop_push_delivery()


@pytest.mark.parametrize("kind,importance", [("question", "normal"), ("notify", "high"), ("review", "high")])
async def test_hub_quiet_hours_never_hold_a_members_question_or_urgent_notice(hub_hold, kind, importance):
    transport = loops._start_push_delivery(Provider())
    try:
        body = notice_body(notice_id="urgent-1", kind=kind, importance=importance)
        assert await notice_relay.accept_member_notice(body) is True
        await settle(lambda: rows(transport) == [("urgent-1", kind, importance, "accepted")])
        assert hub_hold.held() == []
    finally:
        await loops._stop_push_delivery()


async def test_outside_quiet_hours_a_members_routine_notice_is_pushed(hub_hold, monkeypatch):
    transport = loops._start_push_delivery(Provider())
    try:
        hub_hold.config.quiet_hours_enabled = False
        assert await notice_relay.accept_member_notice(notice_body(notice_id="job-2", kind="notify", importance="normal"))
        # On, but the window is elsewhere in the day.
        hub_hold.config.quiet_hours_enabled = True
        hub_hold.config.quiet_hours_start, hub_hold.config.quiet_hours_end = window_around_now(-2)
        assert await notice_relay.accept_member_notice(notice_body(notice_id="job-3", kind="notify", importance="normal"))
        await settle(lambda: sorted(r[0] for r in rows(transport) if r[3] == "accepted") == ["job-2", "job-3"])
        assert hub_hold.held() == []
        # A member the registry lists without a name is called by its id.
        monkeypatch.setattr("graph.workspaces.manager._find", lambda ident: {"id": MEMBER})
        hub_hold.config.quiet_hours_start, hub_hold.config.quiet_hours_end = window_around_now()
        assert await notice_relay.accept_member_notice(notice_body(notice_id="job-4", kind="notify", importance="normal"))
        assert hub_hold.held() == [f"A notice from {MEMBER}"]
    finally:
        await loops._stop_push_delivery()


# ── the hub's notice seam (#5648 fix 7) ──────────────────────────────────────────────


@pytest.fixture
def bus(monkeypatch):
    """What the hub publishes on its event bus (the stream the desktop app reads)."""
    published = []
    monkeypatch.setattr("graph.plugins.host.HOST.publish", lambda topic, data: published.append((topic, data)))
    return published


def second_adapter():
    """One more notice adapter beside the push transport, as a hub chat plugin would be."""
    seen = []

    async def hook(notice):
        seen.append(notice)

    chat_surface.register_notice_adapter("second-adapter", hook)
    return seen


async def test_a_members_notice_is_published_on_the_hub_bus(box, bus):
    loops._start_push_delivery(Provider())
    try:
        before = time.time()
        assert await notice_relay.accept_member_notice(notice_body()) is True
        ((topic, data),) = bus
        assert topic == "notice"
        assert (data["notice_id"], data["kind"], data["importance"]) == ("decision-dec-1", "question", "high")
        # The hub's own wording and no link: nothing the member sent.
        assert (data["text"], data["link"]) == ("A notice from protoEngineer", "")
        assert before <= data["created_at"] < data["expires_at"] <= time.time() + 86400
    finally:
        await loops._stop_push_delivery()


async def test_a_members_notice_reaches_every_notice_adapter_and_still_the_push_ledger(box):
    transport = loops._start_push_delivery(Provider())
    seen = second_adapter()
    try:
        assert await notice_relay.accept_member_notice(notice_body()) is True
        await settle(lambda: len(seen) == 1 and rows(transport) == [("decision-dec-1", "question", "high", "accepted")])
        (notice,) = seen
        assert (notice.notice_id, notice.kind, notice.importance) == ("decision-dec-1", "question", "high")
        assert (notice.text, notice.link) == ("A notice from protoEngineer", "")
    finally:
        chat_surface.unregister_notice_adapter("second-adapter")
        await loops._stop_push_delivery()


async def test_quiet_hours_keep_a_members_routine_notice_from_every_adapter(hub_hold, bus):
    transport = loops._start_push_delivery(Provider())
    seen = second_adapter()
    try:
        routine = notice_body(notice_id="job-9", kind="notify", importance="normal")
        assert await notice_relay.accept_member_notice(routine) is True  # held is still taken
        assert hub_hold.held() == ["A notice from protoEngineer"]
        # Not on the bus either: the desktop app shows every `notice` event as a native
        # banner and has no quiet-hours check of its own.
        assert bus == []
        # A question raised after it is not held, and is the only one any adapter gets.
        assert await notice_relay.accept_member_notice(notice_body(notice_id="decision-dec-3")) is True
        await settle(lambda: seen and rows(transport) == [("decision-dec-3", "question", "high", "accepted")])
        assert [notice.notice_id for notice in seen] == ["decision-dec-3"]
        assert [(topic, data["notice_id"]) for topic, data in bus] == [("notice", "decision-dec-3")]
        assert hub_hold.held() == ["A notice from protoEngineer"]
    finally:
        chat_surface.unregister_notice_adapter("second-adapter")
        await loops._stop_push_delivery()


async def test_outside_quiet_hours_a_members_routine_notice_is_on_the_hub_bus(hub_hold, bus):
    hub_hold.config.quiet_hours_enabled = False
    loops._start_push_delivery(Provider())
    try:
        routine = notice_body(notice_id="job-9", kind="notify", importance="normal")
        assert await notice_relay.accept_member_notice(routine) is True
        assert [(topic, data["notice_id"]) for topic, data in bus] == [("notice", "job-9")]
        assert hub_hold.held() == []
    finally:
        await loops._stop_push_delivery()


async def test_a_members_notice_writes_no_home_entry(box, monkeypatch):
    entries = []
    monkeypatch.setattr("graph.home_entries.post_home_entry", lambda *args, **kwargs: entries.append(args))
    transport = loops._start_push_delivery(Provider())
    try:
        assert await notice_relay.accept_member_notice(notice_body()) is True
        await settle(lambda: rows(transport) == [("decision-dec-1", "question", "high", "accepted")])
        assert entries == []
        # The hub's own notice does write one, so the recorder above is on the real path.
        notify_chat_surfaces("the hub's own notice", "")
        assert [args[2] for args in entries] == ["the hub's own notice"]
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
