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

from background.manager import BackgroundManager, _build_fired_prompt
from background.store import BackgroundStore


def _store(tmp_path: Path) -> BackgroundStore:
    return BackgroundStore(str(tmp_path / "background" / "jobs.db"))


# ── store ────────────────────────────────────────────────────────────────────


class TestStore:
    def test_create_is_running(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(
            agent_name="a", origin_session="s1", subagent_type="researcher",
            description="dig", prompt="go",
        )
        assert jid.startswith("bg-")
        job = s.get(jid)
        assert job is not None
        assert job.status == "running"
        assert job.notified is False
        assert job.origin_session == "s1"

    def test_no_drain_while_running(self, tmp_path):
        s = _store(tmp_path)
        s.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                 description="d", prompt="p")
        assert s.drain_pending("s1") == []

    def test_mark_complete_is_idempotent(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                       description="d", prompt="p")
        assert s.mark_complete(jid, "completed", "the answer") is True
        # a second (e.g. delivery-failure) write must NOT clobber the real result
        assert s.mark_complete(jid, "failed", "nope") is False
        assert s.get(jid).status == "completed"
        assert s.get(jid).result == "the answer"

    def test_drain_is_exactly_once(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                       description="d", prompt="p")
        s.mark_complete(jid, "completed", "result text")
        first = s.drain_pending("s1")
        assert [j.id for j in first] == [jid]
        assert first[0].result == "result text"
        # drained once → never again
        assert s.drain_pending("s1") == []

    def test_drain_is_session_scoped(self, tmp_path):
        s = _store(tmp_path)
        a = s.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                     description="d", prompt="p")
        b = s.create(agent_name="a", origin_session="s2", subagent_type="researcher",
                     description="d", prompt="p")
        s.mark_complete(a, "completed", "ra")
        s.mark_complete(b, "completed", "rb")
        assert [j.id for j in s.drain_pending("s1")] == [a]
        assert [j.id for j in s.drain_pending("s2")] == [b]

    def test_failed_jobs_drain_too(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                       description="d", prompt="p")
        s.mark_complete(jid, "failed", "boom")
        drained = s.drain_pending("s1")
        assert [(j.id, j.status) for j in drained] == [(jid, "failed")]

    def test_reconcile_fails_running_jobs(self, tmp_path):
        s = _store(tmp_path)
        running = s.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                           description="d", prompt="p")
        done = s.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                        description="d2", prompt="p2")
        s.mark_complete(done, "completed", "ok")
        assert s.reconcile_interrupted() == 1  # only the running one
        assert s.get(running).status == "failed"
        assert s.get(done).status == "completed"

    def test_list_filters(self, tmp_path):
        s = _store(tmp_path)
        a = s.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                     description="d", prompt="p")
        s.create(agent_name="a", origin_session="s2", subagent_type="researcher",
                 description="d", prompt="p")
        s.mark_complete(a, "completed", "x")
        assert {j.id for j in s.list(origin_session="s1")} == {a}
        assert {j.status for j in s.list(status="completed")} == {"completed"}
        assert len(s.list()) == 2


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
            origin_session="s1", subagent_type="researcher",
            description="research X", prompt="do the thing",
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
            origin_session="s1", subagent_type="researcher", description="d", prompt="p",
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

    async def test_spawn_publishes_started_event(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        events: list = []
        mgr = _manager(tmp_path, event_publish=lambda topic, data: events.append((topic, data)))
        jid = await mgr.spawn(
            origin_session="s1", subagent_type="researcher", description="dig", prompt="p",
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
            httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(500, "boom")),
        )
        mgr = _manager(tmp_path)
        jid = await mgr.spawn(
            origin_session="s1", subagent_type="researcher", description="d", prompt="p",
        )
        await _drain_fire_tasks(mgr)
        assert mgr.store.get(jid).status == "failed"

    async def test_network_exception_marks_failed(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: _FakeClient(None, raise_exc=RuntimeError("conn refused")),
        )
        mgr = _manager(tmp_path)
        jid = await mgr.spawn(
            origin_session="s1", subagent_type="researcher", description="d", prompt="p",
        )
        await _drain_fire_tasks(mgr)
        assert mgr.store.get(jid).status == "failed"


