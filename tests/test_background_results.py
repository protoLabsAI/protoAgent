"""ADR 0070 — background results: push-resume, indexed reports, disposable workers.

Covers the completion-moment behaviors added on top of ADR 0050's registry/drain:

- D1 push-resume: exactly one nudge into the ORIGIN session on completion, none
  when disabled / canceled / background-origin / incognito; delivery failure falls
  back to the Phase-2 wake and leaves ``notified`` untouched (drain still delivers).
- D2 report indexing: substantial completed results land in the knowledge store
  keyed to the origin session (source_type="background_report", trust tier 2);
  small / failed / incognito results don't; the drain notification carries a
  pointer to the searchable full text once truncated.
- D3 disposable workers: no session-summary persistence for ``background:*``
  sessions, legacy worker files are filtered out of the digest, and retirement
  harvest skips worker threads.
- Incognito propagation onto the job row (``origin_incognito`` column + migration).
- The ``GET /api/background/{job_id}`` by-id route.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from background.manager import BackgroundManager
from background.store import BackgroundStore

# ── helpers (mirroring tests/test_background.py) ─────────────────────────────


def _store(tmp_path: Path) -> BackgroundStore:
    return BackgroundStore(str(tmp_path / "background" / "jobs.db"))


def _manager(tmp_path: Path, **kw) -> BackgroundManager:
    return BackgroundManager(
        agent_name="a",
        invoke_url="http://127.0.0.1:7870",
        store=_store(tmp_path),
        api_key="k",
        bearer_token="b",
        **kw,
    )


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


async def _settle_bg_tasks() -> None:
    """Let the terminal hook's fire-and-forget resume/index/wake tasks finish."""
    import server.a2a as a2a

    for _ in range(200):
        if not a2a._BG_WAKE_TASKS:
            return
        await asyncio.sleep(0.01)


def _outcome(job_id: str, state: str = "completed", text: str = "the report"):
    from a2a_impl.executor import TurnOutcome

    return TurnOutcome(
        task_id="t1",
        context_id=f"background:{job_id}",
        state=state,
        text=text,
        origin="background",
        trigger=job_id,
    )


@pytest.fixture
def hook_env(monkeypatch):
    """Neutral STATE + bus for exercising ``_handle_background_terminal`` directly."""
    import server.a2a as a2a
    from graph.config import LangGraphConfig
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(), raising=False)
    monkeypatch.setattr(STATE, "knowledge_store", None, raising=False)
    monkeypatch.delenv("BACKGROUND_WAKE", raising=False)
    published: list = []
    monkeypatch.setattr(a2a._event_bus, "publish", lambda t, d=None: published.append((t, d)))
    return published


# ── incognito column + migration ─────────────────────────────────────────────


