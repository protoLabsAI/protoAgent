"""Tests for the coding_agent ACP client library (ADR 0024).

The ``code_with`` tool was retired in favour of ``delegate_to`` (ADR 0025); this
module is now the shared ACP client library. Covered here: a real ACP wire
exchange (``AcpClient`` drives a fake ACP agent subprocess through
initialize → session/new → session/prompt, accumulating agent_message_chunk text
and auto-allowing a session/request_permission), the by-kind permission policy,
and client-cache eviction/teardown.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

import plugins.coding_agent as P
from plugins.coding_agent import _make_permission
from plugins.coding_agent.acp_client import AcpClient, AcpError, _short_tool_name, _split_tool_title


def test_short_tool_name_peels_inline_args_and_mcp_source():
    # A verbose MCP tool title → a compact card label (args + source go to the body).
    assert (
        _short_tool_name('web_search (protoagent-operator MCP Server): {"query":"pdx weather"}')
        == "web_search"
    )
    assert _short_tool_name('fetch_url (protoagent-operator MCP Server): {"url":"https://x"}') == "fetch_url"
    # No inline args / no parenthetical → left mostly as-is.
    assert _short_tool_name("Skill: Use skill: 'browser-automation'") == "Skill: Use skill: 'browser-automation'"
    # A legit, non-MCP trailing parenthetical is PRESERVED (only "(… MCP Server)" is peeled).
    assert _short_tool_name("search (beta)") == "search (beta)"
    # Defensive cap so an unbounded title can never blow out the header.
    assert len(_short_tool_name("x" * 500)) <= 80


def test_split_tool_title_separates_inline_json():
    label, inline = _split_tool_title('web_search (proto MCP Server): {"query":"pdx"}')
    assert label == "web_search (proto MCP Server)"
    assert inline == '{"query":"pdx"}'
    # No JSON → empty inline, label unchanged.
    assert _split_tool_title("current_time") == ("current_time", "")


# ── a minimal ACP "agent" (server side) we can drive over stdio ───────────────
# Speaks just enough of the protocol: handshakes, opens a session, and on a
# prompt emits a tool_call narration, asks one permission (server→client
# request), then streams two agent_message_chunks — echoing the chosen option
# id so the test can prove auto-allow picked the `allow` option.
_FAKE_AGENT = r"""
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
"""


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


async def test_close_reaps_the_subprocess(fake_agent, tmp_path):
    """close() must terminate AND await the child so it's reaped while the loop is
    still alive — otherwise the subprocess transport's __del__ fires after the loop
    closes ('Event loop is closed') and the stderr-drain task leaks."""
    client = AcpClient(sys.executable, [str(fake_agent)], cwd=str(tmp_path), name="reap")
    await client.prompt("go", timeout=30.0)
    assert client._proc is not None and client._proc.returncode is None  # alive after the turn
    await client.close()
    assert client._proc.returncode is not None  # reaped during close (the fix)
    assert client._stderr_task is not None and client._stderr_task.done()  # not leaked


async def test_acp_client_readonly_policy_denies_edit(fake_agent, tmp_path):
    # A readonly policy must reject the fake's `edit` permission request — the
    # client picks the reject_once option, which the fake echoes back.
    spec = {"name": "ro", "permissions": "readonly", "allow_kinds": [], "deny_kinds": []}
    client = AcpClient(
        sys.executable,
        [str(fake_agent)],
        cwd=str(tmp_path),
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


# ── abort + auth lifecycle ────────────────────────────────────────────────────

# Handshakes, then on session/prompt HANGS (never replies) — keeps reading stdin
# so it can receive a session/cancel notification, which it records to a marker
# file. Proves the client cancels the in-flight turn on the abort path instead of
# leaving the session mid-generation.
_CANCEL_AGENT = r"""
import sys, json, os
MARKER = os.environ.get("CANCEL_MARKER", "")
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
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
        pass  # hang — wait for the client to cancel
    elif method == "session/cancel":
        if MARKER:
            with open(MARKER, "w") as fh:
                fh.write("cancelled")
        break
