"""Public memory stays available without private turns, under either flag value."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from graph import private_sessions
from graph.middleware.request_context import verified_principal_scope
from runtime.state import STATE
from tests.test_session_gist import harness as harness


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("principal", ["open/unknown", "fleet-service"])
async def test_public_memory_gist_search_and_recall(harness, monkeypatch, enabled, principal):
    from operator_api.chat_routes import _hidden_session, _hidden_session_ids
    from tools.memory_tools import _build_memory_tools

    monkeypatch.setattr(STATE, "a2a_task_engine", None)
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(private_steering_enabled=enabled))
    harness.config.private_steering_enabled = enabled
    with verified_principal_scope(principal):
        harness.turn(2)
        assert harness.card()["message_count"] == 4
        harness.timers[-1].fire()
        assert harness.card()["gist"].startswith("We are designing the idle session gist.")
        tools = {tool.name: tool for tool in _build_memory_tools(object())}
        found = await tools["session_search"].ainvoke({"query": "Design this feature"})
        recalled = await tools["recall_session"].ainvoke({"session_id": "chat-1"})
        assert "chat-1" in found
        assert "Design this feature" in recalled
        assert not private_sessions.withheld("chat-1", raise_unknown=True)
        assert not private_sessions.hidden_from_caller()("chat-1")
        context = SimpleNamespace(state={"principal_kind": principal})
        assert not await _hidden_session("chat-1", context)
        assert await _hidden_session_ids(context) == frozenset()


@pytest.mark.parametrize("store", ["absent", "unreadable", "marked"])
async def test_missing_store_is_distinct_from_private_or_unreadable(tmp_path, monkeypatch, store):
    from langchain_core.messages import HumanMessage
    from operator_api.chat_routes import _hidden_session, _hidden_session_ids

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(private_steering_enabled=True))
    monkeypatch.setattr(STATE, "a2a_task_engine", object() if store == "unreadable" else None)
    if store == "marked":
        private_sessions.mark("chat-1")
    context = SimpleNamespace(state={"principal_kind": "open/unknown"})
    with verified_principal_scope("open/unknown"):
        assert private_sessions.withheld("chat-1") is (store != "absent")
        assert private_sessions.hidden_from_caller()("chat-1") is (store != "absent")
        if store == "unreadable":
            with pytest.raises(private_sessions.OwnershipUnknown):
                private_sessions.withheld("chat-1", raise_unknown=True)
            for read in (_hidden_session("chat-1", context), _hidden_session_ids(context)):
                with pytest.raises(HTTPException) as error:
                    await read
                assert error.value.status_code == 503
        else:
            assert await _hidden_session("chat-1", context) is (store == "marked")
            if store == "absent":
                messages = [HumanMessage(content="public")]
                assert private_sessions.public_part(messages, "chat-1") == messages


@pytest.mark.parametrize("enabled", [False, True])
async def test_private_retry_and_concurrent_delivery(tmp_path, monkeypatch, delegation_env, enabled):
    import asyncio

    from a2a_impl import private_intake
    from graph import steering
    from tests.test_steer_idle_starts_turn import _Graph, _idle_snapshot, _operator_ctx, _partition, chat_mod

    engine, store = await _partition(tmp_path)
    monkeypatch.setattr(STATE, "graph", _Graph(_idle_snapshot()))
    monkeypatch.setattr(STATE, "a2a_task_engine", engine)
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(private_steering_enabled=enabled))
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def start(*args, private_ref, **kwargs):
        calls.append(private_ref)
        started.set()
        await release.wait()
        return "one-task"

    monkeypatch.setattr(chat_mod, "_start_steer_turn", start)
    context = _operator_ctx()

    async def steer():
        return await chat_mod.steer_or_start_turn(
            "chat-parity", "private text", "same-id", private=True, private_store=store, private_context=context,
        )

    steering._reset()
    try:
        first = asyncio.create_task(steer())
        await started.wait()
        duplicate = asyncio.create_task(steer())
        await asyncio.sleep(0)
        release.set()
        one, two = await asyncio.gather(first, duplicate)
        assert one["started_turn"] == two["started_turn"] == "one-task"
        # Scheduling and UI history can grow beyond the bounded timeline. Dedupe
        # must still find the original input after a process-local reset.
        def grow_history(records):
            for index in range(55):
                records[f"later-{index}"] = {"version": 1, "kind": "later"}

        await store.update_scheduling_payloads("chat-parity", context, grow_history)
        steering._reset()
        retry = await steer()
        assert retry["private_ref"] == one["private_ref"]
        assert retry["started_turn"] == "one-task"
        assert calls == [one["private_ref"]]
        inputs = [p for p in (await store.scheduling_payloads("chat-parity", context)).values()
                  if p.get("kind") == private_intake.PRIVATE_KIND_STEER]
        assert len(inputs) == 1
    finally:
        release.set()
        steering._reset()
        await engine.dispose()


@pytest.mark.parametrize("principal", ["open/unknown", "fleet-service", "shared-operator-bearer"])
def test_marked_memory_is_hidden_without_hiding_public_sessions(tmp_path, monkeypatch, principal):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from graph.config import LangGraphConfig
    from knowledge.history import HistoryStore
    from operator_api.memory_routes import register_memory_routes
    from tests.test_session_search import _write

    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    monkeypatch.setattr(STATE, "a2a_task_engine", None)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(private_steering_enabled=True))
    history = HistoryStore(tmp_path / "history.db")
    monkeypatch.setattr(STATE, "history_store", history)
    for sid in ("chat-public", "chat-private"):
        _write(tmp_path, sid, "needle")
        history.add("needle", kind="archive", session_id=sid, heading="Conversation")
    private_sessions.mark("chat-private")
    app = FastAPI()

    @app.middleware("http")
    async def caller(request, call_next):
        request.state.principal_kind = principal
        return await call_next(request)

    register_memory_routes(app)
    with TestClient(app) as client:
        for path, field in (("sessions", "sessions"), ("history/sessions", "sessions"),
                            ("history/search?q=needle", "results")):
            response = client.get(f"/api/memory/{path}")
            assert response.status_code == 200
            ids = {row["session_id"] for row in response.json()[field]}
            assert "chat-public" in ids
            assert ("chat-private" in ids) is (principal == "shared-operator-bearer")
