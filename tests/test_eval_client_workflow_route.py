"""Regression (#4310): the eval client drives workflows through the plugin route.

Core retired ``/api/workflows``; the bundled ``workflows`` plugin serves
``/api/plugins/workflows/{name}/run``. The client kept posting the old path, so
every ``kind: "workflow"`` eval case 404'd without reaching a recipe. These tests
mount the real plugin router on the prefix the plugin registers and drive the
real ``AgentClient`` at it.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import evals.client as ec


class _RouterReg:
    """Plugin-registry double: keeps the router + prefix the plugin registers."""

    def __init__(self):
        self.routers: list = []

    def register_tools(self, tools):
        pass

    def register_workflow_dir(self, d):
        pass

    def register_router(self, router, prefix=None):
        self.routers.append((prefix, router))


def _app(tmp_path, monkeypatch, *, mount: bool = True) -> FastAPI:
    import plugins.workflows as wf
    import runtime.state as rs

    async def run_subagent(subagent_type, prompt, description="", **_sdk_kwargs):
        return f"echo: {prompt}"

    monkeypatch.setattr(wf.sdk, "subagent_types", lambda: {"researcher"})
    monkeypatch.setattr(wf.sdk, "run_subagent", run_subagent)
    monkeypatch.setattr(
        wf.sdk,
        "config",
        lambda: SimpleNamespace(subagent_max_concurrency=2, workflow_dir=str(tmp_path / "writable")),
    )
    monkeypatch.setattr(rs.STATE, "workflow_registry", None, raising=False)
    monkeypatch.setattr(rs.STATE, "workflow_run", None, raising=False)
    monkeypatch.setattr(rs.STATE, "plugin_workflow_dirs", [], raising=False)
    (tmp_path / "writable").mkdir()
    (tmp_path / "writable" / "echo.yaml").write_text(
        "name: echo\ndescription: one step\ninputs:\n  - name: q\n    required: true\n"
        "steps:\n  - id: a\n    subagent: researcher\n    prompt: '{{inputs.q}}'\n",
        encoding="utf-8",
    )
    reg = _RouterReg()
    wf.register(reg)
    app = FastAPI()
    if mount:
        for prefix, router in reg.routers:
            app.include_router(router, prefix=prefix or "")
    return app


def _client(app: FastAPI, monkeypatch) -> ec.AgentClient:
    real = httpx.AsyncClient

    def _fake(*args, **kwargs):
        kwargs["transport"] = httpx.ASGITransport(app=app)
        return real(*args, **kwargs)

    monkeypatch.setattr(ec.httpx, "AsyncClient", _fake)
    return ec.AgentClient(base_url="http://agent")


async def test_run_workflow_reaches_the_plugin_route(tmp_path, monkeypatch):
    client = _client(_app(tmp_path, monkeypatch), monkeypatch)
    out = await client.run_workflow("echo", {"q": "ping"})
    assert "echo: ping" in out.get("output", "")


async def test_run_workflow_names_the_disabled_plugin_on_404(tmp_path, monkeypatch):
    client = _client(_app(tmp_path, monkeypatch, mount=False), monkeypatch)
    with pytest.raises(RuntimeError, match="workflows plugin is not enabled"):
        await client.run_workflow("echo", {"q": "ping"})


async def test_unknown_recipe_is_not_reported_as_a_disabled_plugin(tmp_path, monkeypatch):
    client = _client(_app(tmp_path, monkeypatch), monkeypatch)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await client.run_workflow("no-such-recipe", {})
    assert exc.value.response.status_code == 400