class TestStoreIncognito:
    def test_create_default_not_incognito(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p")
        job = s.get(jid)
        assert job.origin_incognito is False
        assert job.to_dict()["origin_incognito"] is False

    def test_create_incognito_roundtrips(self, tmp_path):
        s = _store(tmp_path)
        jid = s.create(
            agent_name="a",
            origin_session="s1",
            subagent_type="researcher",
            description="d",
            prompt="p",
            origin_incognito=True,
        )
        assert s.get(jid).origin_incognito is True
        assert s.get(jid).to_dict()["origin_incognito"] is True

    def test_migration_adds_column_to_legacy_db(self, tmp_path):
        """A pre-ADR-0070 DB (no origin_incognito column) migrates in place; legacy
        rows read origin_incognito=False."""
        db_path = tmp_path / "jobs.db"
        db = sqlite3.connect(str(db_path))
        db.execute(
            """
            CREATE TABLE background_jobs (
                id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, origin_session TEXT NOT NULL,
                subagent_type TEXT NOT NULL, description TEXT NOT NULL, prompt TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, notified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, completed_at TEXT, a2a_task_id TEXT
            )
            """
        )
        db.execute(
            "INSERT INTO background_jobs (id, agent_name, origin_session, subagent_type, description, "
            "prompt, status, result, notified, created_at, completed_at, a2a_task_id) "
            "VALUES ('bg-aaaaaaaaaaaa', 'a', 's1', 'researcher', 'd', 'p', 'running', '', 0, 't', NULL, '')"
        )
        db.commit()
        db.close()

        s = BackgroundStore(str(db_path))
        legacy = s.get("bg-aaaaaaaaaaaa")
        assert legacy is not None and legacy.origin_incognito is False
        # new inserts carry the flag
        jid = s.create(
            agent_name="a", origin_session="s2", subagent_type="researcher", description="d", prompt="p",
            origin_incognito=True,
        )
        assert s.get(jid).origin_incognito is True

    def test_migration_is_idempotent(self, tmp_path):
        path = str(tmp_path / "jobs.db")
        s1 = BackgroundStore(path)
        jid = s1.create(
            agent_name="a", origin_session="s1", subagent_type="researcher", description="d", prompt="p",
            origin_incognito=True,
        )
        s2 = BackgroundStore(path)  # re-init on an already-migrated DB must not error
        assert s2.get(jid).origin_incognito is True


# ── D1: the manager's push-resume nudge ──────────────────────────────────────


class TestResumeOrigin:
    async def test_posts_nudge_into_origin_session(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a", origin_session="chat-42", subagent_type="researcher", description="dig", prompt="p"
        )
        mgr.store.mark_complete(jid, "completed", "found it")
        assert await mgr.resume_origin(mgr.store.get(jid)) is True
        cap = _FakeClient.captured
        assert cap["url"] == "http://127.0.0.1:7870/a2a"
        msg = cap["json"]["params"]["message"]
        assert msg["contextId"] == "chat-42"  # the ORIGIN session, not a background context
        assert msg["metadata"]["origin"] == "background-resume"  # NOT "background" — no terminal-hook loop
        assert msg["metadata"]["trigger"] == jid
        text = msg["parts"][0]["text"]
        assert jid in text and "dig" in text and "brief the operator" in text and "finished" in text

    async def test_failed_job_nudge_says_failed(self, tmp_path, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a", origin_session="chat-42", subagent_type="researcher", description="d", prompt="p"
        )
        mgr.store.mark_complete(jid, "failed", "boom")
        assert await mgr.resume_origin(mgr.store.get(jid)) is True
        assert "failed" in _FakeClient.captured["json"]["params"]["message"]["parts"][0]["text"]

    async def test_delivery_failure_never_raises_and_leaves_row(self, tmp_path, monkeypatch):
        """A failed nudge changes NOTHING: status stays terminal, notified stays 0 —
        the report still drains exactly-once on the session's next manual turn."""
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(500, "down")))
        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a", origin_session="chat-42", subagent_type="researcher", description="d", prompt="p"
        )
        mgr.store.mark_complete(jid, "completed", "the answer")
        assert await mgr.resume_origin(mgr.store.get(jid)) is False
        job = mgr.store.get(jid)
        assert job.status == "completed" and job.result == "the answer" and job.notified is False
        assert [j.id for j in mgr.store.drain_pending("chat-42")] == [jid]
        assert mgr.store.drain_pending("chat-42") == []  # still exactly-once


# ── D1: the terminal hook's resume/wake routing ──────────────────────────────


