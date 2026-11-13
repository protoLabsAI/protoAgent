"""Task links + auto-close (ADR 0129 Q1/Q2): a task linked to the board cards, scheduler
jobs, directive or PRs that deliver it closes itself — with an attributed reason — when
every linked item reaches a terminal state, over the REAL store and event paths (a real
``EventBus``; no mocks of the close logic). Plus the new ``waiting`` status round-trip."""

from __future__ import annotations

import pytest

from events.bus import EventBus
from tasks.autoclose import TASK_LINK_CLOSED_TOPIC, sweep_task_links, wire_task_autoclose
from tasks.store import TaskStore, parse_links


@pytest.fixture
def store(tmp_path):
    return TaskStore(db_path=str(tmp_path / "issues.db"))


@pytest.fixture
def bus(monkeypatch):
    """A real bus, wired the way production wires it: ``HOST.publish`` → ``bus.publish``,
    so the directive ledger's and task store's own publishes land on it."""
    from graph.plugins import host

    bus = EventBus()
    monkeypatch.setattr(host.HOST, "publish", bus.publish)
    return bus


# ── links on the store ─────────────────────────────────────────────────────────


def test_create_normalizes_links_and_infers_kinds(store):
    pr = "https://github.com/acme/widgets/pull/5146"
    row = store.create("ship it", links=["bd-c8r5h", "agent-12ab34cd56ef", "dir-cfd234", pr, "bd-c8r5h", ""])
    entries = parse_links(row)
    assert [(e["ref"], e["kind"], e["state"]) for e in entries] == [
        ("bd-c8r5h", "card", "open"),
        ("agent-12ab34cd56ef", "job", "open"),
        ("dir-cfd234", "directive", "open"),
        (pr, "pr", "open"),
    ]  # deduped, empty refs dropped
    assert store.create("plain")["links"] == ""  # unlinked tasks carry no links


def test_update_replaces_and_clears_links(store):
    row = store.create("t", links=["bd-1x"])
    updated = store.update(row["id"], links=["job-2"])
    assert [e["ref"] for e in parse_links(updated)] == ["job-2"]
    assert store.update(row["id"], links=[])["links"] == ""  # [] clears; None keeps
    assert store.update(row["id"], title="renamed")["links"] == ""


def test_update_replacement_keeps_resolved_links_and_closes_when_all_done(store, bus):
    wire_task_autoclose(bus.subscribe_handler, store)
    events: list[tuple] = []
    bus.subscribe_handler("task.changed", lambda p: events.append((p["data"]["id"], p["data"]["action"])))
    linked = store.create("grows links", links=["bd-1x", "job-2"])
    bus.publish(TASK_LINK_CLOSED_TOPIC, {"ref": "bd-1x", "reason": "merged (#1)"})

    # Replacing links through update (the task_update/PATCH surface — plain refs only)
    # must NOT reset the already-finished card to open: its terminal event never fires
    # again, so the task could otherwise never close itself.
    grown = store.update(linked["id"], links=["bd-1x", "job-2", "bd-3z"])
    assert {e["ref"]: (e["state"], e["reason"]) for e in parse_links(grown)} == {
        "bd-1x": ("done", "merged (#1)"), "job-2": ("open", ""), "bd-3z": ("open", ""),
    }
    assert grown["status"] == "open"  # two refs still open — not closed by the replace

    # Narrowing to only the finished ref closes NOW, attributed — no event will come.
    closed = store.update(linked["id"], links=["bd-1x"])
    assert closed["status"] == "closed" and closed["closed_at"]
    assert closed["close_reason"] == "bd-1x merged (#1)"
    assert (linked["id"], "closed") in events


def test_links_column_migrates_onto_a_legacy_board(tmp_path):
    import sqlite3

    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE issues (id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '', "
        "status TEXT DEFAULT 'open', priority INTEGER DEFAULT 2, issue_type TEXT DEFAULT 'task', "
        "assignee TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "closed_at TEXT, close_reason TEXT, session_id TEXT NOT NULL DEFAULT '')"
    )
    conn.execute("INSERT INTO issues (id, title, created_at, updated_at) VALUES ('task-1','old','t','t')")
    conn.commit()
    conn.close()
    migrated = TaskStore(db_path=db)
    assert migrated.list()[0]["links"] == ""  # backfilled default
    assert parse_links(migrated.create("new", links=["bd-9"]))[0]["ref"] == "bd-9"


# ── auto-close over the real bus ───────────────────────────────────────────────


def test_board_card_terminal_closes_with_attributed_reason(store, bus):
    wire_task_autoclose(bus.subscribe_handler, store)
    events: list[tuple] = []
    bus.subscribe_handler("task.changed", lambda p: events.append((p["data"]["id"], p["data"]["action"])))
    linked = store.create("release v0.238", links=["bd-c8r5h"])

    bus.publish(TASK_LINK_CLOSED_TOPIC, {"ref": "bd-c8r5h", "reason": "merged (#5146)"})

    row = store.get(linked["id"])
    assert row["status"] == "closed" and row["closed_at"]
    assert row["close_reason"] == "bd-c8r5h merged (#5146)"
    assert (linked["id"], "closed") in events


