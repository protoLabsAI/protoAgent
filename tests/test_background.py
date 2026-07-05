"""Tests for background subagents (ADR 0050) — the durable store + the
self-POST manager.

The store's exactly-once drain and restart reconciliation are the parts most
likely to regress (they back the "notify the model exactly once" guarantee). The
manager's firing path is covered by stubbing ``httpx.AsyncClient`` so a unit test
doesn't need a running A2A endpoint — same approach as the scheduler tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from background.manager import BackgroundManager, _build_fired_prompt
from background.store import BackgroundStore


def _store(tmp_path: Path) -> BackgroundStore:
    return BackgroundStore(str(tmp_path / "background" / "jobs.db"))


# ── store ────────────────────────────────────────────────────────────────────


class TestStore:
    def test_create_is_running(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(
            agent_name="a",
            origin_session="s1",
            subagent_type="researcher",
            description="dig",
            prompt="go",
        )
        assert jid.startswith("bg-")
        job = s.get(jid)
        assert job is not None
        assert job.status == "running"
        assert job.notified is False
        assert job.origin_session == "s1"

    def test_no_drain_while_running(self, tmp_path):
        s = _store(tmp_path)
        s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        assert s.drain_pending("s1") == []

    def test_mark_complete_is_idempotent(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        assert s.mark_complete(jid, "completed", "the answer") is True
        # a second (e.g. delivery-failure) write must NOT clobber the real result
        assert s.mark_complete(jid, "failed", "nope") is False
        assert s.get(jid).status == "completed"
        assert s.get(jid).result == "the answer"

    def test_drain_is_exactly_once(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        s.mark_complete(jid, "completed", "result text")
        first = s.drain_pending("s1")
        assert [j.id for j in first] == [jid]
        assert first[0].result == "result text"
        # drained once → never again
        assert s.drain_pending("s1") == []

    def test_drain_is_session_scoped(self, tmp_path):
        s = _store(tmp_path)
        a = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        b = s.create(agent_name="a", origin_session="s2", subagent_type="researcher", description="d", prompt="p")
        s.mark_complete(a, "completed", "ra")
        s.mark_complete(b, "completed", "rb")
        assert [j.id for j in s.drain_pending("s1")] == [a]
        assert [j.id for j in s.drain_pending("s2")] == [b]

    def test_failed_jobs_drain_too(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        s.mark_complete(jid, "failed", "boom")
        drained = s.drain_pending("s1")
        assert [(j.id, j.status) for j in drained] == [(jid, "failed")]

    def test_reconcile_fails_running_jobs(self, tmp_path):
        s = _store(tmp_path)
        running = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        done = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d2", prompt="p2")
        s.mark_complete(done, "completed", "ok")
        assert s.reconcile_interrupted() == 1  # only the running one
        assert s.get(running).status == "failed"
        assert s.get(done).status == "completed"

    def test_list_filters(self, tmp_path):
        s = _store(tmp_path)
        a = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        s.create(agent_name="a", origin_session="s2", subagent_type="researcher", description="d", prompt="p")
        s.mark_complete(a, "completed", "x")
        assert {j.id for j in s.list(origin_session="s1")} == {a}
        assert {j.status for j in s.list(status="completed")} == {"completed"}
        assert len(s.list()) == 2

    def test_dismiss_hides_a_finished_job_but_retains_it(self, tmp_path):
        # #1808: dismiss is a SOFT flag — the job drops out of the panel listing but its row
        # (and report) are retained, so the chat card can still open the full report by id.
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        s.mark_complete(jid, "completed", "done")
        assert s.dismiss(jid) is True
        assert s.get(jid) is not None and s.get(jid).dismissed is True  # retained
        assert {j.id for j in s.list()} == set()  # but hidden from the panel
        assert s.dismiss(jid) is False  # already dismissed — idempotent

    def test_dismiss_keeps_a_running_job(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        assert s.dismiss(jid) is False  # running jobs are kept — cancel first
        assert {j.id for j in s.list()} == {jid}  # still shown

    def test_dismiss_finished_hides_only_finished(self, tmp_path):
        s = _store(tmp_path)
        done = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        s.mark_complete(done, "completed", "r")
        run = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        assert s.dismiss_finished() == 1
        assert {j.id for j in s.list()} == {run}  # finished hidden, running kept
        assert s.get(done) is not None  # retained, just hidden

    def test_dismiss_finished_is_session_scoped(self, tmp_path):
        s = _store(tmp_path)
        a = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        b = s.create(agent_name="a", origin_session="s2", subagent_type="researcher", description="d", prompt="p")
        s.mark_complete(a, "completed", "ra")
        s.mark_complete(b, "completed", "rb")
        assert s.dismiss_finished("s1") == 1
        assert {j.id for j in s.list()} == {b}  # only s2 still shown
        assert s.get(a) is not None  # a retained, just hidden

    # ── fan-out batches (#1766) ───────────────────────────────────────────────

    def test_batch_id_persists_and_defaults_none(self, tmp_path):
        s = _store(tmp_path)
        lone = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        member = s.create(
            agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p",
            batch_id="batch-A",
        )
        assert s.get(lone).batch_id is None  # unbatched spawn → NULL
        assert s.get(member).batch_id == "batch-A"

    def test_batch_size_and_outstanding(self, tmp_path):
        s = _store(tmp_path)
        ids = [
            s.create(
                agent_name="a", origin_session="s1", subagent_type="researcher", description=f"d{i}", prompt="p",
                batch_id="batch-A",
            )
            for i in range(3)
        ]
        # a job in a DIFFERENT batch (and one unbatched) must not leak into the counts
        s.create(
            agent_name="a", origin_session="s1", subagent_type="researcher", description="other", prompt="p",
            batch_id="batch-B",
        )
        s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="lone", prompt="p")
        assert s.batch_size("batch-A") == 3
        assert s.batch_outstanding("batch-A") == 3  # all running
        s.mark_complete(ids[0], "completed", "r0")
        assert s.batch_outstanding("batch-A") == 2  # a queued/running sibling still counts
        s.mark_complete(ids[1], "failed", "boom")
        s.mark_complete(ids[2], "completed", "r2")
        assert s.batch_outstanding("batch-A") == 0  # fully settled
        assert s.batch_size("batch-A") == 3  # size never changes
        # null / unknown batch is empty, never an error
        assert s.batch_size("") == 0 and s.batch_size("nope") == 0
        assert s.batch_outstanding("") == 0

    def test_batch_status_counts(self, tmp_path):
        s = _store(tmp_path)
        ids = [
            s.create(
                agent_name="a", origin_session="s1", subagent_type="researcher", description=f"d{i}", prompt="p",
                batch_id="batch-A",
            )
            for i in range(3)
        ]
        s.mark_complete(ids[0], "completed", "r0")
        s.mark_complete(ids[1], "failed", "boom")  # a failed member is still a settled member
        # ids[2] left running
        counts = s.batch_status_counts("batch-A")
        assert counts == {"completed": 1, "failed": 1, "running": 1}
        assert s.batch_status_counts("nope") == {}

    def test_batch_id_migrates_in_place(self, tmp_path):
        """A pre-#1766 DB (no batch_id column) upgrades in place on open — existing rows
        read batch_id None and new rows can carry one."""
        import sqlite3

        db_path = tmp_path / "background" / "jobs.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db_path))
        con.execute(
            """
            CREATE TABLE background_jobs (
                id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, origin_session TEXT NOT NULL,
                subagent_type TEXT NOT NULL, description TEXT NOT NULL, prompt TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, notified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, completed_at TEXT
            )
            """
        )
        con.execute(
            "INSERT INTO background_jobs (id, agent_name, origin_session, subagent_type, description, prompt, "
            "status, result, notified, created_at, completed_at) "
            "VALUES ('bg-old', 'a', 's1', 'researcher', 'legacy', 'p', 'completed', 'r', 0, 't1', 't2')"
        )
        con.commit()
        con.close()

        s = BackgroundStore(str(db_path))  # opening runs the guarded migration
        old = s.get("bg-old")
        assert old is not None and old.batch_id is None  # legacy row migrated, unbatched
        new = s.create(
            agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p",
            batch_id="batch-A",
        )
        assert s.get(new).batch_id == "batch-A"
        assert s.batch_size("batch-A") == 1


# ── manager ──────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Captures the POST and returns a canned response (stubs httpx.AsyncClient)."""

    captured: dict = {}

    def __init__(self, response, raise_exc=None, **_kw):
        self._response = response
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.captured = {"url": url, "headers": headers, "json": json}
        if self._raise:
            raise self._raise
        return self._response