class TestTerminalHookResume:
    def _wire(self, tmp_path, monkeypatch, *, origin="chat-42", incognito=False):
        import server.a2a as a2a
        from runtime.state import STATE

        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a",
            origin_session=origin,
            subagent_type="researcher",
            description="dig",
            prompt="p",
            origin_incognito=incognito,
        )
        monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
        resumes: list = []

        async def fake_resume(job):
            resumes.append(job.id)
            return True

        monkeypatch.setattr(mgr, "resume_origin", fake_resume)
        wakes: list = []
        monkeypatch.setattr(a2a, "_spawn_background_wake", lambda job: wakes.append(job.id))
        return a2a, mgr, jid, resumes, wakes

    async def test_completion_fires_exactly_one_nudge_and_no_wake(self, tmp_path, monkeypatch, hook_env):
        a2a, mgr, jid, resumes, wakes = self._wire(tmp_path, monkeypatch)
        a2a._handle_background_terminal(_outcome(jid))
        await _settle_bg_tasks()
        assert resumes == [jid]
        assert wakes == []  # the resume turn IS the reaction — never both

    async def test_drain_still_exactly_once_after_nudge(self, tmp_path, monkeypatch, hook_env):
        a2a, mgr, jid, resumes, wakes = self._wire(tmp_path, monkeypatch)
        a2a._handle_background_terminal(_outcome(jid))
        await _settle_bg_tasks()
        # the nudge itself never flips notified — the origin session's (nudge) turn drains it once
        assert [j.id for j in mgr.store.drain_pending("chat-42")] == [jid]
        assert mgr.store.drain_pending("chat-42") == []

    async def test_disabled_config_restores_wake(self, tmp_path, monkeypatch, hook_env):
        from graph.config import LangGraphConfig
        from runtime.state import STATE

        a2a, mgr, jid, resumes, wakes = self._wire(tmp_path, monkeypatch)
        monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(background_auto_resume=False), raising=False)
        a2a._handle_background_terminal(_outcome(jid))
        await _settle_bg_tasks()
        assert resumes == []
        assert wakes == [jid]

    async def test_canceled_job_never_resumes(self, tmp_path, monkeypatch, hook_env):
        a2a, mgr, jid, resumes, wakes = self._wire(tmp_path, monkeypatch)
        a2a._handle_background_terminal(_outcome(jid, state="canceled"))
        await _settle_bg_tasks()
        assert resumes == []
        assert wakes == [jid]  # pre-ADR-0070 wake behavior preserved

    async def test_background_origin_never_resumes(self, tmp_path, monkeypatch, hook_env):
        """No resume chains: a job spawned FROM a background turn must not nudge the
        worker context (which would spiral turn-on-turn)."""
        a2a, mgr, jid, resumes, wakes = self._wire(tmp_path, monkeypatch, origin="background:bg-parentparent")
        a2a._handle_background_terminal(_outcome(jid))
        await _settle_bg_tasks()
        assert resumes == []
        assert wakes == [jid]

    async def test_incognito_origin_never_resumes(self, tmp_path, monkeypatch, hook_env):
        a2a, mgr, jid, resumes, wakes = self._wire(tmp_path, monkeypatch, incognito=True)
        a2a._handle_background_terminal(_outcome(jid))
        await _settle_bg_tasks()
        assert resumes == []

    async def test_failed_delivery_falls_back_to_wake(self, tmp_path, monkeypatch, hook_env):
        import server.a2a as a2a_mod
        from runtime.state import STATE

        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a", origin_session="chat-42", subagent_type="researcher", description="d", prompt="p"
        )
        monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)

        async def failing_resume(job):
            return False

        monkeypatch.setattr(mgr, "resume_origin", failing_resume)
        woke: list = []

        async def fake_wake(job):
            woke.append(job.id)
            return True

        monkeypatch.setattr(a2a_mod, "_background_wake", fake_wake)
        a2a_mod._handle_background_terminal(_outcome(jid))
        await _settle_bg_tasks()
        assert woke == [jid]


# ── D1: the nudge turn is autonomous (no operator on the wire) ───────────────


class _HitlTurnStream:
    """Stand-in for ``_run_turn_stream`` (mirrors tests/test_hitl_forms.py): yields a
    HITL interrupt on the first pass, then an answer once resumed."""

    def __init__(self):
        self.resume_values: list = []

    def __call__(self, message, session_id, config, *, resume_value=None, **_kw):
        self.resume_values.append(resume_value)
        first = len(self.resume_values) == 1

        async def _gen():
            if first:
                yield ("input_required", {"question": "Should I dig deeper?"})
            else:
                yield ("__raw__", "Briefing delivered.")

        return _gen()