def test_two_links_close_only_when_both_finish(store, bus):
    wire_task_autoclose(bus.subscribe_handler, store, get_job=lambda job_id: None)
    linked = store.create("two-part", links=["bd-1x", "agent-job1"])

    bus.publish(TASK_LINK_CLOSED_TOPIC, {"ref": "bd-1x", "reason": "merged (#1)"})
    mid = store.get(linked["id"])
    assert mid["status"] == "open"  # one of two links done — not closed yet
    assert {e["ref"]: e["state"] for e in parse_links(mid)} == {"bd-1x": "done", "agent-job1": "open"}

    bus.publish("turn.finished", {"origin": "scheduler", "trigger": "agent-job1", "ok": True})
    done = store.get(linked["id"])
    assert done["status"] == "closed"
    assert done["close_reason"] == "bd-1x merged (#1); agent-job1 finished"


def test_job_settles_only_when_the_job_is_gone(store, bus):
    jobs = {"cron-b": object()}  # still-live recurring job
    wire_task_autoclose(bus.subscribe_handler, store, get_job=jobs.get)
    one_shot = store.create("one-shot", links=["job-a"])
    recurring = store.create("recurring", links=["cron-b"])

    bus.publish("turn.finished", {"origin": "scheduler", "trigger": "job-a", "ok": True})
    bus.publish("turn.finished", {"origin": "scheduler", "trigger": "cron-b", "ok": True})
    assert store.get(one_shot["id"])["status"] == "closed"
    assert store.get(one_shot["id"])["close_reason"] == "job-a finished"
    assert store.get(recurring["id"])["status"] == "open"  # the fire settled, the job didn't

    # Cancel settles it — the same scheduler.changed the console/cancel tool publish.
    bus.publish("scheduler.changed", {"job_id": "cron-b", "action": "canceled"})
    assert store.get(recurring["id"])["close_reason"] == "cron-b canceled"


