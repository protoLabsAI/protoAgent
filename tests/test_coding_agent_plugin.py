"""Tests for the coding_agent plugin (ADR 0024).

Covers config normalization + the `code_with` tool wiring (unit), and a real
ACP wire exchange: AcpClient drives a fake ACP agent subprocess through
initialize → session/new → session/prompt, accumulating agent_message_chunk
text and auto-allowing a session/request_permission.
"""

from __future__ import annotations

import sys

import pytest

import plugins.coding_agent as P
from plugins.coding_agent import _make_permission, _normalize_agents, register
from plugins.coding_agent.acp_client import AcpClient, AcpError

# ── a minimal ACP "agent" (server side) we can drive over stdio ───────────────
# Speaks just enough of the protocol: handshakes, opens a session, and on a
# prompt emits a tool_call narration, asks one permission (server→client
# request), then streams two agent_message_chunks — echoing the chosen option
# id so the test can prove auto-allow picked the `allow` option.
_FAKE_AGENT = r'''
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}})
    elif method == "session/prompt":
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s1",
            "update": {"sessionUpdate": "tool_call", "title": "Editing app.py"}}})
        # Ask permission; the client must respond before we continue.
        send({"jsonrpc": "2.0", "id": 999, "method": "session/request_permission",
              "params": {"sessionId": "s1",
                         "toolCall": {"toolCallId": "t1", "kind": "edit"},
                         "options": [
                             {"optionId": "reject", "kind": "reject_once"},
                             {"optionId": "ok", "kind": "allow_once"}]}})
        resp = json.loads(sys.stdin.readline().strip())
        chosen = resp.get("result", {}).get("outcome", {}).get("optionId")
        for chunk in ("Hello ", "world [" + str(chosen) + "]"):
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "s1",
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": chunk}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
'''


# ── config normalization ──────────────────────────────────────────────────────


def test_normalize_agents_keeps_valid_and_drops_bad():
    agents = _normalize_agents([
        {"name": "proto", "command": "proto", "args": ["--acp"], "workdir": "/tmp"},
        {"name": "nofields"},                       # missing command/workdir
        {"name": "x", "command": "x"},              # missing workdir
        "not-a-dict",                                # wrong type
        {"name": "proto", "command": "dup", "workdir": "/tmp"},  # duplicate name
    ])
    assert set(agents) == {"proto"}
    assert agents["proto"]["command"] == "proto"     # first wins over the dup
    assert agents["proto"]["args"] == ["--acp"]


def test_normalize_agents_coerces_env_and_args():
    agents = _normalize_agents([
        {"name": "a", "command": "c", "workdir": "/tmp",
         "args": "oops", "env": {"K": 1}},
    ])
    assert agents["a"]["args"] == []                 # non-list args ignored
    assert agents["a"]["env"] == {"K": "1"}          # env values stringified


# ── register() wiring ─────────────────────────────────────────────────────────


class _StubRegistry:
    def __init__(self, config):
        self.config = config
        self.tools = []

    def register_tool(self, tool):
        self.tools.append(tool)


def test_register_no_agents_registers_nothing():
    reg = _StubRegistry({"agents": []})
    register(reg)
    assert reg.tools == []


def test_register_with_agents_exposes_code_with():
    reg = _StubRegistry({"agents": [
        {"name": "proto", "command": "proto", "args": ["--acp"], "workdir": "/tmp"},
    ]})
    register(reg)
    assert [t.name for t in reg.tools] == ["code_with"]
    # Configured agent names are surfaced in the LLM-facing description.
    assert "proto" in reg.tools[0].description


async def test_code_with_unknown_agent_returns_error():
    reg = _StubRegistry({"agents": [
        {"name": "proto", "command": "proto", "args": ["--acp"], "workdir": "/tmp"},
    ]})
    register(reg)
    code_with = reg.tools[0]
    out = await code_with.ainvoke({"agent": "nope", "task": "do it"})
    assert "unknown coding agent" in out and "proto" in out


async def test_code_with_empty_task_returns_error():
    reg = _StubRegistry({"agents": [
        {"name": "proto", "command": "proto", "args": ["--acp"], "workdir": "/tmp"},
    ]})
    register(reg)
    code_with = reg.tools[0]
    out = await code_with.ainvoke({"agent": "proto", "task": "   "})
    assert "empty" in out.lower()


# ── ACP wire exchange against the fake agent ──────────────────────────────────


@pytest.fixture
def fake_agent(tmp_path):
    script = tmp_path / "fake_acp_agent.py"
    script.write_text(_FAKE_AGENT, encoding="utf-8")
    return script


async def test_acp_client_drives_a_turn(fake_agent, tmp_path):
    narrations: list[str] = []

    async def on_progress(title: str) -> None:
        narrations.append(title)

    client = AcpClient(sys.executable, [str(fake_agent)], cwd=str(tmp_path), name="fake")
    try:
        answer = await client.prompt("add a healthz route", progress_callback=on_progress, timeout=30.0)
    finally:
        await client.close()

    # agent_message_chunks accumulated; default auto-allow picked the allow option.
    assert answer == "Hello world [ok]"
    # tool_call title narrated via the progress callback.
    assert "Editing app.py" in narrations


async def test_acp_client_readonly_policy_denies_edit(fake_agent, tmp_path):
    # A readonly policy must reject the fake's `edit` permission request — the
    # client picks the reject_once option, which the fake echoes back.
    spec = {"name": "ro", "permissions": "readonly", "allow_kinds": [], "deny_kinds": []}
    client = AcpClient(
        sys.executable, [str(fake_agent)], cwd=str(tmp_path),
        permission=_make_permission(spec),
    )
    try:
        answer = await client.prompt("edit a file", timeout=30.0)
    finally:
        await client.close()
    assert answer == "Hello world [reject]"