class TestResumeTurnIsAutonomous:
    async def test_nudge_turn_auto_answers_hitl_instead_of_parking(self, monkeypatch):
        """The push-resume nudge is server-fired — the manager discards the A2A
        response, so nobody can answer a HITL pause. origin="background-resume"
        must ride the autonomous auto-answer path (like scheduler/background), or a
        briefing turn that asks a question parks its task in input-required forever."""
        import importlib

        from runtime.state import STATE

        chat_mod = importlib.import_module("server.chat")
        monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
        fake = _HitlTurnStream()
        monkeypatch.setattr(chat_mod, "_run_turn_stream", fake)

        frames = [
            frame
            async for frame in chat_mod._run_native_turn(
                "[background job bg-abcabcabcabc (dig) finished — brief the operator]",
                "chat-42",
                {"configurable": {"thread_id": "a2a:chat-42"}},
                request_metadata={"origin": "background-resume"},
            )
        ]
        kinds = [k for k, _ in frames]
        assert "input_required" not in kinds  # never parks
        assert ("done", "Briefing delivered.") in frames
        assert chat_mod._AUTONOMOUS_HITL_SENTINEL in fake.resume_values


# ── D2: report indexing ──────────────────────────────────────────────────────


class _FakeKnowledgeStore:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def add_document(self, content, **kwargs):
        self.calls.append((content, kwargs))
        return [1, 2]


class TestReportIndexing:
    def _wire(self, tmp_path, monkeypatch, *, incognito=False, origin="chat-42"):
        import server.a2a as a2a
        from graph.config import LangGraphConfig
        from runtime.state import STATE

        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a",
            origin_session=origin,
            subagent_type="researcher",
            description="dig",
            prompt="p",
            origin_incognito=incognito,
        )
        monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
        # pull-only so the resume path stays out of these assertions
        monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(background_auto_resume=False), raising=False)
        monkeypatch.setattr(a2a, "_spawn_background_wake", lambda job: None)
        fake = _FakeKnowledgeStore()
        monkeypatch.setattr(STATE, "knowledge_store", fake, raising=False)
        return a2a, jid, fake

    async def test_substantial_result_indexed_with_provenance(self, tmp_path, monkeypatch, hook_env):
        a2a, jid, fake = self._wire(tmp_path, monkeypatch)
        report = "finding line\n" * 100  # > 800 chars
        a2a._handle_background_terminal(_outcome(jid, text=report))
        await _settle_bg_tasks()
        assert len(fake.calls) == 1
        content, kwargs = fake.calls[0]
        assert content == report.strip() or content == report  # full report, not the preview
        assert kwargs["source"] == "chat-42"  # keyed to the ORIGIN session
        assert kwargs["source_type"] == "background_report"
        assert kwargs["heading"] == f"Background report: dig ({jid})"

    async def test_small_result_not_indexed(self, tmp_path, monkeypatch, hook_env):
        a2a, jid, fake = self._wire(tmp_path, monkeypatch)
        a2a._handle_background_terminal(_outcome(jid, text="short answer"))
        await _settle_bg_tasks()
        assert fake.calls == []

    async def test_failed_job_not_indexed(self, tmp_path, monkeypatch, hook_env):
        a2a, jid, fake = self._wire(tmp_path, monkeypatch)
        a2a._handle_background_terminal(_outcome(jid, state="failed", text="x" * 2000))
        await _settle_bg_tasks()
        assert fake.calls == []

    async def test_incognito_job_not_indexed(self, tmp_path, monkeypatch, hook_env):
        a2a, jid, fake = self._wire(tmp_path, monkeypatch, incognito=True)
        a2a._handle_background_terminal(_outcome(jid, text="x" * 2000))
        await _settle_bg_tasks()
        assert fake.calls == []

    async def test_chained_background_origin_job_not_indexed(self, tmp_path, monkeypatch, hook_env):
        """A job spawned FROM another background worker's turn is never indexed:
        its origin is a disposable worker identity (D3), its content flows into the
        parent's own report, and the worker turn runs non-incognito — indexing here
        would leak an incognito root's report into the KB transitively."""
        a2a, jid, fake = self._wire(tmp_path, monkeypatch, origin="background:bg-parentparent")
        a2a._handle_background_terminal(_outcome(jid, text="x" * 2000))
        await _settle_bg_tasks()
        assert fake.calls == []

    def test_background_report_is_agent_trust_tier(self):
        from knowledge.trust import trust_label, trust_tier

        assert trust_tier("background_report") == 2
        assert trust_label("background_report") == "agent"


