"""SubagentOnlyMiddleware (ADR 0117): on an unfenced pass (the lead) a held tool is
hidden from the model and a call to it is blocked; a fenced pass (a background subagent
running the lead graph) is left to the fence."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from graph.middleware.subagent_only import SubagentOnlyMiddleware

MW = SubagentOnlyMiddleware({"media_grab"})


class _Req(SimpleNamespace):
    def override(self, **kw):
        return _Req(**{**self.__dict__, **kw})


def _tools(*names):
    return [SimpleNamespace(name=n) for n in names]


def _model_req(fence=None):
    return _Req(tools=_tools("media_grab", "task"), state={"subagent_fence": fence} if fence else {})


def _seen_names(req):
    return MW.wrap_model_call(req, lambda r: [t.name for t in r.tools])


def test_the_lead_model_never_sees_a_held_tool():
    assert _seen_names(_model_req()) == ["task"]


def test_a_fenced_pass_keeps_it_for_the_fence_to_decide():
    assert _seen_names(_model_req(fence=["media_grab"])) == ["media_grab", "task"]


def _tool_req(name, fence=None):
    return _Req(tool_call={"name": name, "id": "c1"}, state={"subagent_fence": fence} if fence else {})


def test_the_lead_calling_a_held_tool_is_blocked_with_a_route():
    out = MW.wrap_tool_call(_tool_req("media_grab"), lambda r: "ran")
    assert isinstance(out, ToolMessage) and out.status == "error" and "task" in out.content


def test_a_fenced_subagent_runs_it():
    assert MW.wrap_tool_call(_tool_req("media_grab", fence=["media_grab"]), lambda r: "ran") == "ran"


def test_other_tools_pass_through_and_async_matches():
    assert MW.wrap_tool_call(_tool_req("task"), lambda r: "ran") == "ran"

    async def h(_r):
        return "ran"

    out = asyncio.run(MW.awrap_tool_call(_tool_req("media_grab"), h))
    assert isinstance(out, ToolMessage)
    assert asyncio.run(MW.awrap_model_call(_model_req(), lambda r: _async_names(r))) == ["task"]


async def _async_names(r):
    return [t.name for t in r.tools]
