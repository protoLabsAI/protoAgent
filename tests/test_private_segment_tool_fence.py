"""Private tool calls use the real fence middleware, independently of feature flags."""
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from a2a_impl.requester_delivery import RequesterProjection
from graph.fence_scope import current_fence, fence_scope
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