"""

# Advertises an auth method, then rejects session/new with AUTH_REQUIRED (-32000).
_AUTH_AGENT = r"""
import sys, json
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
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
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "authMethods": [{"id": "openai", "name": "Use OpenAI API key"}]}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid,
              "error": {"code": -32000, "message": "auth required"}})
"""


async def test_prompt_timeout_sends_session_cancel(tmp_path):
    script = tmp_path / "cancel_agent.py"
    script.write_text(_CANCEL_AGENT, encoding="utf-8")
    marker = tmp_path / "cancelled.marker"
    client = AcpClient(
        sys.executable,
        [str(script)],
        cwd=str(tmp_path),
        name="cancel",
        env={"CANCEL_MARKER": str(marker)},
    )
    try:
        with pytest.raises(AcpError):
            await client.prompt("hang please", timeout=1.0)
        # The timeout path must have sent session/cancel; the fake records it.
        for _ in range(60):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        assert marker.exists(), "client did not send session/cancel on prompt timeout"
    finally:
        await client.close()


async def test_session_new_auth_required_raises_actionable(tmp_path):
    script = tmp_path / "auth_agent.py"
    script.write_text(_AUTH_AGENT, encoding="utf-8")
    client = AcpClient(sys.executable, [str(script)], cwd=str(tmp_path), name="auth")
    try:
        with pytest.raises(AcpError) as ei:
            await client.prompt("do work", timeout=10.0)
    finally:
        await client.close()
    msg = str(ei.value)
    assert "requires authentication" in msg  # actionable, not opaque
    assert "openai" in msg  # advertised auth method surfaced
    assert ei.value.code == -32000  # AUTH_REQUIRED preserved


# ── session lifecycle: load / close / version / thought (#970) ────────────────

# Advertises the `loadSession` capability, records whether session/new vs
# session/load fired (to a marker), and on load replays one history chunk BEFORE
# responding null — so the test can prove the replay is suppressed on reattach.
_LOADER_AGENT = r"""
import sys, json, os
MARKER = os.environ.get("MARKER", "")
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
def mark(s):
    if MARKER:
        with open(MARKER, "w") as fh: fh.write(s)
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
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1, "agentCapabilities": {"loadSession": True}}})
    elif method == "session/new":
        mark("new")
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}})
    elif method == "session/load":
        # Replay one history entry, then respond null (per spec).
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk",
                                          "content": {"type": "text", "text": "OLD HISTORY"}}}})
        mark("load:" + str(msg.get("params", {}).get("sessionId")))
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "session/prompt":
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk",
                                          "content": {"type": "text", "text": "fresh"}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""

