"""Directive links + auto-fulfil (ADR 0129 D4/Q2): a directive linked to the issues, PRs,
board cards, scheduler jobs or tasks its approval covers fulfils itself — ``closed_by:
"system"``, with an attributed reason — once EVERY linked item reaches a terminal state,
over the REAL ledger and event paths (a real ``EventBus``; no mocks of the fulfil logic).
Unlinked directives keep today's behavior untouched.
"""

from __future__ import annotations

import pytest

from events.bus import EventBus
from graph.directive_autofulfil import LINK_CLOSED_TOPIC, sweep_directive_links, wire_directive_autofulfil
from graph.directives import (
    CLOSED_BY_SYSTEM,
    MAX_LINKS,
    STATUS_FULFILLED,
    DirectiveError,
    DirectiveStore,
    working_state_section,
)


@pytest.fixture
def store(tmp_path, monkeypatch) -> DirectiveStore:
    import runtime.state as rs

    s = DirectiveStore(tmp_path / "directives")
    monkeypatch.setattr(rs.STATE, "directive_store", s, raising=False)
    return s


@pytest.fixture
def bus(monkeypatch):
    """A real bus, wired the way production wires it: ``HOST.publish`` → ``bus.publish``,
    so the directive ledger's and task store's own publishes land on it."""
    from graph.plugins import host

    bus = EventBus()
    monkeypatch.setattr(host.HOST, "publish", bus.publish)
    return bus


# ── links on the ledger ─────────────────────────────────────────────────────────


def test_record_normalizes_links_and_infers_kinds(store):
    url = "https://github.com/acme/widgets/pull/5146"
    d = store.record(
        "s1", "cut the release once both merge",
        links=["acme/widgets#879", "bd-c8r5h", "task-3", "agent-12ab34cd", url, "acme/widgets#879", ""],
    )
    assert [(e["ref"], e["kind"], e["state"]) for e in d.links] == [
        ("acme/widgets#879", "github", "open"),
        ("bd-c8r5h", "card", "open"),
        ("task-3", "task", "open"),
        ("agent-12ab34cd", "job", "open"),
        (url, "github", "open"),
    ]  # deduped, empty refs dropped
    # The stored entries survive a reload through a fresh store over the same directory.
    again = DirectiveStore(store.path).get("s1", d.id)
    assert [e["ref"] for e in again.links] == [e["ref"] for e in d.links]
    assert store.record("s1", "plain").links == []  # unlinked directives carry no links


def test_record_rejects_refs_the_autofulfil_paths_could_never_resolve(store):
    with pytest.raises(DirectiveError, match="owner/repo#879"):
        store.record("s1", "q", links=["#879"])  # a bare #N names no repository
    with pytest.raises(DirectiveError, match="GitHub issue/PR URL"):
        store.record("s1", "q", links=["https://example.com/thing/7"])
    with pytest.raises(DirectiveError, match=str(MAX_LINKS)):
        store.record("s1", "q", links=[f"bd-{n}" for n in range(MAX_LINKS + 1)])
    assert store.list("s1") == []  # nothing was recorded by the rejected calls


# ── auto-fulfil over the real bus ───────────────────────────────────────────────


def test_board_card_done_fulfils_with_attributed_reason(store, bus):
    wire_directive_autofulfil(bus.subscribe_handler, store)
    d = store.record("s1", "merge it when the board card lands", links=["bd-c8r5h"])

    bus.publish(LINK_CLOSED_TOPIC, {"ref": "bd-c8r5h", "reason": "merged (#5146)"})

    got = store.get("s1", d.id)
    assert got.status == STATUS_FULFILLED and got.closed_by == CLOSED_BY_SYSTEM and got.closed_at
    assert got.close_reason == "bd-c8r5h merged (#5146)"


def test_task_close_through_the_real_task_store_fulfils(store, bus, tmp_path):
    """The REAL ``TaskStore.close`` publish (``task.changed``, action ``closed``) is what
    fulfils a directive linked to that task — no hand-built event."""
    from tasks.store import TaskStore

    wire_directive_autofulfil(bus.subscribe_handler, store)
    tasks = TaskStore(db_path=str(tmp_path / "issues.db"))
    row = tasks.create("ship the fix")
    d = store.record("s1", "approved once the fix task closes", links=[row["id"]])

    tasks.update(row["id"], status="in_progress")  # non-terminal actions say nothing
    assert store.get("s1", d.id).in_force
    tasks.close(row["id"], reason="shipped")

    got = store.get("s1", d.id)
    assert got.status == STATUS_FULFILLED and got.closed_by == CLOSED_BY_SYSTEM
    assert got.close_reason == f"{row['id']} closed"


def _make_scheduler(tmp_path, bus):
    """A real sqlite-backed LocalScheduler publishing on the real bus — the production
    ``event_publish`` wiring (``server.agent_init``)."""
    from scheduler.local import LocalScheduler

    return LocalScheduler(
        agent_name="dir-test",
        invoke_url="http://127.0.0.1:7870",
        api_key="k",
        bearer_token="b",
        db_dir=tmp_path / "sched",
        event_publish=bus.publish,
    )


