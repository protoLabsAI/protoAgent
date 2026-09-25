"""Console ↔ Zed chat hand-off: the store (``runtime/editor_handoff.py``), its routes
(``operator_api/editor_routes.py``), ``open_in_editor`` offering the calling chat, and the
per-session busy signal (``GET /api/chat/sessions/{id}`` → ``active``)."""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import runtime.state as rs
import tools.fs_tools as fs
from graph.config import LangGraphConfig
from runtime import editor_handoff as eh
from runtime import turn_activity


@pytest.fixture(autouse=True)
def _clean_store():
    eh.clear()
    yield
    eh.clear()


# ── the store ─────────────────────────────────────────────────────────────────


def test_match_rule_root_inside_parent(tmp_path):
    root = tmp_path / "dev" / "nava" / "repo"
    (root / "src").mkdir(parents=True)
    r = str(root)
    assert eh.matches(r, r)  # the root itself
    assert eh.matches(r, str(root / "src"))  # inside it
    assert eh.matches(r, str(tmp_path / "dev" / "nava"))  # a parent (Zed on ~/dev/nava)
    assert not eh.matches(r, str(tmp_path / "dev" / "other"))  # a sibling
    assert not eh.matches(r, str(tmp_path / "dev" / "nava" / "repo-2"))  # a prefix is not a parent
    assert not eh.matches(r, "")  # no cwd → only an "any" offer
    assert eh.matches(None, "")  # "any" matches everything
    assert eh.matches(None, "/anywhere")