async def test_real_fire_settlement_closes_linked_tasks(store, bus, tmp_path, monkeypatch):
    """Through the REAL scheduler path, not a hand-published event: ``_fire_and_settle``
    over a live jobs.db, the stubbed transport answering a completed turn WITHOUT a
    durable task id (the fallback path). Its ``turn.finished`` must land only after the
    one-shot row is deleted / the max_fires cron expires — sync handlers run inline, so
    published any earlier the auto-close handler reads the row still live and the
    linked task never closes."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    import httpx

    from scheduler.local import LocalScheduler

    class _Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, _url, headers=None, json=None):
            return SimpleNamespace(
                status_code=200, text="",
                json=lambda: {"result": {"status": {"state": "TASK_STATE_COMPLETED"}}},
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    s = LocalScheduler(
        agent_name="autoclose-fire", invoke_url="http://127.0.0.1:7870",
        db_dir=tmp_path, event_publish=bus.publish,
    )
    wire_task_autoclose(bus.subscribe_handler, store, get_job=s.get_job)
    live_at_finish: list[bool] = []
    bus.subscribe_handler(
        "turn.finished", lambda p: live_at_finish.append(s.get_job(p["data"]["trigger"]) is not None)
    )
    once = s.add_job("ship the digest", (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                     job_id="agent-once", context_id="chat-1")
    cron = s.add_job("hourly sweep", "0 * * * *", job_id="agent-cron", max_fires=1, context_id="chat-2")
    one_shot_task = store.create("one-shot delivery", links=["agent-once"])
    cron_task = store.create("bounded cron", links=["agent-cron"])

    await s._fire_and_settle(once, cron=False)
    await s._fire_and_settle(cron, cron=True)

    # turn.finished lands while each row still exists; the scheduler's own post-settle
    # `scheduler.changed` (finished / expired) is what closes the linked task — the same
    # contract linked directives use (graph.directive_autofulfil).
    assert live_at_finish == [True, True]
    for linked, reason in ((one_shot_task, "agent-once finished"), (cron_task, "agent-cron expired")):
        row = store.get(linked["id"])
        assert row["status"] == "closed" and row["close_reason"] == reason


async def test_cancel_schedule_tool_publish_closes_linked_task(store, bus):
    """Through the TOOL itself, not a hand-built event: ``cancel_schedule``'s own
    ``scheduler.changed`` publish is what closes a task linked to the canceled job."""
    from types import SimpleNamespace

    from tools.scheduler_tools import _build_scheduler_tools

    wire_task_autoclose(bus.subscribe_handler, store, get_job=lambda job_id: object())
    linked = store.create("recurring sweep", links=["cron-7"])
    by_name = {t.name: t for t in _build_scheduler_tools(SimpleNamespace(cancel_job=lambda job_id: True))}

    assert await by_name["cancel_schedule"].ainvoke({"job_id": "cron-7"}) == "Canceled cron-7."
    row = store.get(linked["id"])
    assert row["status"] == "closed"
    assert row["close_reason"] == "cron-7 canceled"


def test_no_scheduler_wired_never_reads_as_job_gone(store, bus):
    wire_task_autoclose(bus.subscribe_handler, store)  # default get_job: no scheduler → raises
    linked = store.create("careful", links=["job-x"])
    bus.publish("turn.finished", {"origin": "scheduler", "trigger": "job-x", "ok": True})
    assert store.get(linked["id"])["status"] == "open"


def test_directive_close_fulfils_linked_task(store, bus, tmp_path):
    from graph.directives import STATUS_FULFILLED, DirectiveStore

    dstore = DirectiveStore(base_dir=tmp_path / "directives")
    wire_task_autoclose(bus.subscribe_handler, store, get_directive=dstore.get)
    directive = dstore.record("sess-1", "cut the release once #A merges")  # publish: still in force
    linked = store.create("release approval", links=[directive.id], session_id="sess-1")
    assert store.get(linked["id"])["status"] == "open"

    dstore.close("sess-1", directive.id, status=STATUS_FULFILLED, reason="shipped", closed_by="agent")
    row = store.get(linked["id"])
    assert row["status"] == "closed"
    assert row["close_reason"] == f"{directive.id} fulfilled: shipped"


def test_unlinked_tasks_are_untouched_by_every_event(store, bus):
    wire_task_autoclose(bus.subscribe_handler, store, get_job=lambda job_id: None)
    plain = store.create("unlinked")
    bus.publish(TASK_LINK_CLOSED_TOPIC, {"ref": "bd-c8r5h", "reason": "merged"})
    bus.publish("turn.finished", {"origin": "scheduler", "trigger": "job-a", "ok": True})
    bus.publish("scheduler.changed", {"job_id": "job-a", "action": "canceled"})
    row = store.get(plain["id"])
    assert row["status"] == "open" and row["updated_at"] == plain["updated_at"]
    assert store.resolve_link("bd-nothing", "merged") == []


# ── periodic PR sweep ──────────────────────────────────────────────────────────


async def test_pr_sweep_closes_merged_and_leaves_open(store):
    merged = "https://github.com/acme/widgets/pull/1"
    still_open = "https://github.com/acme/widgets/pull/2"
    done = store.create("merged pr", links=[merged])
    pending = store.create("open pr", links=[still_open])
    states = {merged: "merged", still_open: "open"}
    seen: list[str] = []

    async def pr_state(url):
        seen.append(url)
        return states[url]

    assert await sweep_task_links(store, pr_state=pr_state) == 1
    assert sorted(seen) == sorted([merged, still_open])  # one gh read per distinct PR
    assert store.get(done["id"])["close_reason"] == f"{merged} merged"
    assert store.get(pending["id"])["status"] == "open"

    # The merged PR's link is resolved, so only the still-open PR is fetched again.
    seen.clear()
    assert await sweep_task_links(store, pr_state=pr_state) == 0
    assert seen == [still_open]


# ── waiting status + tool/route round-trips ────────────────────────────────────


def test_waiting_roundtrips_store_and_tools(store):
    from tools.lg_tools import get_all_tools

    by_name = {getattr(t, "name", ""): t for t in get_all_tools(tasks_store=store)}
    out = by_name["task_create"].invoke({"title": "needs a call", "links": ["bd-7q"]})
    assert "task-1" in out
    assert parse_links(store.get("task-1"))[0]["ref"] == "bd-7q"
    by_name["task_update"].invoke({"issue_id": "task-1", "status": "waiting"})
    assert store.get("task-1")["status"] == "waiting"
    listing = by_name["task_list"].invoke({})
    assert "[waiting]" in listing and "links=bd-7q" in listing
    by_name["task_update"].invoke({"issue_id": "task-1", "links": ["bd-7q", "job-9"]})
    assert [e["ref"] for e in parse_links(store.get("task-1"))] == ["bd-7q", "job-9"]


async def test_waiting_and_links_roundtrip_operator_routes(store):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from operator_api.routes import register_operator_routes

    app = FastAPI()
    register_operator_routes(
        app, runtime_status=lambda: {}, subagent_list=lambda: [],
        subagent_run=lambda r: None, subagent_batch=lambda r: None, tasks_store=store,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/tasks/issues", json={"title": "from console", "links": ["bd-5a", "job-9"]})
        assert created.status_code == 200, created.text
        issue = created.json()["issue"]
        assert [e["ref"] for e in parse_links(issue)] == ["bd-5a", "job-9"]

        # A PATCH that replaces links keeps the ref the store already resolved done.
        store.resolve_link("bd-5a", "merged (#2)")
        replaced = await client.patch(f"/api/tasks/issues/{issue['id']}", json={"links": ["bd-5a", "job-9"]})
        assert replaced.status_code == 200, replaced.text
        assert {e["ref"]: e["state"] for e in parse_links(replaced.json()["issue"])} == {"bd-5a": "done", "job-9": "open"}

        patched = await client.patch(f"/api/tasks/issues/{issue['id']}", json={"status": "waiting"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["issue"]["status"] == "waiting"

        listed = await client.get("/api/tasks/issues")
        assert [i["status"] for i in listed.json()["issues"]] == ["waiting"]