def _stub_a2a_delivery(monkeypatch):
    """Stub ONLY the network (``httpx.AsyncClient``) so the real ``_fire`` runs — including
    the ``turn.finished`` publish its ``finally`` makes BEFORE ``_fire_and_settle`` deletes
    the job row (the round-1 race) — against a completed, task-id-less A2A response."""
    import httpx

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"result": {"status": {"state": "TASK_STATE_COMPLETED"}}}

    class _Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())


async def test_one_shot_job_settle_fulfils_over_the_real_scheduler(store, bus, tmp_path, monkeypatch):
    """The REAL fire→settle path: ``turn.finished`` leaves ``_fire`` while the job row
    still exists, and only the scheduler's post-delete ``scheduler.changed`` publish
    resolves the link — the round-1 race, exercised end to end."""
    s = _make_scheduler(tmp_path, bus)
    _stub_a2a_delivery(monkeypatch)
    wire_directive_autofulfil(bus.subscribe_handler, store)
    job = s.add_job("run the sweep once", "2099-05-01T15:00:00+00:00", job_id="agent-job1")
    d = store.record("s1", "run the sweep once", links=["agent-job1"])

    live_at_turn_finished: list[bool] = []
    bus.subscribe_handler(
        "turn.finished",
        lambda p: live_at_turn_finished.append(store.get("s1", d.id).in_force)
        if p["data"].get("trigger") == "agent-job1" else None,
    )

    await s._fire_and_settle(job, cron=False)

    # turn.finished fired pre-delete — the directive was still in force then — and the
    # settle publish (not a job-table read racing it) is what fulfilled it after.
    assert live_at_turn_finished == [True]
    assert s.get_job("agent-job1") is None
    got = store.get("s1", d.id)
    assert got.status == STATUS_FULFILLED and got.closed_by == CLOSED_BY_SYSTEM
    assert got.close_reason == "agent-job1 finished"


async def test_cron_job_stays_linked_until_it_expires(store, bus, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, bus)
    _stub_a2a_delivery(monkeypatch)
    wire_directive_autofulfil(bus.subscribe_handler, store)
    s.add_job("hourly check", "0 * * * *", job_id="cron-b", max_fires=2)
    d = store.record("s1", "keep the cron until it runs out", links=["cron-b"])

    await s._fire_and_settle(s.list_jobs()[0], cron=True)
    assert store.get("s1", d.id).in_force  # fire 1 of 2 — the fire settled, the job didn't

    await s._fire_and_settle(s.list_jobs()[0], cron=True)  # max_fires reached → row deleted
    assert s.list_jobs() == []
    got = store.get("s1", d.id)
    assert got.status == STATUS_FULFILLED and got.closed_by == CLOSED_BY_SYSTEM
    assert got.close_reason == "cron-b expired"


async def test_cancel_schedule_tool_fulfils_the_linked_directive(store, bus, tmp_path):
    """The agent-side cancel: the REAL ``cancel_schedule`` tool over the real scheduler
    publishes the same ``scheduler.changed`` the console does (round-1 gap — it used to
    publish nothing, so a canceled job's link stayed open forever)."""
    from tools.scheduler_tools import _build_scheduler_tools

    s = _make_scheduler(tmp_path, bus)
    wire_directive_autofulfil(bus.subscribe_handler, store)
    s.add_job("recurring report", "0 * * * *", job_id="cron-c")
    d = store.record("s1", "keep the cron until I say", links=["cron-c"])

    cancel = next(t for t in _build_scheduler_tools(s) if t.name == "cancel_schedule")
    assert await cancel.ainvoke({"job_id": "nope"}) != "Canceled nope."
    assert store.get("s1", d.id).in_force  # a failed cancel publishes nothing
    assert await cancel.ainvoke({"job_id": "cron-c"}) == "Canceled cron-c."

    got = store.get("s1", d.id)
    assert got.status == STATUS_FULFILLED and got.closed_by == CLOSED_BY_SYSTEM
    assert got.close_reason == "cron-c canceled"