def _manager(tmp_path: Path, **kw) -> BackgroundManager:
    return BackgroundManager(
        agent_name="a",
        invoke_url="http://127.0.0.1:7870",
        store=_store(tmp_path),
        api_key="k",
        bearer_token="b",
        **kw,
    )


async def _drain_fire_tasks(mgr: BackgroundManager) -> None:
    """Let the detached fire task run to completion."""
    for _ in range(50):
        if not mgr._fire_tasks:
            return
        await asyncio.sleep(0.01)


class TestManager:
    async def test_spawn_returns_immediately_and_registers(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        mgr = _manager(tmp_path)
        jid = await mgr.spawn(
            origin_session="s1",
            subagent_type="researcher",
            description="research X",
            prompt="do the thing",
        )
        # registered as running immediately (terminal hook would settle it later)
        assert mgr.store.get(jid).status == "running"
        await _drain_fire_tasks(mgr)
        # a 200 must NOT mark it failed — the (here absent) terminal hook owns completion
        assert mgr.store.get(jid).status == "running"

    async def test_fire_posts_a2a_shape(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        mgr = _manager(tmp_path)
        jid = await mgr.spawn(
            origin_session="s1",
            subagent_type="researcher",
            description="d",
            prompt="p",
        )
        await _drain_fire_tasks(mgr)
        cap = _FakeClient.captured
        assert cap["url"] == "http://127.0.0.1:7870/a2a"
        assert cap["headers"]["A2A-Version"] == "1.0"
        assert cap["headers"]["Authorization"] == "Bearer b"
        body = cap["json"]
        assert body["method"] == "SendMessage"
        msg = body["params"]["message"]
        assert msg["role"] == "ROLE_USER"
        assert msg["contextId"] == f"background:{jid}"
        assert msg["metadata"]["origin"] == "background"
        assert msg["metadata"]["trigger"] == jid
        # A registry subagent's detached run carries its tool ALLOWLIST (#1639) —
        # the chat entry stamps it on the turn's state and SubagentFenceMiddleware
        # enforces it, closing the "role guidance is the only guard" gap.
        from graph.subagents.config import SUBAGENT_REGISTRY

        assert msg["metadata"]["subagent_fence"] == list(SUBAGENT_REGISTRY["researcher"].tools)

    async def test_fire_carries_no_fence_for_unknown_types(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        mgr = _manager(tmp_path)
        await mgr.spawn(
            origin_session="s1",
            subagent_type="totally-custom-role",
            description="d",
            prompt="p",
        )
        await _drain_fire_tasks(mgr)
        assert "subagent_fence" not in _FakeClient.captured["json"]["params"]["message"]["metadata"]

    async def test_spawn_publishes_started_event(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        events: list = []
        mgr = _manager(tmp_path, event_publish=lambda topic, data: events.append((topic, data)))
        jid = await mgr.spawn(
            origin_session="s1",
            subagent_type="researcher",
            description="dig",
            prompt="p",
        )
        await _drain_fire_tasks(mgr)
        started = [d for (t, d) in events if t == "background.started"]
        assert len(started) == 1
        assert started[0]["job_id"] == jid
        assert started[0]["origin_session"] == "s1"
        assert started[0]["description"] == "dig"

    async def test_http_error_marks_failed(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: _FakeClient(_FakeResponse(500, "boom")),
        )
        mgr = _manager(tmp_path)
        jid = await mgr.spawn(
            origin_session="s1",
            subagent_type="researcher",
            description="d",
            prompt="p",
        )
        await _drain_fire_tasks(mgr)
        assert mgr.store.get(jid).status == "failed"

    async def test_network_exception_marks_failed(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: _FakeClient(None, raise_exc=RuntimeError("conn refused")),
        )
        mgr = _manager(tmp_path)
        jid = await mgr.spawn(
            origin_session="s1",
            subagent_type="researcher",
            description="d",
            prompt="p",
        )
        await _drain_fire_tasks(mgr)
        assert mgr.store.get(jid).status == "failed"

    async def test_fire_respects_concurrency_cap(self, tmp_path, monkeypatch):
        """A fan-out of background jobs runs at most ``max_concurrency`` turns at once —
        the semaphore gates the self-POST, which holds its slot for the whole turn. Without
        the cap all N would POST concurrently and hammer the gateway."""
        import httpx

        live = {"n": 0, "peak": 0}

        class _SlowClient:
            def __init__(self, **_kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def post(self, url, headers=None, json=None):
                live["n"] += 1
                live["peak"] = max(live["peak"], live["n"])
                await asyncio.sleep(0.03)  # stand in for a whole turn running server-side
                live["n"] -= 1
                return _FakeResponse(200)

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _SlowClient(**kw))
        mgr = _manager(tmp_path, max_concurrency=2)
        for i in range(6):
            await mgr.spawn(origin_session="s", subagent_type="researcher", description=f"d{i}", prompt="p")
        # Drain all six fires (longer budget than the default helper — 6 jobs / cap 2).
        for _ in range(400):
            if not mgr._fire_tasks:
                break
            await asyncio.sleep(0.01)
        assert not mgr._fire_tasks, "fires did not all complete"
        assert 1 <= live["peak"] <= 2, f"peak concurrency {live['peak']} should be capped at 2"

    async def test_default_concurrency_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKGROUND_MAX_CONCURRENCY", "7")
        assert _manager(tmp_path)._max_concurrency == 7
        monkeypatch.setenv("BACKGROUND_MAX_CONCURRENCY", "bogus")
        assert _manager(tmp_path)._max_concurrency == 3  # default on a bad value


# ── #1767: turn-lifecycle events around the push-resume self-POST ─────────────


def _resume_job(status: str = "completed") -> "BackgroundJob":  # noqa: F821 — imported at call sites
    from background.store import BackgroundJob

    return BackgroundJob(
        id="bg-xyz",
        agent_name="a",
        origin_session="sess-1",
        subagent_type="researcher",
        description="dig the archives",
        prompt="p",
        status=status,
        result="found it",
        notified=False,
        created_at="t1",
        completed_at="t2",
    )


class TestResumeOriginTurnEvents:
    """A push-resume nudge (ADR 0070) holds the connection open for the WHOLE
    origin-session turn; #1767 wraps it in ``turn.started`` / ``turn.finished`` so an
    open console can render its typing indicator during that otherwise-invisible turn."""

    async def test_resume_origin_emits_started_then_finished(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        events: list = []
        mgr = _manager(tmp_path, event_publish=lambda topic, data: events.append((topic, data)))

        ok = await mgr.resume_origin(_resume_job())

        assert ok is True
        turn_events = [(t, d) for (t, d) in events if t.startswith("turn.")]
        assert [t for (t, _) in turn_events] == ["turn.started", "turn.finished"]
        for _t, d in turn_events:
            assert d["session_id"] == "sess-1"
            assert d["origin"] == "background-resume"
            assert d["trigger"] == "bg-xyz"
        # turn.finished carries the outcome so the console clears an accurate state.
        assert turn_events[-1][1]["ok"] is True

    async def test_resume_origin_finishes_even_on_delivery_failure(self, tmp_path, monkeypatch):
        """A failed nudge must still emit ``turn.finished`` — a hanging ``turn.started``
        would spin the console's indicator forever."""
        import httpx

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: _FakeClient(None, raise_exc=RuntimeError("conn refused")),
        )
        events: list = []
        mgr = _manager(tmp_path, event_publish=lambda topic, data: events.append((topic, data)))

        ok = await mgr.resume_origin(_resume_job(status="failed"))

        assert ok is False  # push-resume is best-effort; the report still drains next turn
        topics = [t for (t, _) in events if t.startswith("turn.")]
        assert topics == ["turn.started", "turn.finished"]
        finished = next(d for (t, d) in events if t == "turn.finished")
        assert finished["ok"] is False


# ── #1766: fan-out batch-join (coalesce N completions into one push-resume) ──


class TestBatchJoin:
    """A fan-out of background jobs sharing one ``batch_id`` push-resumes ONCE — held
    until the last member settles — instead of N drip-fed briefing turns (#1766)."""

    def _member(self, mgr, batch_id, origin="s1", desc="d"):
        return mgr.store.create(
            agent_name="a", origin_session=origin, subagent_type="researcher",
            description=desc, prompt="p", batch_id=batch_id,
        )

    def _patch_send(self, mgr) -> list:
        """Replace the network self-A2A send with an in-memory recorder (no network)."""
        sends: list[dict] = []

        async def _fake_send(*, context_id, text, metadata):
            sends.append({"context_id": context_id, "text": text, "metadata": metadata})

        mgr._send_a2a_message = _fake_send
        return sends

    async def _settle(self, mgr, job_id, status="completed", result="r"):
        """Mirror the server flow: mark the row terminal, then route the fresh row through
        the batch-aware terminal delivery."""
        mgr.store.mark_complete(job_id, status, result)
        return await mgr.resume_for_terminal(mgr.store.get(job_id))

    async def test_batch_of_three_fires_exactly_one_join(self, tmp_path):
        mgr = _manager(tmp_path, batch_join_timeout_s=1000)
        sends = self._patch_send(mgr)
        a = self._member(mgr, "batch-A", desc="topic a")
        b = self._member(mgr, "batch-A", desc="topic b")
        c = self._member(mgr, "batch-A", desc="topic c")
        # the first two settles HOLD — nothing delivered, not a failure
        assert await self._settle(mgr, a) is None
        assert await self._settle(mgr, b) is None
        assert sends == []
        # the last member fires ONE coalesced batch nudge for the whole fan-out
        assert await self._settle(mgr, c) is True
        assert len(sends) == 1
        nudge = sends[0]
        assert nudge["context_id"] == "s1"
        assert nudge["metadata"]["background_batch_id"] == "batch-A"
        assert nudge["metadata"]["origin"] == "background-resume"
        assert "synthesize ONE briefing" in nudge["text"]
        assert "all 3 background jobs" in nudge["text"]

    async def test_already_joined_batch_never_double_fires(self, tmp_path):
        mgr = _manager(tmp_path, batch_join_timeout_s=1000)
        sends = self._patch_send(mgr)
        a = self._member(mgr, "batch-A")
        b = self._member(mgr, "batch-A")
        assert await self._settle(mgr, a) is None
        assert await self._settle(mgr, b) is True  # last → fires exactly once
        assert len(sends) == 1
        # a redundant delivery for an already-joined batch delivers nothing (no double-fire)
        assert await mgr.resume_for_terminal(mgr.store.get(b)) is None
        assert len(sends) == 1

    async def test_singleton_none_batch_uses_resume_origin(self, tmp_path):
        mgr = _manager(tmp_path, batch_join_timeout_s=1000)
        sends = self._patch_send(mgr)
        jid = mgr.store.create(  # batch_id defaults to None → singleton
            agent_name="a", origin_session="s1", subagent_type="researcher", description="lone", prompt="p"
        )
        assert await self._settle(mgr, jid) is True
        assert len(sends) == 1
        # single-job text + single-job metadata (no batch key) — the UNCHANGED path
        assert "background job bg-" in sends[0]["text"]
        assert "background_batch_id" not in sends[0]["metadata"]
        assert sends[0]["metadata"]["background_job_id"] == jid

    async def test_batch_of_one_is_a_singleton(self, tmp_path):
        mgr = _manager(tmp_path, batch_join_timeout_s=1000)
        sends = self._patch_send(mgr)
        jid = self._member(mgr, "batch-solo")
        assert await self._settle(mgr, jid) is True  # batch_size 1 → singleton path
        assert len(sends) == 1
        assert "background_batch_id" not in sends[0]["metadata"]

    async def test_failed_member_counts_as_settled_and_in_summary(self, tmp_path):
        mgr = _manager(tmp_path, batch_join_timeout_s=1000)
        sends = self._patch_send(mgr)
        a = self._member(mgr, "batch-A")
        b = self._member(mgr, "batch-A")
        assert await self._settle(mgr, a, status="failed", result="boom") is None
        assert await self._settle(mgr, b, status="completed", result="ok") is True
        assert len(sends) == 1
        text = sends[0]["text"]
        assert "completed 1" in text and "failed 1" in text
        assert "all 2 background jobs" in text

    async def test_straggler_timeout_forces_partial_join(self, tmp_path):
        mgr = _manager(tmp_path, batch_join_timeout_s=0.05)  # tiny straggler window
        sends = self._patch_send(mgr)
        a = self._member(mgr, "batch-A")
        b = self._member(mgr, "batch-A")  # left running — the straggler
        assert await self._settle(mgr, a) is None  # holds + arms the timeout
        assert sends == []
        for _ in range(200):  # let the straggler timeout fire the partial join
            if sends:
                break
            await asyncio.sleep(0.01)
        assert len(sends) == 1
        text = sends[0]["text"]
        assert "1 of 2 background jobs" in text
        assert "still running" in text
        assert "batch-A" in mgr._joined_batches
        # the hung member finishing later must NOT fire a second nudge
        assert await self._settle(mgr, b) is None
        assert len(sends) == 1


# ── Phase 2: autonomous idle-wake (server/a2a.py) ────────────────────────────


class TestPhase2Wake:
    def _job(self, status="completed"):
        from background.store import BackgroundJob

        return BackgroundJob(
            id="bg-abc",
            agent_name="a",
            origin_session="sess-X",
            subagent_type="strategist",
            description="audit fleet",
            prompt="p",
            status=status,
            result="Tuned buy_buffer to 30k.",
            notified=False,
            created_at="t1",
            completed_at="t2",
        )

    def test_wake_enabled_default_and_optout(self, monkeypatch):
        from server.a2a import _background_wake_enabled

        monkeypatch.delenv("BACKGROUND_WAKE", raising=False)
        assert _background_wake_enabled() is True
        monkeypatch.setenv("BACKGROUND_WAKE", "0")
        assert _background_wake_enabled() is False
        monkeypatch.setenv("BACKGROUND_WAKE", "1")
        assert _background_wake_enabled() is True

    def test_wake_text_includes_job_and_result(self):
        from server.a2a import _background_wake_text

        t = _background_wake_text(self._job("completed"))
        assert "audit fleet" in t and "strategist" in t
        assert "finished" in t and "sess-X" in t
        assert "Tuned buy_buffer" in t

    def test_wake_text_failed_verb(self):
        from server.a2a import _background_wake_text

        assert "failed" in _background_wake_text(self._job("failed"))

    async def test_wake_adds_now_inbox_item(self, monkeypatch):
        import operator_api.console_handlers as ch
        import server.a2a as a2a
        from runtime.state import STATE

        monkeypatch.setattr(STATE, "inbox_store", object(), raising=False)
        captured: dict = {}

        async def fake_add(payload):
            captured.update(payload)
            return {"ok": True, "fired": True}

        monkeypatch.setattr(ch, "_operator_inbox_add", fake_add)
        fired = await a2a._background_wake(self._job())
        assert fired is True
        assert captured["priority"] == "now"
        assert captured["source"] == "background"
        assert captured["dedup_key"] == "background-wake:bg-abc"
        assert "audit fleet" in captured["text"]

    async def test_wake_noop_without_inbox(self, monkeypatch):
        import server.a2a as a2a
        from runtime.state import STATE

        monkeypatch.setattr(STATE, "inbox_store", None, raising=False)
        assert await a2a._background_wake(self._job()) is False


def test_task_tool_constrains_subagent_type_to_enum():
    """The `task` tool's subagent_type must render as a JSON-schema enum of the live
    registry (ADR 0050 follow-up) so the model can't pass a name that doesn't exist."""
    from graph.agent import _build_task_tools
    from graph.config import LangGraphConfig
    from graph.subagents.config import SUBAGENT_REGISTRY

    tools = _build_task_tools(LangGraphConfig(), [])
    task = next(t for t in tools if t.name == "task")
    st = task.args_schema.model_json_schema()["properties"]["subagent_type"]
    assert st.get("enum") == list(SUBAGENT_REGISTRY.keys())
    assert "run_in_background" in task.args_schema.model_json_schema()["properties"]


def test_fired_prompt_includes_task_and_prompt():
    out = _build_fired_prompt("researcher", "research ships", "find all ship types")
    assert "research ships" in out
    assert "find all ship types" in out
    assert "background" in out.lower()


# ── Phase 4 / realtime: a2a_task_id, cancel, progress hook (ADR 0051) ─────────


class TestStorePhase4:
    def test_set_a2a_task_id_only_fills_blank(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        assert s.get(jid).a2a_task_id == ""
        s.set_a2a_task_id(jid, "task-1")
        assert s.get(jid).a2a_task_id == "task-1"
        s.set_a2a_task_id(jid, "task-2")  # must not clobber
        assert s.get(jid).a2a_task_id == "task-1"

    def test_canceled_is_terminal_and_drains(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        assert s.mark_complete(jid, "canceled", "stopped") is True
        assert s.get(jid).status == "canceled"
        assert [j.status for j in s.drain_pending("s1")] == ["canceled"]


class TestManagerCancel:
    async def test_cancel_posts_canceltask_and_settles(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p"
        )
        mgr.store.set_a2a_task_id(jid, "task-xyz")
        res = await mgr.cancel(jid)
        assert res["ok"] is True and res["status"] == "canceled"
        cap = _FakeClient.captured
        assert cap["json"]["method"] == "CancelTask"
        assert cap["json"]["params"]["id"] == "task-xyz"
        assert mgr.store.get(jid).status == "canceled"

    async def test_cancel_without_task_id_marks_canceled(self, tmp_path):
        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p"
        )
        res = await mgr.cancel(jid)  # no a2a_task_id captured yet
        assert res["status"] == "canceled"
        assert mgr.store.get(jid).status == "canceled"

    async def test_cancel_noop_on_terminal_job(self, tmp_path):
        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p"
        )
        mgr.store.mark_complete(jid, "completed", "done")
        res = await mgr.cancel(jid)
        assert res["ok"] is False and res["status"] == "completed"


class TestProgressHook:
    def test_turn_started_records_task_id_no_publish(self, tmp_path, monkeypatch):
        import server.a2a as a2a
        from runtime.state import STATE

        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p"
        )
        monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
        published: list = []
        monkeypatch.setattr(a2a._event_bus, "publish", lambda t, d=None: published.append((t, d)))
        a2a._a2a_progress(f"background:{jid}", "task-42", {"phase": "turn_started"})
        assert mgr.store.get(jid).a2a_task_id == "task-42"
        assert published == []  # turn_started records, doesn't publish

    def test_tool_frame_publishes_progress(self, tmp_path, monkeypatch):
        import server.a2a as a2a
        from runtime.state import STATE

        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p"
        )
        monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
        published: list = []
        monkeypatch.setattr(a2a._event_bus, "publish", lambda t, d=None: published.append((t, d)))
        a2a._a2a_progress(f"background:{jid}", "task-42", {"phase": "tool_start", "id": "tc1", "name": "web_search"})
        assert len(published) == 1
        topic, data = published[0]
        assert topic == "background.progress"
        assert data["job_id"] == jid and data["tool"] == "web_search" and data["phase"] == "tool_start"

    def test_non_background_context_ignored(self, monkeypatch):
        import server.a2a as a2a

        published: list = []
        monkeypatch.setattr(a2a._event_bus, "publish", lambda t, d=None: published.append((t, d)))
        a2a._a2a_progress("some-chat-session", "task-1", {"phase": "tool_start", "name": "x"})
        assert published == []


# ── drain into the spawning chat turn (server/chat.py) ───────────────────────


class TestDrainIntoChat:
    def test_drain_renders_task_notification(self, tmp_path, monkeypatch):
        from runtime.state import STATE
        from server.chat import _drain_background_messages

        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a",
            origin_session="sess-X",
            subagent_type="researcher",
            description="research ships",
            prompt="p",
        )
        mgr.store.mark_complete(jid, "completed", "Ships: A, B, C")
        monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)

        msgs = _drain_background_messages("sess-X")
        assert len(msgs) == 1
        body = msgs[0].content
        assert "<task-notification>" in body
        assert jid in body
        assert "research ships" in body
        assert "<status>completed</status>" in body
        assert "Ships: A, B, C" in body
        # exactly-once: a second drain yields nothing
        assert _drain_background_messages("sess-X") == []

    def test_drain_noop_without_manager(self, monkeypatch):
        from runtime.state import STATE
        from server.chat import _drain_background_messages

        monkeypatch.setattr(STATE, "background_mgr", None, raising=False)
        assert _drain_background_messages("any") == []

    def test_drain_truncates_huge_result(self, tmp_path, monkeypatch):
        from runtime.state import STATE
        from server.chat import _BG_RESULT_CAP, _drain_background_messages

        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a",
            origin_session="sess-Y",
            subagent_type="researcher",
            description="d",
            prompt="p",
        )
        mgr.store.mark_complete(jid, "completed", "x" * (_BG_RESULT_CAP + 5000))
        monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
        body = _drain_background_messages("sess-Y")[0].content
        assert "truncated to" in body
        assert len(body) < _BG_RESULT_CAP + 2000


