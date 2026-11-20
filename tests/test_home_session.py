"""The instance's Home conversation (ADR 0135 D9, #5545): a stable session id in instance
state, ``GET /api/home`` / ``POST /api/home/read``, behind the ``home_conversation`` flag."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.home_routes import register_home_routes
from runtime import home_session

_FLAG_ENV = "PROTOAGENT_FLAG_HOME_CONVERSATION"


@pytest.fixture(autouse=True)
def _hermetic_flags(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PROTOAGENT_FLAG_") or key == "PROTOAGENT_CHANNEL":
            monkeypatch.delenv(key, raising=False)


def _restart():
    """What a restart does to the store: the cached instance paths are dropped and the
    state is read from disk again (nothing about Home is held in memory)."""
    from infra.paths import reset_instance_paths

    reset_instance_paths()


def _client(**seams) -> TestClient:
    app = FastAPI()
    register_home_routes(app, **seams)
    return TestClient(app)


def test_home_id_is_stable_across_a_restart_and_is_an_operator_chat():
    first = home_session.home_session_id()
    _restart()
    assert home_session.home_session_id() == first
    assert home_session._path().is_file()
    assert not first.startswith(("system:", "job:"))
    assert first.startswith("chat-home-")


def test_home_id_passes_every_server_thread_exclusion():
    """Compaction, session gists and the memory harvest each skip server-run threads by
    prefix; Home must be on the kept side of all three, and be a console chat."""
    from a2a_impl.stores import _CONSOLE_CHAT_PREFIX
    from graph.conversation_harvest import facts_allowed
    from graph.middleware.compaction import _is_system_thread
    from runtime.session_ids import is_valid_session_id

    sid = home_session.home_session_id()
    assert is_valid_session_id(sid)
    assert sid.startswith(_CONSOLE_CHAT_PREFIX)
    assert not _is_system_thread({"session_id": sid})
    assert not sid.startswith(("background:", "job:", "palette-", "system:"))  # session_gist's set
    assert facts_allowed(f"a2a:{sid}", {"operator_chat_turn": True})


def test_unparseable_state_mints_a_new_id_but_an_unreadable_file_does_not(monkeypatch):
    first = home_session.home_session_id()
    path = home_session._path()

    def _denied(*_a, **_kw):
        raise PermissionError("locked")

    with monkeypatch.context() as patched:
        patched.setattr(type(path), "read_text", _denied)
        with pytest.raises(PermissionError):
            home_session.home_state()
    assert home_session.home_session_id() == first  # nothing was overwritten

    path.write_text("{not json", encoding="utf-8")
    second = home_session.home_session_id()
    assert second != first and second.startswith("chat-home-")
    assert home_session.home_session_id() == second


def test_flag_off_both_routes_404_and_mint_nothing():
    client = _client()
    assert client.get("/api/home").status_code == 404
    assert client.post("/api/home/read").status_code == 404
    assert not home_session._path().exists()


def test_flag_is_registered_off_by_default():
    from runtime import flags

    (flag,) = [f for f in flags.FLAGS if f.id == home_session.FLAG_ID]
    assert flag.tier == "off"
    assert not flags.flag_enabled(home_session.FLAG_ID, channel="dev")


def test_read_round_trips_last_read_at(monkeypatch):
    monkeypatch.setenv(_FLAG_ENV, "1")
    client = _client()
    before = client.get("/api/home")
    assert before.status_code == 200
    assert before.json() == {"session_id": home_session.home_session_id(), "last_read_at": None}

    marked = client.post("/api/home/read")
    assert marked.status_code == 200
    stamp = marked.json()["last_read_at"]
    assert datetime.fromisoformat(stamp).utcoffset().total_seconds() == 0

    _restart()
    assert client.get("/api/home").json() == {"session_id": before.json()["session_id"], "last_read_at": stamp}


def test_storage_failure_is_a_503_not_a_new_id(monkeypatch):
    monkeypatch.setenv(_FLAG_ENV, "1")

    def _boom():
        raise OSError("disk gone")

    monkeypatch.setattr(home_session, "home_state", _boom)
    assert _client().get("/api/home").status_code == 503


def test_session_listing_includes_home(monkeypatch, tmp_path):
    """Home is in ``GET /api/chat/sessions`` — the operator's chat list — before it has a
    turn, and asking for it again neither duplicates nor reopens it."""
    import operator_api.chat_routes as cr
    import runtime.state as rs
    from a2a.server.tasks.database_task_store import Base
    from server import session_registry
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/tasks.db")

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())
    monkeypatch.setenv(_FLAG_ENV, "1")
    monkeypatch.setattr(rs.STATE, "a2a_task_engine", engine, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    register_home_routes(
        app,
        read_session_list=session_registry.read_session_list,
        register_session=session_registry.register_session,
    )
    client = TestClient(app)

    sid = client.get("/api/home").json()["session_id"]
    rows = [s for s in client.get("/api/chat/sessions").json()["sessions"] if s["session_id"] == sid]
    assert len(rows) == 1 and rows[0]["title"] == "Home"

    # A Home the operator closed stays closed: finding it again is not a send.
    assert client.patch(f"/api/chat/sessions/{sid}", json={"closed": True}).status_code == 200
    assert client.get("/api/home").json()["session_id"] == sid
    listing = client.get("/api/chat/sessions").json()
    assert sid in listing["closed"]
    assert sid not in [s["session_id"] for s in listing["sessions"]]