async def test_acp_client_missing_binary_raises_acp_error(tmp_path):
    client = AcpClient("definitely-not-a-real-binary-xyz", [], cwd=str(tmp_path))
    with pytest.raises(AcpError):
        await client.prompt("hi", timeout=10.0)


async def test_acp_client_bad_workdir_raises_acp_error():
    client = AcpClient(sys.executable, [], cwd="/no/such/dir/anywhere")
    with pytest.raises(AcpError):
        await client.prompt("hi", timeout=10.0)


# ── permission policy ─────────────────────────────────────────────────────────

_OPTS = [{"optionId": "a", "kind": "allow_once"}, {"optionId": "r", "kind": "reject_once"}]


def _perm(policy, kind, options=None, allow=None, deny=None):
    spec = {
        "name": "x", "permissions": policy,
        "allow_kinds": [k.lower() for k in (allow or [])],
        "deny_kinds": [k.lower() for k in (deny or [])],
    }
    return _make_permission(spec)({"toolCall": {"kind": kind}, "options": options or _OPTS})


def test_policy_auto_allows_everything():
    assert _perm("auto", "execute") == "a"
    assert _perm("auto", "delete") == "a"
    assert _perm("auto", "edit") == "a"


def test_policy_allowlist_denies_risky_allows_safe():
    assert _perm("allowlist", "edit") == "a"
    assert _perm("allowlist", "read") == "a"
    assert _perm("allowlist", "execute") == "r"      # risky → reject option
    assert _perm("allowlist", "delete") == "r"


def test_policy_readonly_allows_read_denies_writes():
    assert _perm("readonly", "read") == "a"
    assert _perm("readonly", "search") == "a"
    assert _perm("readonly", "edit") == "r"
    assert _perm("readonly", "execute") == "r"


def test_policy_deny_cancels_when_no_reject_option():
    only_allow = [{"optionId": "a", "kind": "allow_once"}]
    assert _perm("readonly", "edit", options=only_allow) is None


def test_policy_custom_allow_deny_kinds():
    assert _perm("allowlist", "edit", deny=["edit"]) == "r"        # explicitly denied
    assert _perm("readonly", "edit", allow=["read", "edit"]) == "a"  # explicitly allowed


def test_normalize_agents_parses_safety_fields():
    a = _normalize_agents([{
        "name": "p", "command": "c", "workdir": "/tmp",
        "permissions": "READONLY", "confirm": True,
        "allow_kinds": ["Read"], "deny_kinds": ["Execute"],
    }])["p"]
    assert a["permissions"] == "readonly"          # lower-cased
    assert a["confirm"] is True
    assert a["allow_kinds"] == ["read"] and a["deny_kinds"] == ["execute"]


def test_normalize_agents_bad_policy_falls_back_to_auto():
    a = _normalize_agents([{"name": "p", "command": "c", "workdir": "/tmp", "permissions": "yolo"}])["p"]
    assert a["permissions"] == "auto"
    assert a["confirm"] is False                    # default


# ── per-call consent gate (confirm) ───────────────────────────────────────────


async def test_confirm_gate_declines(monkeypatch):
    import langgraph.types as lt
    monkeypatch.setattr(lt, "interrupt", lambda payload: "no")
    reg = _StubRegistry({"agents": [
        {"name": "proto", "command": "proto", "workdir": "/tmp", "confirm": True},
    ]})
    register(reg)
    out = await reg.tools[0].ainvoke({"agent": "proto", "task": "do it"})
    assert "Declined" in out


async def test_confirm_gate_approves_then_runs(monkeypatch):
    import langgraph.types as lt
    monkeypatch.setattr(lt, "interrupt", lambda payload: "yes")

    class _StubClient:
        async def prompt(self, task, progress_callback=None, timeout=600.0):
            return "did the work"

    monkeypatch.setattr(P, "_client_for", lambda spec: _StubClient())
    reg = _StubRegistry({"agents": [
        {"name": "proto", "command": "proto", "workdir": "/tmp", "confirm": True},
    ]})
    register(reg)
    out = await reg.tools[0].ainvoke({"agent": "proto", "task": "do it"})
    assert out == "did the work"


async def test_evict_client_pops_and_closes():
    """evict_client removes the cached client AND awaits close() — a plain pop
    would forget the handle but leave the subprocess alive."""
    spec = {
        "name": "proto", "command": "proto", "args": ["--acp"], "workdir": "/tmp/wt-1",
        "permissions": "allowlist", "allow_kinds": [], "deny_kinds": [],
    }

    class _FakeClient:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    fake = _FakeClient()
    P._CLIENTS[P._cache_key(spec)] = fake

    assert await P.evict_client(spec) is True
    assert fake.closed is True
    assert P._cache_key(spec) not in P._CLIENTS
    # idempotent: nothing cached for this spec now
    assert await P.evict_client(spec) is False


async def test_evict_client_swallows_close_errors():
    """A close() that raises must not propagate — teardown is best-effort, and the
    cache entry is dropped regardless."""
    spec = {
        "name": "proto", "command": "proto", "args": [], "workdir": "/tmp/wt-2",
        "permissions": "auto", "allow_kinds": [], "deny_kinds": [],
    }

    class _BadClient:
        async def close(self):
            raise RuntimeError("terminate blew up")

    P._CLIENTS[P._cache_key(spec)] = _BadClient()
    assert await P.evict_client(spec) is True          # did not raise
    assert P._cache_key(spec) not in P._CLIENTS