# ── the `task` tool captures the spawning session (bd-3v0) ────────────────────
# A chat-originated `task(run_in_background=True)` must stamp the turn's
# session_id as origin_session, or the completion can never drain back to the
# spawning chat (server.chat._drain_background_messages matches origin_session).
# Regression for the contextvar-empty-in-tool-body class — origin_session was
# read from tracing.current_session_id(), which is empty in a tool body; it now
# comes from injected graph state. Drives a REAL graph so a monkeypatch can't
# mask it.


class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class _RecordingBG:
    def __init__(self):
        self.seen_origin = "__unset__"
        self.seen_incognito = None
        self.seen_batch_id = "__unset__"

    async def spawn(
        self, *, origin_session, subagent_type, description, prompt, origin_incognito=False, batch_id=None
    ):
        self.seen_origin = origin_session
        self.seen_incognito = origin_incognito
        self.seen_batch_id = batch_id
        return "bg-test-1"


@pytest.mark.asyncio
async def test_background_task_stamps_the_turn_session_as_origin(monkeypatch):
    from unittest.mock import patch

    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import MemorySaver

    from graph.config import LangGraphConfig

    task_call = AIMessage(
        id="turn-single-1",  # #1766: the emitting turn's id becomes the job's batch_id
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {
                    "description": "deep dive",
                    "prompt": "go research",
                    "subagent_type": "researcher",
                    "run_in_background": True,
                },
                "id": "c1",
                "type": "tool_call",
            }
        ],
    )
    fake = _ToolFake(messages=iter([task_call, AIMessage(content="started, carrying on")]))
    bg = _RecordingBG()
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        graph = create_agent_graph(
            LangGraphConfig(),
            include_subagents=True,
            background_mgr=bg,
            checkpointer=MemorySaver(),
        )
    await graph.ainvoke(
        {"messages": [HumanMessage("kick off background research")], "session_id": "sess-BG"},
        config={"configurable": {"thread_id": "t1"}},
    )
    assert bg.seen_origin == "sess-BG"
    # A lone task carries the emitting turn's id as its batch_id (#1766) — the store then
    # sees batch_size 1 → the unchanged singleton push-resume.
    assert bg.seen_batch_id == "turn-single-1"