def test_unlinked_directives_are_untouched_by_every_event(store, bus):
    wire_directive_autofulfil(bus.subscribe_handler, store)
    plain = store.record("s1", "don't touch prod until I say so")
    manual = store.record("s1", "hold", links=["bd-1x"])
    waitish = store.record("s1", "odd link", links=["wait:s1"])
    store.close("s1", manual.id, status=STATUS_FULFILLED, reason="done by hand", closed_by="agent")

    bus.publish(LINK_CLOSED_TOPIC, {"ref": "bd-1x", "reason": "merged"})
    bus.publish("task.changed", {"id": "task-1", "action": "closed"})
    bus.publish("task.changed", {"id": "task-1", "action": "deleted"})
    bus.publish("scheduler.changed", {"job_id": "job-a", "action": "canceled"})
    # Non-terminal job actions and reused `wait:` ids never resolve a link.
    bus.publish("scheduler.changed", {"job_id": "wait:s1", "action": "finished"})
    for action in ("added", "updated", "enabled", "disabled", "restored"):
        bus.publish("scheduler.changed", {"job_id": "wait:s1", "action": action})

    got = store.get("s1", plain.id)
    assert got.in_force and got.links == [] and got.closed_at is None
    assert store.get("s1", waitish.id).in_force
    # A directive the agent already closed stays the agent's close — never re-written.
    assert store.get("s1", manual.id).closed_by == "agent"
    assert store.resolve_link("bd-nothing", "merged") == []


def test_fulfilled_directive_leaves_working_state_and_the_in_force_count(store, bus):
    """The server truth behind r4: Work's 'Your instructions' and the rail's 'Told it N'
    render the in-force set and refresh on ``directive.changed`` — both move here."""
    wire_directive_autofulfil(bus.subscribe_handler, store)
    d = store.record("s1", "file drafted issues + keep the #877 checklist current", links=["bd-aaaa1"])
    assert d.id in working_state_section("s1")
    seen: list[dict] = []
    bus.subscribe_handler("directive.changed", lambda p: seen.append(p["data"]))

    bus.publish(LINK_CLOSED_TOPIC, {"ref": "bd-aaaa1", "reason": "done"})

    assert working_state_section("s1") == ""
    assert store.in_force("s1") == []  # the GET route's in_force count — 'Told it N' — drops
    assert {"session_id": "s1", "id": d.id} in seen  # the push the console refreshes on


# ── two issues: in force while either is open (the periodic GitHub sweep) ───────


async def test_two_issues_fulfil_only_when_both_close(store, bus):
    d = store.record("s1", "work #879 then #873", links=["acme/widgets#879", "acme/widgets#873"])
    states = {"acme/widgets#879": "closed", "acme/widgets#873": "open"}
    seen: list[str] = []

    async def issue_state(ref):
        seen.append(ref)
        return states[ref]

    assert await sweep_directive_links(store, issue_state=issue_state) == 1  # one link resolved
    mid = store.get("s1", d.id)
    assert mid.in_force  # one of two issues still open — stays in force
    assert {e["ref"]: e["state"] for e in mid.links} == {"acme/widgets#879": "done", "acme/widgets#873": "open"}
    assert sorted(seen) == ["acme/widgets#873", "acme/widgets#879"]  # one gh read per distinct ref

    seen.clear()
    states["acme/widgets#873"] = "closed"
    assert await sweep_directive_links(store, issue_state=issue_state) == 1
    assert seen == ["acme/widgets#873"]  # the resolved ref is never fetched again
    done = store.get("s1", d.id)
    assert done.status == STATUS_FULFILLED and done.closed_by == CLOSED_BY_SYSTEM
    assert done.close_reason == "acme/widgets#879 closed; acme/widgets#873 closed"


async def test_sweep_is_free_when_no_github_ref_is_unresolved(store):
    store.record("s1", "card only", links=["bd-1x"])

    async def issue_state(ref):  # pragma: no cover - the sweep must return before any fetch
        raise AssertionError("no gh call expected")

    assert await sweep_directive_links(store, issue_state=issue_state) == 0


async def test_mixed_links_card_event_then_github_sweep(store, bus):
    wire_directive_autofulfil(bus.subscribe_handler, store)
    d = store.record("s1", "release once the card and the PR land", links=["bd-1x", "acme/widgets#7"])
    bus.publish(LINK_CLOSED_TOPIC, {"ref": "bd-1x", "reason": "merged (#1)"})
    assert store.get("s1", d.id).in_force

    async def issue_state(ref):
        return "closed"

    assert await sweep_directive_links(store, issue_state=issue_state) == 1
    done = store.get("s1", d.id)
    assert done.status == STATUS_FULFILLED
    assert done.close_reason == "bd-1x merged (#1); acme/widgets#7 closed"


# ── the agent-facing tool ───────────────────────────────────────────────────────


def test_record_directive_tool_links_roundtrip(store):
    from tools.directive_tools import build_directive_tools

    tool = next(t for t in build_directive_tools() if t.name == "record_directive")
    state = {"session_id": "s1", "operator_chat_turn": True}
    out = tool.invoke({"quote": "work #879–#884 then #873", "links": ["acme/widgets#879", "bd-7q"], "state": state})
    assert "linked to acme/widgets#879, bd-7q" in out and "fulfils itself" in out
    assert [e["kind"] for e in store.list("s1")[0].links] == ["github", "card"]
    # A ref the auto-fulfil paths could never resolve is refused, and records nothing.
    assert tool.invoke({"quote": "q", "links": ["#879"], "state": state}).startswith("Error:")
    assert len(store.list("s1")) == 1