# Emits an agent_thought_chunk (reasoning) then an agent_message_chunk (answer),
# so the test can prove thoughts are surfaced separately and never folded in.
_THOUGHT_AGENT = r"""
import sys, json
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
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
            "sessionId": "s1", "update": {"sessionUpdate": "agent_thought_chunk",
                                          "content": {"type": "text", "text": "thinking hard"}}}})
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk",
                                          "content": {"type": "text", "text": "the answer"}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""

# Counters with protocolVersion 2 (which this client does not speak) — the client
# must close rather than proceed.
_V2_AGENT = r"""
import sys, json
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": msg.get("id"), "result": {"protocolVersion": 2}})
"""


async def test_session_load_reattaches_persisted_session(tmp_path):
    """A persisted session id + an agent that advertises loadSession ⇒ the client
    session/loads (reattaches) rather than session/news, and the replayed history is
    suppressed (the reattach is silent — only the new turn's text is the answer)."""
    script = tmp_path / "loader_agent.py"
    script.write_text(_LOADER_AGENT, encoding="utf-8")
    marker = tmp_path / "which.marker"
    sess_file = tmp_path / "sess.json"
    sess_file.write_text(
        json.dumps({"sessionId": "s1", "cwd": str(tmp_path), "command": sys.executable}),
        encoding="utf-8",
    )
    client = AcpClient(
        sys.executable,
        [str(script)],
        cwd=str(tmp_path),
        name="loader",
        env={"MARKER": str(marker)},
        session_id_path=sess_file,
    )
    try:
        answer = await client.prompt("continue the thread", timeout=30.0)
    finally:
        await client.close()
    assert answer == "fresh"  # replayed "OLD HISTORY" suppressed
    assert marker.read_text() == "load:s1"  # reattached via session/load, not new
    assert client._session_id == "s1"


async def test_session_new_persists_id_for_reattach(fake_agent, tmp_path):
    """A fresh session/new writes its id (with cwd) to the session-id path so a
    later client for the same launch signature can reattach it."""
    sess_file = tmp_path / "sess.json"
    client = AcpClient(
        sys.executable,
        [str(fake_agent)],
        cwd=str(tmp_path),
        name="persist",
        session_id_path=sess_file,
    )
    try:
        await client.prompt("go", timeout=30.0)
    finally:
        await client.close()
    data = json.loads(sess_file.read_text(encoding="utf-8"))
    assert data["sessionId"] == "s1"
    assert data["cwd"] == str(tmp_path)


async def test_forget_session_deletes_persisted_id(tmp_path, monkeypatch):
    """forget_session deletes the persisted session-id file (and evicts the client)
    so the next dispatch starts a fresh session/new instead of session/load-resuming
    the old thread — fresh-both for callers that recreate the workdir per attempt."""
    import plugins.coding_agent as ca

    sess = tmp_path / "sess.json"
    sess.write_text('{"sessionId": "s1"}', encoding="utf-8")
    monkeypatch.setattr(ca, "_session_id_path", lambda spec: sess)
    spec = {
        "name": "proto",
        "command": "proto",
        "args": ["--acp"],
        "workdir": str(tmp_path),
        "env": None,
        "permissions": "auto",
        "allow_kinds": [],
        "deny_kinds": [],
    }
    assert await ca.forget_session(spec) is True
    assert not sess.exists()
    # idempotent: nothing left to clear → False
    assert await ca.forget_session(spec) is False


async def test_persisted_id_ignored_when_agent_lacks_loadsession(fake_agent, tmp_path):
    """A persisted id must NOT trigger session/load if the agent doesn't advertise
    loadSession — the client falls back to a fresh session/new and overwrites it."""
    sess_file = tmp_path / "sess.json"
    sess_file.write_text(
        json.dumps({"sessionId": "OLD", "cwd": str(tmp_path), "command": sys.executable}),
        encoding="utf-8",
    )
    client = AcpClient(
        sys.executable,
        [str(fake_agent)],
        cwd=str(tmp_path),
        name="nolc",
        session_id_path=sess_file,
    )
    try:
        answer = await client.prompt("add a healthz route", timeout=30.0)
    finally:
        await client.close()
    assert answer == "Hello world [ok]"  # ran a normal new-session turn
    assert client._session_id == "s1"  # the new id, not the stale "OLD"
    assert json.loads(sess_file.read_text(encoding="utf-8"))["sessionId"] == "s1"


async def test_close_emits_session_close_before_reaping(fake_agent, tmp_path):
    """close() sends a best-effort session/close while the child is still alive
    (returncode None) — the graceful, spec-aligned shutdown before the SIGTERM."""
    client = AcpClient(sys.executable, [str(fake_agent)], cwd=str(tmp_path), name="close")
    await client.prompt("go", timeout=30.0)
    calls: list[tuple] = []
    orig = client._notify_session

    async def spy(method: str) -> None:
        calls.append((method, client._proc.returncode))
        await orig(method)

    client._notify_session = spy
    await client.close()
    assert ("session/close", None) in calls  # emitted before the process was reaped


async def test_initialize_rejects_unsupported_protocol_version(tmp_path):
    """The agent counters with protocolVersion 2 (unsupported) — the client must
    raise (close the connection) instead of warn-and-continue."""
    script = tmp_path / "v2_agent.py"
    script.write_text(_V2_AGENT, encoding="utf-8")
    client = AcpClient(sys.executable, [str(script)], cwd=str(tmp_path), name="v2")
    try:
        with pytest.raises(AcpError) as ei:
            await client.prompt("hi", timeout=10.0)
    finally:
        await client.close()
    msg = str(ei.value).lower()
    assert "protocol" in msg and "v2" in msg


async def test_agent_thought_chunk_surfaced_not_in_answer(tmp_path):
    """agent_thought_chunk reasoning is routed to thought_callback and never folded
    into the answer text."""
    script = tmp_path / "thought_agent.py"
    script.write_text(_THOUGHT_AGENT, encoding="utf-8")
    thoughts: list[str] = []

    async def on_thought(t: str) -> None:
        thoughts.append(t)

    client = AcpClient(sys.executable, [str(script)], cwd=str(tmp_path), name="thought")
    try:
        answer = await client.prompt("go", thought_callback=on_thought, timeout=30.0)
    finally:
        await client.close()
    assert answer == "the answer"  # thought NOT folded into the answer
    assert thoughts == ["thinking hard"]  # surfaced to the thought callback


# ── permission policy ─────────────────────────────────────────────────────────

_OPTS = [{"optionId": "a", "kind": "allow_once"}, {"optionId": "r", "kind": "reject_once"}]


def _perm(policy, kind, options=None, allow=None, deny=None):
    spec = {
        "name": "x",
        "permissions": policy,
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
    assert _perm("allowlist", "execute") == "r"  # risky → reject option
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
    assert _perm("allowlist", "edit", deny=["edit"]) == "r"  # explicitly denied
    assert _perm("readonly", "edit", allow=["read", "edit"]) == "a"  # explicitly allowed


# ── client cache eviction / teardown ──────────────────────────────────────────


async def test_evict_client_pops_and_closes():
    """evict_client removes the cached client AND awaits close() — a plain pop
    would forget the handle but leave the subprocess alive."""
    spec = {
        "name": "proto",
        "command": "proto",
        "args": ["--acp"],
        "workdir": "/tmp/wt-1",
        "permissions": "allowlist",
        "allow_kinds": [],
        "deny_kinds": [],
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


def test_session_id_path_is_stable_and_keyed_per_signature():
    """The factory derives a session-id path from the cache key — stable for the same
    spec, distinct when the launch signature (e.g. workdir) changes."""
    spec_a = {
        "name": "proto",
        "command": "proto",
        "args": ["--acp"],
        "workdir": "/tmp/wt-a",
        "permissions": "auto",
        "allow_kinds": [],
        "deny_kinds": [],
    }
    spec_b = {**spec_a, "workdir": "/tmp/wt-b"}
    p_a, p_a2, p_b = P._session_id_path(spec_a), P._session_id_path(spec_a), P._session_id_path(spec_b)
    assert p_a == p_a2  # stable for the same signature
    assert p_a != p_b  # workdir is part of the key
    assert p_a.name.endswith(".json") and p_a.parent.name == "acp_sessions"


async def test_evict_client_swallows_close_errors():
    """A close() that raises must not propagate — teardown is best-effort, and the
    cache entry is dropped regardless."""
    spec = {
        "name": "proto",
        "command": "proto",
        "args": [],
        "workdir": "/tmp/wt-2",
        "permissions": "auto",
        "allow_kinds": [],
        "deny_kinds": [],
    }

    class _BadClient:
        async def close(self):
            raise RuntimeError("terminate blew up")

    P._CLIENTS[P._cache_key(spec)] = _BadClient()
    assert await P.evict_client(spec) is True  # did not raise
    assert P._cache_key(spec) not in P._CLIENTS