# ── task_batch(run_in_background=True) stamps the session + spawns every spec ──
# The batch background path reads origin_session from injected graph state (same
# contextvar-empty-in-tool-body class as the single task). Drive a REAL graph so a
# monkeypatch can't mask it.


class _RecordingBatchBG:
    def __init__(self):
        self.spawns: list[dict] = []

    async def spawn(
        self, *, origin_session, subagent_type, description, prompt, origin_incognito=False, batch_id=None
    ):
        self.spawns.append(
            {
                "origin": origin_session,
                "subagent_type": subagent_type,
                "description": description,
                "batch_id": batch_id,
            }
        )
        return f"bg-{len(self.spawns)}"


@pytest.mark.asyncio
async def test_background_batch_stamps_session_and_spawns_all(monkeypatch):
    from unittest.mock import patch

    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import MemorySaver

    from graph.config import LangGraphConfig

    batch_call = AIMessage(
        id="turn-batch-1",  # #1766: shared by every spec's job as the fan-out batch key
        content="",
        tool_calls=[
            {
                "name": "task_batch",
                "args": {
                    "tasks": [
                        {"description": "topic a", "prompt": "research a"},
                        {"description": "topic b", "prompt": "research b", "subagent_type": "researcher"},
                    ],
                    "run_in_background": True,
                },
                "id": "c1",
                "type": "tool_call",
            }
        ],
    )
    fake = _ToolFake(messages=iter([batch_call, AIMessage(content="all three kicked off, moving on")]))
    bg = _RecordingBatchBG()
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        graph = create_agent_graph(
            LangGraphConfig(),
            include_subagents=True,
            background_mgr=bg,
            checkpointer=MemorySaver(),
        )
    await graph.ainvoke(
        {"messages": [HumanMessage("research these in the background")], "session_id": "sess-BB"},
        config={"configurable": {"thread_id": "t1"}},
    )
    assert len(bg.spawns) == 2
    assert {s["origin"] for s in bg.spawns} == {"sess-BB"}
    assert {s["description"] for s in bg.spawns} == {"topic a", "topic b"}
    # Every spec's job shares ONE batch_id — the emitting turn's id (#1766) — so their
    # completions coalesce into a single push-resume.
    assert {s["batch_id"] for s in bg.spawns} == {"turn-batch-1"}
