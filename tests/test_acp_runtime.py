"""ACP agent runtime (ADR 0033 slice 3) — runtime resolution + turn driving (mocked client)."""

from __future__ import annotations

import types

import pytest

from runtime.acp_runtime import AcpRuntime, adapter_for, operator_mcp_server_spec, resolve_runtime
from runtime.context import AssembledContext


def _cfg(**kw):
    base = dict(agent_runtime="acp:codex", operator_mcp_tools=["beads_list"], acp_agents={})
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_resolve_runtime_variants():
    assert resolve_runtime(types.SimpleNamespace(agent_runtime="native")) == ("native", "")
    assert resolve_runtime(types.SimpleNamespace(agent_runtime="acp:codex")) == ("acp", "codex")
    assert resolve_runtime(types.SimpleNamespace(agent_runtime="acp")) == ("native", "")   # needs an agent
    assert resolve_runtime(types.SimpleNamespace(agent_runtime="bogus")) == ("native", "")


def test_adapter_default_and_override():
    assert adapter_for("codex")["command"] == "npx"
    cfg = types.SimpleNamespace(acp_agents={"codex": {"command": "mycodex", "args": ["x"]}})
    assert adapter_for("codex", cfg) == {"command": "mycodex", "args": ["x"]}
    with pytest.raises(ValueError):
        adapter_for("nonexistent")


def test_operator_mcp_spec_gated_on_allowlist():
    assert operator_mcp_server_spec(types.SimpleNamespace(operator_mcp_tools=[])) is None
    spec = operator_mcp_server_spec(types.SimpleNamespace(operator_mcp_tools=["beads_list"]))
    assert spec["name"] == "protoagent-operator"
    assert spec["args"] == ["-m", "server.operator_mcp"]


class _FakeCtx:
    def __init__(self):
        self.after = []

    def assemble(self, *, query=""):
        return AssembledContext(stable_prefix="PERSONA", volatile_delta=f"KB[{query}]", sources=[])

    def after_turn(self, *, user="", response=""):
        self.after.append((user, response))


class _FakeClient:
    def __init__(self):
        self.prompts = []
        self.closed = False

    async def prompt(self, text, progress_callback=None):
        self.prompts.append(text)
        return "ANSWER"

    async def close(self):
        self.closed = True


async def test_run_turn_sends_prefix_once_then_deltas():
    client, ctx = _FakeClient(), _FakeCtx()
    rt = AcpRuntime(_cfg(), client_factory=lambda: client, context=ctx)
    a1 = await rt.run_turn("hello")
    a2 = await rt.run_turn("again")
    assert a1 == a2 == "ANSWER"
    # First turn carries the cacheable persona prefix; later turns don't (stateful session).
    assert client.prompts[0] == "PERSONA\n\nKB[hello]\n\nhello"
    assert client.prompts[1] == "KB[again]\n\nagain"
    assert ctx.after == [("hello", "ANSWER"), ("again", "ANSWER")]
    await rt.close()
    assert client.closed


def test_default_factory_mounts_operator_mcp(monkeypatch):
    captured = {}

    import plugins.coding_agent.acp_client as acp

    class _Spy:
        def __init__(self, command, args=None, *, cwd, name, mcp_servers=None, **kw):
            captured.update(command=command, name=name, mcp_servers=mcp_servers)

    monkeypatch.setattr(acp, "AcpClient", _Spy)
    rt = AcpRuntime(_cfg(), context=_FakeCtx())
    rt._ensure_client()
    assert captured["name"] == "codex"
    assert captured["command"] == "npx"
    assert captured["mcp_servers"][0]["name"] == "protoagent-operator"


def test_constructing_for_native_raises():
    with pytest.raises(ValueError):
        AcpRuntime(types.SimpleNamespace(agent_runtime="native"))


def test_chat_caches_acp_runtime_per_thread(monkeypatch):
    import importlib

    chat = importlib.import_module("server.chat")  # the `server.chat` attr is the re-exported fn
    from runtime.state import STATE

    monkeypatch.setattr(
        STATE, "graph_config",
        types.SimpleNamespace(agent_runtime="acp:codex", operator_mcp_tools=[], acp_agents={}),
        raising=False,
    )
    chat._ACP_RUNTIMES.clear()
    r1 = chat._get_acp_runtime("t1")
    r2 = chat._get_acp_runtime("t1")
    r3 = chat._get_acp_runtime("t2")
    assert r1 is r2          # same thread → same stateful ACP session
    assert r1 is not r3      # different thread → its own session
    assert r1.agent == "codex"
