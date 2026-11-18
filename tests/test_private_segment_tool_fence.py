"""Private tool calls use the real fence middleware, independently of feature flags."""
import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from a2a_impl.requester_delivery import RequesterProjection
from graph.fence_scope import current_fence, fence_scope, in_private_segment
from graph.middleware.subagent_fence import SubagentFenceMiddleware


@pytest.mark.parametrize("name,access,allowed", [
    ("read_file", None, True), ("list_dir", None, True),
    ("plugin_read", "read", True), ("run_command", None, False),
    ("write_file", None, False), ("fetch_url", "read", False),
    ("web_search", "read", False), ("remote__read", "read", False),
    ("schedule_task", None, False), ("task", None, False),
    ("task_batch", None, False), ("delegate_to", None, False),
])
@pytest.mark.asyncio
async def test_private_calls(name, access, allowed):
    ran = []

    @tool(name)
    def candidate() -> str:
        """Exercise an actual tool body."""
        ran.append(current_fence())
        return "ran"

    candidate.metadata = {"access": access}
    projection = RequesterProjection(None, None, None, {}, None)
    request = SimpleNamespace(tool=candidate, state={},
                              tool_call={"name": name, "args": {}, "id": "call", "type": "tool_call"})

    async def invoke(request):
        return await candidate.ainvoke(request.tool_call)

    with projection.tool_scope():
        projection.private_ref = "private"
        result = await SubagentFenceMiddleware().awrap_tool_call(request, invoke)
    assert bool(ran) is allowed
    if allowed:
        assert result.content == "ran"
        assert ran == [[name]]
    else:
        assert result.status == "error"
        assert result.content == f"Blocked by policy: tool '{name}' is blocked: this turn allows no tools — answer without calling any."
    assert current_fence() == []


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("fence", [[], ["read_file"], ["write_file"]])
def test_segment_intersection_and_scope_restoration(private, fence):
    projection = RequesterProjection(None, None, None, {}, None)
    request = SimpleNamespace(tool=SimpleNamespace(name="read_file"),
                              tool_call={"name": "read_file", "id": "call"},
                              state={"subagent_fence": fence})
    mw = SubagentFenceMiddleware()
    handler = lambda request: ToolMessage(content="ran", tool_call_id="call")
    ordinary = mw.wrap_tool_call(request, handler)
    with projection.tool_scope():
        public = mw.wrap_tool_call(request, handler)
        assert public == ordinary
        projection.private_ref = "private" if private else ""
        with fence_scope(["write_file"]):
            result = mw.wrap_tool_call(request, handler)
        if private:
            assert result.status == "error"  # context fence intersects the read allowlist
        else:
            assert result == ordinary
    assert mw.wrap_tool_call(request, handler) == ordinary


def test_resumed_private_segment_filters_schemas_and_blocks_parked_answers():
    from graph.middleware.subagent_fence import _SCRATCHPAD_KEY
    from tests.test_subagent_fence import _model_request

    projection = RequesterProjection(None, None, None, {}, None)
    projection.private_ref = "resumed-private"
    mw = SubagentFenceMiddleware()
    with projection.tool_scope():
        seen = []
        mw.wrap_model_call(_model_request(["read_file", "run_command"], []), seen.append)
        assert [t.name for t in seen[0].tools] == ["read_file"]
        request = SimpleNamespace(
            tool=SimpleNamespace(name="ask_human"), state={},
            tool_call={"name": "ask_human", "id": "call"},
            runtime=SimpleNamespace(config={"configurable": {
                _SCRATCHPAD_KEY: SimpleNamespace(resume=["private answer"]),
            }}),
        )
        result = mw.wrap_tool_call(request, lambda _: pytest.fail("private answer escaped"))
        assert result.status == "error"
    # Restoring the scope also restores ordinary model binding.
    seen = []
    mw.wrap_model_call(_model_request(["read_file", "run_command"], []), seen.append)
    assert [t.name for t in seen[0].tools] == ["read_file", "run_command"]


@pytest.mark.parametrize("private", [True, False])
@pytest.mark.asyncio
async def test_live_runner_fences_a_private_steer_turn(tmp_path, monkeypatch, delegation_env, private):
    # The PRODUCTION seam: no projection factory is installed on the live executor, so the
    # real runner binds the private segment itself once it resolves a private reference.
    from a2a_impl import private_intake
    from graph.fleet.delegation import DELEGATION_HEADER
    from tests.test_steer_idle_starts_turn import STATE, _Graph, _idle_snapshot, _operator_ctx, _partition, chat_mod

    engine, store = await _partition(tmp_path)
    monkeypatch.setattr(STATE, "graph", _Graph(_idle_snapshot()), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", engine, raising=False)
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(max_iterations=50), raising=False)
    message, metadata = "an ordinary message", {}
    if private:
        message = await private_intake.record_private_steer(
            store, session_id="chat-fence", text="FENCE-SECRET", msg_id="m1", context=_operator_ctx())
        token = chat_mod._mint_loopback_delegation(context="chat-fence", operation=message)
        metadata = {"private_input": True, DELEGATION_HEADER: token}
    mw, results = SubagentFenceMiddleware(), {}

    async def _no_pre(pre, session_id, request_metadata):
        return
        yield  # pragma: no cover

    async def _native(message, session_id, config, **kwargs):
        for name in ("read_file", "run_command"):
            request = SimpleNamespace(tool=SimpleNamespace(name=name, metadata=None), state={},
                                      tool_call={"name": name, "id": name})
            # A tool call runs in its own task, as under the real graph.
            results[name] = await asyncio.create_task(mw.awrap_tool_call(request, _ran))
        yield ("done", "reply")

    async def _ran(request):
        return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])

    async def _model(session_id, request_model):
        return "", ""

    monkeypatch.setattr(chat_mod._chat_dispatch, "_pre_turn_dispatch", _no_pre)
    monkeypatch.setattr(chat_mod, "_run_native_turn", _native)
    monkeypatch.setattr(chat_mod._goal_loop, "resolve_turn_model", _model)
    frames = [f async for f in chat_mod._chat_langgraph_stream_impl(message, "chat-fence", request_metadata=metadata)]
    assert frames[-1] == ("done", "reply")
    assert results["read_file"].content == "ran"
    assert (results["run_command"].status == "error") is private
    assert (results["run_command"].content == "ran") is not private
    assert not in_private_segment()  # the scope ends with the turn
    await engine.dispose()