def test_match_follows_symlinked_cwd(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    link = tmp_path / "link"
    link.symlink_to(root)
    assert eh.matches(str(root.resolve()), str(link))


def test_claim_is_one_shot(tmp_path):
    eh.offer("chat-1", root=str(tmp_path))
    first = eh.claim(str(tmp_path))
    assert first and first.session_id == "chat-1"
    assert eh.claim(str(tmp_path)) is None


def test_ttl_expires_offers(tmp_path):
    eh.offer("chat-1", root=str(tmp_path), now=1000.0)
    assert eh.claim(str(tmp_path), now=1000.0 + eh.TTL_SECONDS + 0.1) is None
    eh.offer("chat-2", root=str(tmp_path), now=2000.0)
    got = eh.claim(str(tmp_path), now=2000.0 + eh.TTL_SECONDS - 1)
    assert got and got.session_id == "chat-2"


def test_latest_write_per_root_wins(tmp_path):
    eh.offer("chat-old", root=str(tmp_path), now=100.0)
    eh.offer("chat-new", root=str(tmp_path), now=101.0)
    got = eh.claim(str(tmp_path), now=102.0)
    assert got and got.session_id == "chat-new"
    assert eh.claim(str(tmp_path), now=102.0) is None  # the older one was replaced, not queued


def test_most_recent_match_across_roots(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    eh.offer("chat-a", root=str(a), now=100.0)
    eh.offer("chat-any", root=None, now=101.0)
    eh.offer("chat-b", root=str(b), now=102.0)
    # cwd = the common parent matches all three; the newest wins, the rest stay.
    assert eh.claim(str(tmp_path), now=103.0).session_id == "chat-b"
    # cwd inside a: a and "any" match; "any" is newer.
    assert eh.claim(str(a / "x"), now=103.0).session_id == "chat-any"
    assert eh.claim(str(a), now=103.0).session_id == "chat-a"
    assert eh.claim(str(a), now=103.0) is None


# ── routes ────────────────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "work" / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "router.py").write_text("a\nb\n")
    (tmp_path / "work" / "elsewhere").mkdir()
    return root


def _app(monkeypatch, repo: Path, engine=None) -> TestClient:
    from operator_api.chat_routes import register_chat_routes
    from operator_api.editor_routes import register_editor_routes

    cfg = LangGraphConfig(filesystem_projects=[{"name": "repo", "path": str(repo)}])
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    monkeypatch.setattr(rs.STATE, "a2a_task_engine", engine, raising=False)
    app = FastAPI()
    register_editor_routes(app)
    register_chat_routes(app, "full")
    return TestClient(app)


def _engine_with(tmp_path, *context_ids: str):
    from a2a.server.tasks.database_task_store import Base, TaskModel
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/handoff-tasks.db")

    async def _seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if context_ids:
                await conn.execute(
                    TaskModel.__table__.insert(),
                    [
                        {
                            "id": f"t-{i}",
                            "context_id": cid,
                            "kind": "task",
                            "status": {"state": "TASK_STATE_COMPLETED"},
                            "artifacts": [],
                            "history": [],
                            "last_updated": datetime(2026, 9, 24, 12, i, tzinfo=timezone.utc),
                        }
                        for i, cid in enumerate(context_ids)
                    ],
                )

    asyncio.run(_seed())
    return engine


def test_offer_then_claim_round_trip(monkeypatch, repo, tmp_path):
    client = _app(monkeypatch, repo, _engine_with(tmp_path, "chat-1"))
    r = client.post(
        "/api/editor/handoff",
        json={"session_id": "chat-1", "project": "repo", "path": "./src//router.py", "line": 2, "title": "Router bug"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root"] == str(repo.resolve())
    assert body["id"].startswith("ho-") and body["expires_at"]
    # Zed opened on the PARENT folder still claims it.
    c = client.post("/api/editor/handoff/claim", json={"cwd": str(repo.parent)})
    assert c.status_code == 200
    assert c.json() == {"session_id": "chat-1", "project": "repo", "path": "src/router.py", "line": 2, "title": "Router bug"}
    # One-shot.
    assert client.post("/api/editor/handoff/claim", json={"cwd": str(repo)}).status_code == 204


def test_claim_miss_is_204(monkeypatch, repo, tmp_path):
    client = _app(monkeypatch, repo)
    client.post("/api/editor/handoff", json={"session_id": "chat-1", "project": "repo"})
    r = client.post("/api/editor/handoff/claim", json={"cwd": str(repo.parent / "elsewhere")})
    assert r.status_code == 204 and r.content == b""


def test_offer_without_project_matches_any_cwd(monkeypatch, repo):
    client = _app(monkeypatch, repo)
    r = client.post("/api/editor/handoff", json={"session_id": "chat-9"})
    assert r.status_code == 200 and r.json()["root"] is None
    got = client.post("/api/editor/handoff/claim", json={"cwd": "/some/other/place"})
    assert got.status_code == 200 and got.json()["session_id"] == "chat-9"


def test_unknown_session_is_404(monkeypatch, repo, tmp_path):
    client = _app(monkeypatch, repo, _engine_with(tmp_path, "chat-known"))
    r = client.post("/api/editor/handoff", json={"session_id": "chat-nope", "project": "repo"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"
    assert eh.pending() == []


def test_project_outside_the_fence_is_refused(monkeypatch, repo):
    client = _app(monkeypatch, repo)
    r = client.post("/api/editor/handoff", json={"session_id": "chat-1", "project": "elsewhere"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "unknown_project"
    r = client.post("/api/editor/handoff", json={"session_id": "chat-1", "project": "repo", "path": "../elsewhere"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_path"
    assert eh.pending() == []


def test_bad_bodies(monkeypatch, repo):
    client = _app(monkeypatch, repo)
    assert client.post("/api/editor/handoff", json={}).status_code == 400
    assert client.post("/api/editor/handoff", json={"session_id": "c", "path": "src"}).status_code == 400
    assert client.post("/api/editor/handoff", json={"session_id": "c", "line": 0}).status_code == 400
    assert client.post("/api/editor/handoff", json={"session_id": "c", "line": True}).status_code == 400


# ── busy signal ───────────────────────────────────────────────────────────────


def test_turn_activity_counts_per_session():
    turn_activity.begin("chat-a")
    turn_activity.begin("chat-a")
    assert turn_activity.is_active("chat-a") and not turn_activity.is_active("chat-b")
    turn_activity.end("chat-a")
    assert turn_activity.is_active("chat-a")  # the other driver still runs
    turn_activity.end("chat-a")
    turn_activity.end("chat-a")  # an extra end never underflows
    assert not turn_activity.is_active("chat-a")


def test_session_route_reports_active_and_404(monkeypatch, repo, tmp_path):
    server_chat = importlib.import_module("server.chat")  # `server.chat` the attr is a function

    client = _app(monkeypatch, repo, _engine_with(tmp_path, "chat-1"))
    body = client.get("/api/chat/sessions/chat-1").json()
    assert body["active"] is False and body["turn_count"] == 1
    assert body["last_state"] == "TASK_STATE_COMPLETED"
    server_chat._turn_started("chat-1")
    try:
        assert client.get("/api/chat/sessions/chat-1").json()["active"] is True
    finally:
        server_chat._turn_ended("chat-1")
    assert client.get("/api/chat/sessions/chat-1").json()["active"] is False
    r = client.get("/api/chat/sessions/chat-unknown")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    # A brand-new session with a turn in flight but nothing stored yet is known (and busy).
    server_chat._turn_started("chat-new")
    try:
        assert client.get("/api/chat/sessions/chat-new").json()["active"] is True
    finally:
        server_chat._turn_ended("chat-new")


@pytest.mark.asyncio
async def test_stream_driver_marks_the_session_busy(monkeypatch):
    """The streaming turn driver (A2A + console) brackets the WHOLE generator."""
    server_chat = importlib.import_module("server.chat")  # `server.chat` the attr is a function

    seen: list[bool] = []

    async def _impl(message, session_id, **_kw):
        seen.append(turn_activity.is_active(session_id))
        yield ("text", "hi")
        seen.append(turn_activity.is_active(session_id))

    monkeypatch.setattr(server_chat, "_chat_langgraph_stream_impl", _impl)
    monkeypatch.setattr(server_chat, "_note_agent_active", lambda _sid: None)
    async for _ in server_chat._chat_langgraph_stream("hello", "chat-busy"):
        pass
    assert seen == [True, True]
    assert not turn_activity.is_active("chat-busy")


# ── open_in_editor offers the calling chat ────────────────────────────────────


@dataclass
class _Cfg:
    filesystem_enabled: bool = True
    filesystem_allow_run: bool = False
    filesystem_run_requires_approval: bool = True
    filesystem_bypass_allowed: bool = True
    filesystem_editor_command: str = "zed"
    filesystem_editor_handoff: bool = True
    filesystem_projects: list = field(default_factory=list)
    tools_memoize_reads_enabled: bool = False
    identity_name: str = "navaEngineer"


class _FakePopen:
    def __init__(self, argv, **kwargs):
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def fake_launch(monkeypatch):
    monkeypatch.setattr(fs.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(fs.subprocess, "Popen", _FakePopen)


def _open_tool(repo: Path, **kw):
    cfg = _Cfg(filesystem_projects=[{"name": "repo", "path": str(repo)}], **kw)
    return {t.name: t for t in fs.build_fs_tools(cfg)}["open_in_editor"]


def test_open_in_editor_offers_the_injected_session(repo, fake_launch, monkeypatch):
    from langchain_core.messages import HumanMessage

    from observability import tracing

    monkeypatch.setattr(tracing, "current_session_id", lambda: "")  # empty in a tool body
    out = _open_tool(repo).invoke(
        {
            "project": "repo",
            "path": "src/router.py",
            "line": 2,
            "state": {"session_id": "chat-123", "messages": [HumanMessage("Find the router bug please")]},
        }
    )
    assert out.startswith("Opened repo/src/router.py:2 in zed.")
    assert "navaEngineer thread in Zed's agent panel within 2 minutes, it continues this chat" in out
    got = eh.claim(str(repo / "src"))
    assert got is not None
    assert (got.session_id, got.project, got.path, got.line) == ("chat-123", "repo", "src/router.py", 2)
    assert got.title == "Find the router bug please"


def test_open_in_editor_handoff_can_be_disabled(repo, fake_launch):
    out = _open_tool(repo, filesystem_editor_handoff=False).invoke(
        {"project": "repo", "path": "src/router.py", "state": {"session_id": "chat-123"}}
    )
    assert out == "Opened repo/src/router.py in zed."
    assert eh.pending() == []


def test_open_in_editor_without_a_session_offers_nothing(repo, fake_launch, monkeypatch):
    from observability import tracing

    monkeypatch.setattr(tracing, "current_session_id", lambda: "")
    out = _open_tool(repo).invoke({"project": "repo", "path": "src/router.py"})
    assert out == "Opened repo/src/router.py in zed."
    assert eh.pending() == []


def test_config_default_and_parse():
    assert LangGraphConfig().filesystem_editor_handoff is True
    assert LangGraphConfig.from_dict({"filesystem": {"editor_handoff": False}}).filesystem_editor_handoff is False


@pytest.mark.asyncio
async def test_open_in_editor_in_a_real_graph_offers_the_turn_session(repo, monkeypatch):
    """Drive a REAL create_agent graph: the session reaches the tool via InjectedState
    (ProtoAgentState), not the contextvar — the path a monkeypatched resolver would hide."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import MemorySaver

    from observability import tracing

    class _ToolFake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    monkeypatch.setattr(tracing, "current_session_id", lambda: "")
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "open_in_editor",
                "args": {"project": "repo", "path": "src/router.py", "line": 1},
                "id": "c1",
                "type": "tool_call",
            }
        ],
    )
    fake = _ToolFake(messages=iter([call, AIMessage(content="done")]))
    cfg = LangGraphConfig(
        filesystem_editor_command="zed", filesystem_projects=[{"name": "repo", "path": str(repo)}]
    )
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        graph = create_agent_graph(cfg, include_subagents=False, checkpointer=MemorySaver())
    # Stub the launch only AFTER the build — the graph build itself shells out.
    monkeypatch.setattr(fs.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(fs.subprocess, "Popen", _FakePopen)
    await graph.ainvoke(
        {"messages": [HumanMessage("open the router")], "session_id": "chat-graph-1"},
        config={"configurable": {"thread_id": "t-handoff"}},
    )
    got = eh.claim(str(repo))
    assert got is not None and got.session_id == "chat-graph-1"