# ── D2: the drain notification's pointer line ────────────────────────────────


class TestDrainPointer:
    def _drained_body(
        self, tmp_path, monkeypatch, *, result: str, incognito=False, status="completed", origin="sess-P"
    ) -> str:
        from runtime.state import STATE
        from server.chat import _drain_background_messages

        mgr = _manager(tmp_path)
        monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
        jid = mgr.store.create(
            agent_name="a",
            origin_session=origin,
            subagent_type="researcher",
            description="d",
            prompt="p",
            origin_incognito=incognito,
        )
        mgr.store.mark_complete(jid, status, result)
        msgs = _drain_background_messages(origin)
        assert len(msgs) == 1
        return msgs[0].content

    def test_truncated_notification_points_at_memory_recall(self, tmp_path, monkeypatch):
        from server.chat import _BG_RESULT_CAP

        assert _BG_RESULT_CAP == 3000  # ADR 0070 D2 shrank the inline cap
        body = self._drained_body(tmp_path, monkeypatch, result="x" * (_BG_RESULT_CAP + 500))
        assert "memory_recall" in body
        assert "report card" in body
        assert "job id bg-" in body

    def test_small_result_carries_no_pointer(self, tmp_path, monkeypatch):
        body = self._drained_body(tmp_path, monkeypatch, result="tiny result")
        assert "memory_recall" not in body
        assert "tiny result" in body

    def test_incognito_truncation_does_not_claim_searchability(self, tmp_path, monkeypatch):
        """An incognito job's report is NOT indexed — the notification must not
        point the model at a memory_recall hit that doesn't exist."""
        from server.chat import _BG_RESULT_CAP

        body = self._drained_body(tmp_path, monkeypatch, result="x" * (_BG_RESULT_CAP + 500), incognito=True)
        assert "memory_recall" not in body
        assert "report card" in body  # jobs.db still has the full text

    def test_chained_job_truncation_does_not_claim_searchability(self, tmp_path, monkeypatch):
        """A background-origin (chained) job's report is NOT indexed — mirror of the
        indexing guard, so the notification never points at a hit that doesn't exist."""
        from server.chat import _BG_RESULT_CAP

        body = self._drained_body(
            tmp_path, monkeypatch, result="x" * (_BG_RESULT_CAP + 500), origin="background:bg-parentparent"
        )
        assert "memory_recall" not in body
        assert "report card" in body


# ── D3: disposable workers ───────────────────────────────────────────────────


class TestDisposableWorkers:
    def test_persist_session_skips_background_sessions(self, tmp_path, monkeypatch):
        from langchain_core.messages import AIMessage, HumanMessage

        from graph.middleware.memory import _persist_session

        monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
        msgs = [HumanMessage(content="do the research"), AIMessage(content="the full report")]
        _persist_session({"session_id": "background:bg-abcabcabcabc", "messages": msgs}, "trace-1")
        assert list(tmp_path.iterdir()) == []  # nothing written
        # control: an ordinary chat session still persists
        _persist_session({"session_id": "chat-77", "messages": msgs}, "trace-1")
        assert (tmp_path / "chat-77.json").exists()

    def test_digest_loader_skips_legacy_background_files(self, tmp_path):
        from graph.middleware.memory import load_prior_sessions

        summary = {
            "session_id": "chat-1",
            "messages": [{"role": "user", "content": "plan the sprint"}],
            "timestamp": "2026-07-01T00:00:00+00:00",
        }
        (tmp_path / "chat-1.json").write_text(json.dumps(summary))
        legacy = dict(summary, session_id="background:bg-abcabcabcabc")
        legacy["messages"] = [{"role": "user", "content": "SECRET WORKER REPORT"}]
        (tmp_path / "background:bg-abcabcabcabc.json").write_text(json.dumps(legacy))

        block = load_prior_sessions(memory_dir=str(tmp_path))
        assert "chat-1" in block
        assert "background:bg-abcabcabcabc" not in block
        assert "SECRET WORKER REPORT" not in block

    async def test_harvest_skips_background_threads(self):
        from graph.conversation_harvest import harvest_thread

        class _RecordingCheckpointer:
            calls: list = []

            async def aget_tuple(self, cfg):
                self.calls.append(cfg)
                return None

        cp = _RecordingCheckpointer()
        for tid in ("a2a:background:bg-abcabcabcabc", "background:bg-abcabcabcabc"):
            out = await harvest_thread(tid, checkpointer=cp, knowledge_store=object(), config=None)
            assert out is None
        assert cp.calls == []  # early-returned before touching the checkpointer


