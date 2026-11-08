"""Register-time footgun guards (#4804 part 1).

Two mistakes a plugin ships green and only hits at runtime, now caught the moment it
registers — the live ``PluginRegistry`` WARNS (degrade-safe: the contribution still lands) and
the host-free ``FakeRegistry`` RAISES (so a plugin's smoke test fails before it ships):

  A. A FastAPI route whose ``Request`` / ``WebSocket`` / ``Response`` / ``BackgroundTasks`` is
     imported inside the router-builder under ``from __future__ import annotations`` keeps a
     string annotation FastAPI can't resolve, so it becomes a required query parameter and the
     route 422s.
  B. A LangChain ``@tool`` parameter named ``config`` / ``callbacks`` / ``run_manager`` that
     isn't annotated as the injected type — LangChain reserves the name, so the model never
     supplies it and its value arrives as ``None``.

The detection is duplicated between ``graph.plugins.registry`` and the host-free
``graph.plugins.testkit`` (which can't import the former); the parity tests at the bottom keep
the two copies from drifting.
"""

import importlib.util
import logging
import sys
import uuid
from types import SimpleNamespace
from typing import ForwardRef

import pytest
from langchain_core.callbacks import Callbacks
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from graph.plugins import registry as R
from graph.plugins import testkit
from graph.plugins.registry import PluginRegistry

# ── fixtures ──────────────────────────────────────────────────────────────────────────────

# The footgun needs a module under ``from __future__ import annotations`` whose Request is NOT
# in the module globals (it was imported inside the builder), vs a correct module that imports
# it at module level. Two distinct module namespaces are required — the endpoint's own globals
# are where FastAPI resolves the string annotation — so build them from source into tmp modules.

_FOOTGUN_SOURCE = """
from __future__ import annotations
from fastapi import APIRouter


def build():
    from fastapi import Request  # the footgun: a LOCAL import, absent from module globals

    r = APIRouter()

    @r.get("/ping")
    async def ping(request: Request):
        return {"ok": True}

    return r
"""

_CORRECT_SOURCE = """
from __future__ import annotations
from fastapi import APIRouter, Request  # the fix: Request at module level resolves fine


def build():
    r = APIRouter()

    @r.get("/ping")
    async def ping(request: Request):
        return {"ok": True}

    @r.get("/search")
    async def search(q: str):  # a genuine query param — must stay silent
        return {"q": q}

    return r
"""

_NESTED_SOURCE = """
from __future__ import annotations
from fastapi import APIRouter


def build():
    from fastapi import WebSocket

    child = APIRouter()

    @child.websocket("/ws")
    async def ws(websocket: WebSocket):
        ...

    parent = APIRouter()
    parent.include_router(child, prefix="/child")
    return parent
"""


