"""Tests for the coding_agent plugin (ADR 0024).

Covers config normalization + the `code_with` tool wiring (unit), and a real
ACP wire exchange: AcpClient drives a fake ACP agent subprocess through
initialize → session/new → session/prompt, accumulating agent_message_chunk
text and auto-allowing a session/request_permission.
"""

from __future__ import annotations

import sys

import pytest

from plugins.coding_agent import _normalize_agents, register
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
              "params": {"sessionId": "s1", "options": [
                  {"optionId": "no", "kind": "deny"},
                  {"optionId": "yes", "kind": "allow_once"}]}})
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

    # agent_message_chunks accumulated; auto-allow picked the `allow_once` option.
    assert answer == "Hello world [yes]"
    # tool_call title narrated via the progress callback.
    assert "Editing app.py" in narrations


async def test_acp_client_missing_binary_raises_acp_error(tmp_path):
    client = AcpClient("definitely-not-a-real-binary-xyz", [], cwd=str(tmp_path))
    with pytest.raises(AcpError):
        await client.prompt("hi", timeout=10.0)


async def test_acp_client_bad_workdir_raises_acp_error():
    client = AcpClient(sys.executable, [], cwd="/no/such/dir/anywhere")
    with pytest.raises(AcpError):
        await client.prompt("hi", timeout=10.0)