# ── Phase 2: autonomous idle-wake (server/a2a.py) ────────────────────────────


class TestPhase2Wake:
    def _job(self, status="completed"):
        from background.store import BackgroundJob

        return BackgroundJob(
            id="bg-abc", agent_name="a", origin_session="sess-X",
            subagent_type="strategist", description="audit fleet", prompt="p",
            status=status, result="Tuned buy_buffer to 30k.", notified=False,
            created_at="t1", completed_at="t2",
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
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                       description="d", prompt="p")
        assert s.get(jid).a2a_task_id == ""
        s.set_a2a_task_id(jid, "task-1")
        assert s.get(jid).a2a_task_id == "task-1"
        s.set_a2a_task_id(jid, "task-2")  # must not clobber
        assert s.get(jid).a2a_task_id == "task-1"

    def test_canceled_is_terminal_and_drains(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                       description="d", prompt="p")
        assert s.mark_complete(jid, "canceled", "stopped") is True
        assert s.get(jid).status == "canceled"
        assert [j.status for j in s.drain_pending("s1")] == ["canceled"]


class TestManagerCancel:
    async def test_cancel_posts_canceltask_and_settles(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        mgr = _manager(tmp_path)
        jid = mgr.store.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                               description="d", prompt="p")
        mgr.store.set_a2a_task_id(jid, "task-xyz")
        res = await mgr.cancel(jid)
        assert res["ok"] is True and res["status"] == "canceled"
        cap = _FakeClient.captured
        assert cap["json"]["method"] == "CancelTask"
        assert cap["json"]["params"]["id"] == "task-xyz"
        assert mgr.store.get(jid).status == "canceled"

    async def test_cancel_without_task_id_marks_canceled(self, tmp_path):
        mgr = _manager(tmp_path)
        jid = mgr.store.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                               description="d", prompt="p")
        res = await mgr.cancel(jid)  # no a2a_task_id captured yet
        assert res["status"] == "canceled"
        assert mgr.store.get(jid).status == "canceled"

    async def test_cancel_noop_on_terminal_job(self, tmp_path):
        mgr = _manager(tmp_path)
        jid = mgr.store.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                               description="d", prompt="p")
        mgr.store.mark_complete(jid, "completed", "done")
        res = await mgr.cancel(jid)
        assert res["ok"] is False and res["status"] == "completed"


class TestProgressHook:
    def test_turn_started_records_task_id_no_publish(self, tmp_path, monkeypatch):
        import server.a2a as a2a
        from runtime.state import STATE

        mgr = _manager(tmp_path)
        jid = mgr.store.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                               description="d", prompt="p")
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
        jid = mgr.store.create(agent_name="a", origin_session="s1", subagent_type="researcher",
                               description="d", prompt="p")
        monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
        published: list = []
        monkeypatch.setattr(a2a._event_bus, "publish", lambda t, d=None: published.append((t, d)))
        a2a._a2a_progress(f"background:{jid}", "task-42",
                          {"phase": "tool_start", "id": "tc1", "name": "web_search"})
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
            agent_name="a", origin_session="sess-X", subagent_type="researcher",
            description="research ships", prompt="p",
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
            agent_name="a", origin_session="sess-Y", subagent_type="researcher",
            description="d", prompt="p",
        )
        mgr.store.mark_complete(jid, "completed", "x" * (_BG_RESULT_CAP + 5000))
        monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
        body = _drain_background_messages("sess-Y")[0].content
        assert "truncated to" in body
        assert len(body) < _BG_RESULT_CAP + 2000