def _module_from_source(source: str):
    name = "ftg_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__["__name__"] = name
    sys.modules[name] = mod
    try:
        exec(compile(source, f"<{name}>", "exec"), mod.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod


@pytest.fixture(params=["Request", "WebSocket", "Response", "BackgroundTasks"])
def footgun_router(request):
    mod = _module_from_source(_FOOTGUN_SOURCE.replace("Request", request.param))
    try:
        yield mod.build()
    finally:
        sys.modules.pop(mod.__name__, None)


@pytest.fixture
def correct_router():
    mod = _module_from_source(_CORRECT_SOURCE)
    try:
        yield mod.build()
    finally:
        sys.modules.pop(mod.__name__, None)


@pytest.fixture
def nested_footgun_router():
    mod = _module_from_source(_NESTED_SOURCE)
    try:
        yield mod.build()
    finally:
        sys.modules.pop(mod.__name__, None)


def _live_registry():
    return PluginRegistry("demo-plugin", __import__("pathlib").Path("."))


# ── footgun A: a string-annotated ASGI param on a route ─────────────────────────────────────


def test_router_footgun_fake_registry_raises_with_the_fix(footgun_router):
    fake = testkit.FakeRegistry(plugin_id="demo-plugin")
    with pytest.raises(ValueError) as exc:
        fake.register_router(footgun_router)
    msg = str(exc.value)
    assert "request" in msg
    assert "demo-plugin" in msg and "GET /ping" in msg
    assert "at module level" in msg
    assert "from __future__ import annotations" in msg
    assert fake.routers == []  # refused: nothing captured


def test_router_footgun_live_registry_warns_and_still_mounts(footgun_router, caplog):
    reg = _live_registry()
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        reg.register_router(footgun_router)
    hits = [r for r in caplog.records if "request" in r.getMessage() and "422" in r.getMessage()]
    assert hits, "live registry must warn about the footgun"
    assert "at module level" in hits[0].getMessage()
    assert len(reg.routers) == 1  # degrade-safe: the route still mounts


def test_router_nested_websocket_footgun_is_caught(nested_footgun_router):
    fake = testkit.FakeRegistry(plugin_id="demo-plugin")
    with pytest.raises(ValueError) as exc:
        fake.register_router(nested_footgun_router)
    msg = str(exc.value)
    assert "/child/ws" in msg  # nested prefix is walked through
    assert "websocket" in msg and "WebSocket" in msg


def test_router_correct_import_passes_silently(correct_router, caplog):
    # Module-level Request resolves, so `request` is NOT a query param and `q` is a genuine one.
    fake = testkit.FakeRegistry(plugin_id="demo-plugin")
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        fake.register_router(correct_router)  # must not raise
        _live_registry().register_router(correct_router)
    assert len(fake.routers) == 1
    assert not [r for r in caplog.records if "422" in r.getMessage()]


# ── footgun B: a reserved @tool parameter name ──────────────────────────────────────────────


@tool
def bad_config(x: int, config: dict) -> str:
    """A plain `config` param — LangChain hijacks it as the injected RunnableConfig."""
    return "ok"


@tool
def bad_callbacks(x: int, callbacks: list) -> str:
    """A plain `callbacks` param — reserved and silently stripped from the model's schema."""
    return "ok"


@tool
async def bad_run_manager(x: int, run_manager: str) -> str:
    """A plain `run_manager` param (async tool) — reserved too."""
    return "ok"


@tool
def good_config(x: int, config: RunnableConfig) -> str:
    """`config` annotated RunnableConfig — the author meant the injected arg; fine."""
    return "ok"


@tool
def good_callbacks(x: int, callbacks: Callbacks) -> str:
    """`callbacks` annotated as the callback type — fine."""
    return "ok"


@tool
def clean_tool(x: int, note: str) -> str:
    """No reserved parameter names at all."""
    return "ok"


@pytest.mark.parametrize(
    "bad_tool, param",
    [(bad_config, "config"), (bad_callbacks, "callbacks"), (bad_run_manager, "run_manager")],
)
def test_tool_footgun_fake_registry_raises_with_the_fix(bad_tool, param):
    fake = testkit.FakeRegistry(plugin_id="demo-plugin")
    with pytest.raises(ValueError) as exc:
        fake.register_tool(bad_tool)
    msg = str(exc.value)
    assert bad_tool.name in msg
    assert repr(param) in msg
    assert "Rename the parameter" in msg
    assert fake.tools == []  # refused: nothing captured


def test_tool_footgun_also_fires_through_register_tools():
    fake = testkit.FakeRegistry(plugin_id="demo-plugin")
    with pytest.raises(ValueError):  # register_tools checks each tool, not just extends
        fake.register_tools([clean_tool, bad_config])


@pytest.mark.parametrize("bad_tool", [bad_config, bad_callbacks, bad_run_manager])
def test_tool_footgun_live_registry_warns_and_still_registers(caplog, bad_tool):
    reg = _live_registry()
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        reg.register_tools([bad_tool])
    hits = [r for r in caplog.records if bad_tool.name in r.getMessage() and "Rename" in r.getMessage()]
    assert hits, "live registry must warn about the reserved tool param"
    assert reg.tools == [bad_tool]  # degrade-safe: still registered


@pytest.mark.parametrize("good_tool", [good_config, good_callbacks, clean_tool])
def test_tool_correct_annotation_passes_silently(good_tool, caplog):
    fake = testkit.FakeRegistry(plugin_id="demo-plugin")
    reg = _live_registry()
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        fake.register_tool(good_tool)  # must not raise
        reg.register_tool(good_tool)
    assert good_tool in fake.tools and good_tool in reg.tools
    assert not [r for r in caplog.records if "Rename" in r.getMessage()]


# ── parity: the registry and testkit copies must not drift ──────────────────────────────────


def test_router_detection_parity(footgun_router, nested_footgun_router, correct_router):
    for router in (footgun_router, nested_footgun_router, correct_router):
        assert list(R._router_param_footguns(router)) == list(testkit._router_param_footguns(router))


def test_tool_detection_parity():
    for t in (bad_config, bad_callbacks, bad_run_manager, good_config, good_callbacks, clean_tool):
        assert R._tool_reserved_param_footgun(t) == testkit._tool_reserved_param_footgun(t)


def test_message_builder_parity():
    assert R._router_footgun_message("p", "GET", "/x", "request", "Request") == testkit._router_footgun_message(
        "p", "GET", "/x", "request", "Request"
    )
    assert R._tool_footgun_message("p", "t", "config") == testkit._tool_footgun_message("p", "t", "config")


@pytest.mark.parametrize("param", ["config", "callbacks", "run_manager"])
@pytest.mark.parametrize("schema_kind", ["pydantic", "json"])
def test_schema_only_tool_is_checked(param, schema_kind, caplog):
    from pydantic import create_model

    schema = (
        create_model("ToolArgs", **{param: (str, ...)})
        if schema_kind == "pydantic"
        else {"type": "object", "properties": {param: {"type": "string"}}}
    )
    candidate = SimpleNamespace(name="schema_tool", args_schema=schema, invoke=lambda args: args)
    fake = testkit.FakeRegistry(plugin_id="demo-plugin")
    with pytest.raises(ValueError, match="Rename the parameter"):
        fake.register_tools([candidate])
    live = _live_registry()
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        live.register_tools([candidate])
    assert live.tools == [candidate]
    assert "schema_tool" in caplog.text and repr(param) in caplog.text
    assert "Rename the parameter" in caplog.text


@pytest.mark.parametrize("annotation", ["Request", ForwardRef("Request")])
def test_query_parameter_annotation_shapes(annotation):
    route = SimpleNamespace(
        path="/ping", methods={"GET"},
        dependant=SimpleNamespace(query_params=[
            SimpleNamespace(name="request", field_info=SimpleNamespace(annotation=annotation))
        ]),
    )
    router = SimpleNamespace(routes=[route])
    expected = [("GET", "/ping", "request", "Request")]
    assert list(R._router_param_footguns(router)) == expected
    assert list(testkit._router_param_footguns(router)) == expected


@pytest.mark.parametrize("param", ["config", "callbacks", "run_manager"])
def test_unannotated_tool_parameter_is_rejected(param, caplog):
    namespace = {}
    exec(
        f'def unannotated({param}):\n    """A tool with an untyped reserved argument."""\n    return "ok"',
        namespace,
    )
    candidate = tool(namespace["unannotated"])
    with pytest.raises(ValueError, match="Rename the parameter"):
        testkit.FakeRegistry(plugin_id="demo-plugin").register_tool(candidate)
    live = _live_registry()
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        live.register_tool(candidate)
    assert live.tools == [candidate]
    assert repr(param) in caplog.text and "Rename the parameter" in caplog.text