# ── incognito propagation from the task tool ─────────────────────────────────


class TestIncognitoPropagation:
    def test_incognito_from_state(self):
        from graph.agent import _incognito_from

        assert _incognito_from({"incognito": True}) is True
        assert _incognito_from({"incognito": False}) is False
        assert _incognito_from({}) is False
        assert _incognito_from(None) is False

    async def test_task_tool_stamps_incognito_on_the_job(self, tmp_path, monkeypatch):
        import httpx

        from graph.agent import _build_task_tools
        from graph.config import LangGraphConfig

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(_FakeResponse(200)))
        mgr = _manager(tmp_path)
        tools = _build_task_tools(LangGraphConfig(), [], background_mgr=mgr)
        task = next(t for t in tools if t.name == "task")
        await task.ainvoke(
            {
                "name": "task",
                "type": "tool_call",
                "id": "tc1",
                "args": {
                    "description": "dig",
                    "prompt": "p",
                    "subagent_type": "researcher",
                    "run_in_background": True,
                    "state": {"session_id": "chat-9", "incognito": True},
                },
            }
        )
        jobs = mgr.store.list()
        assert len(jobs) == 1
        assert jobs[0].origin_session == "chat-9"
        assert jobs[0].origin_incognito is True


# ── the by-id route ──────────────────────────────────────────────────────────


class TestBackgroundByIdRoute:
    def _client(self, monkeypatch, mgr):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from operator_api.routes import register_operator_routes
        from runtime.state import STATE

        app = FastAPI()

        async def _run(_payload):
            return ""

        register_operator_routes(
            app,
            runtime_status=lambda: {},
            subagent_list=lambda: [],
            subagent_run=_run,
            subagent_batch=_run,
        )
        monkeypatch.setattr(STATE, "background_mgr", mgr, raising=False)
        return TestClient(app)

    def test_happy_path_returns_full_row(self, tmp_path, monkeypatch):
        mgr = _manager(tmp_path)
        jid = mgr.store.create(
            agent_name="a", origin_session="chat-42", subagent_type="researcher", description="dig", prompt="p"
        )
        mgr.store.mark_complete(jid, "completed", "the FULL report " * 500)
        client = self._client(monkeypatch, mgr)
        r = client.get(f"/api/background/{jid}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == jid
        assert body["origin_session"] == "chat-42"
        assert body["result"].startswith("the FULL report")  # full text, not a preview
        assert body["origin_incognito"] is False

    def test_unknown_id_is_404(self, tmp_path, monkeypatch):
        client = self._client(monkeypatch, _manager(tmp_path))
        assert client.get("/api/background/bg-abcdefabcdef").status_code == 404

    @pytest.mark.parametrize("bad", ["bg-XYZ", "notajob", "bg-abc", "bg-ABCDEFABCDEF", "bg-abcdefabcdef0"])
    def test_malformed_id_is_400(self, tmp_path, monkeypatch, bad):
        client = self._client(monkeypatch, _manager(tmp_path))
        assert client.get(f"/api/background/{bad}").status_code == 400

    def test_disabled_manager_is_404(self, monkeypatch):
        client = self._client(monkeypatch, None)
        assert client.get("/api/background/bg-abcdefabcdef").status_code == 404
