"""A redeemed plugin composer-form completes the A2A task it parked (#3470).

The form rides the input_required frame so the console renders the card, but the redeem
route never touched the task: the session's latest task read "waiting on the operator"
forever (the TTL sweep skips input_required) and a fleet roster parked the member on every
probe. The SDK's consumer treats input_required as the stream's final event, so the
executor cannot finish it — the redeem route settles it with a direct row update (the
SDK store scopes get/save by the request's owner, which the route does not have)."""

from __future__ import annotations

import pytest
from a2a.server.context import ServerCallContext
from a2a.types import a2a_pb2

from a2a_impl.executor import HITL_MIME, _data_part_proto, _text_part
from a2a_impl.stores import DatabaseTaskStore, make_sqlite_engine
from server import a2a as server_a2a


def _parked(tid: str, ctx: str, payload: dict) -> a2a_pb2.Task:
    msg = a2a_pb2.Message(role=a2a_pb2.ROLE_AGENT, message_id=f"m-{tid}", parts=[_text_part("Input required."), _data_part_proto(payload, HITL_MIME)])
    return a2a_pb2.Task(id=tid, context_id=ctx, status=a2a_pb2.TaskStatus(state=a2a_pb2.TASK_STATE_INPUT_REQUIRED, message=msg))


@pytest.mark.asyncio
async def test_a_redeemed_plugin_form_completes_its_parked_task_and_a_real_hitl_is_left_alone(tmp_path, monkeypatch):
    engine = make_sqlite_engine(str(tmp_path / "tasks.db"))
    store = DatabaseTaskStore(engine)
    await store.initialize()
    ctx = ServerCallContext()
    await store.save(_parked("form-1", "chat-form", {"kind": "form", "title": "Post?", "steps": [], "plugin_callback_id": "cb1"}), ctx)
    await store.save(_parked("hitl-1", "chat-hitl", {"kind": "approval", "title": "Approve?"}), ctx)
    await store.save(a2a_pb2.Task(id="work-1", context_id="chat-work", status=a2a_pb2.TaskStatus(state=a2a_pb2.TASK_STATE_WORKING)), ctx)
    monkeypatch.setattr(server_a2a.STATE, "a2a_task_engine", engine, raising=False)
    try:
        assert await server_a2a.settle_plugin_form_task("chat-form") is True
        done = await store.get("form-1", ctx)
        assert done.status.state == a2a_pb2.TASK_STATE_COMPLETED and done.status.timestamp.seconds > 0
        assert "cb1" in str(done.status.message)  # the card is still in the record for anyone replaying the turn
        assert await server_a2a.settle_plugin_form_task("chat-form") is False  # once
        assert await server_a2a.settle_plugin_form_task("chat-hitl") is False
        assert (await store.get("hitl-1", ctx)).status.state == a2a_pb2.TASK_STATE_INPUT_REQUIRED
        assert await server_a2a.settle_plugin_form_task("chat-work") is False
        assert (await store.get("work-1", ctx)).status.state == a2a_pb2.TASK_STATE_WORKING
        assert await server_a2a.settle_plugin_form_task("nowhere") is False and await server_a2a.settle_plugin_form_task("") is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_settle_is_a_no_op_without_the_durable_stores(monkeypatch):
    monkeypatch.setattr(server_a2a.STATE, "a2a_task_engine", None, raising=False)
    assert await server_a2a.settle_plugin_form_task("chat-x") is False


def test_the_callback_id_is_read_from_either_data_part_spelling():
    assert server_a2a._plugin_form_callback_in({"state": "TASK_STATE_INPUT_REQUIRED", "message": {"parts": [{"text": "x"}, {"data": {"plugin_callback_id": "cb9"}, "metadata": {"mimeType": HITL_MIME}}]}}) == "cb9"
    assert server_a2a._plugin_form_callback_in({"message": {"parts": [{"data": {"data": {"plugin_callback_id": "cb8"}}}]}}) == "cb8"
    assert server_a2a._plugin_form_callback_in({"message": {"parts": [{"data": {"kind": "approval"}}]}}) == ""
    assert server_a2a._plugin_form_callback_in("nope") == "" and server_a2a._plugin_form_callback_in({}) == ""
