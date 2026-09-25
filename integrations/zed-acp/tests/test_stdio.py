"""The shim as Zed runs it: a subprocess speaking ACP on stdio, driven by the SDK's
client side. Proves stdout carries only protocol (a stray print would break framing)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import fake_a2a as fa
import pytest
from acp import PROTOCOL_VERSION, spawn_agent_process, text_block


class Client:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        self.updates.append(update.model_dump(by_alias=True, exclude_none=True))

    def on_connect(self, conn: Any) -> None:
        pass


async def test_stdio_round_trip(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    with fa.FakeA2A(roots={"proj": "/abs/proj"}) as fake:
        fake.script = lambda ctx, msg: [
            fa.task(ctx),
            fa.tool(ctx, "c1", "read_file", "started", args='{"project": "proj", "path": "README.md", "offset": 1}'),
            fa.tool(ctx, "c1", "read_file", "completed", result="# hi"),
            fa.text(ctx, "Done.", append=False),
            fa.done(ctx),
        ]
        client = Client()
        args = ["-m", "protoagent_acp", "--url", fake.url, "--token-file", str(token_file)]
        async with spawn_agent_process(client, sys.executable, *args) as (conn, _proc):
            init = await conn.initialize(protocol_version=PROTOCOL_VERSION)
            assert init.protocol_version == PROTOCOL_VERSION
            assert {m.id for m in init.auth_methods} >= {"protoagent-login"}
            sess = await conn.new_session(cwd="/abs/proj", mcp_servers=[])
            resp = await conn.prompt(session_id=sess.session_id, prompt=[text_block("read the readme")])
    assert resp.stop_reason == "end_turn"
    kinds = [u["sessionUpdate"] for u in client.updates]
    assert kinds == ["tool_call", "tool_call_update", "agent_message_chunk"]
    assert client.updates[0]["locations"] == [{"path": "/abs/proj/README.md", "line": 1}]


def _approval_script(ctx, msg):
    if not (msg.get("metadata") or {}).get("hitl_resume"):
        return [
            fa.task(ctx),
            fa.tool(ctx, "c1", "run_command", "started", args='{"project": "p", "command": "git status"}'),
            fa.hitl(ctx, {"kind": "approval", "title": "Approve shell command?", "detail": "git status"}),
        ]
    return [fa.text(ctx, "resumed: " + msg["parts"][0]["text"], append=False), fa.done(ctx)]


@pytest.mark.parametrize(("flag", "word"), [([], "denied"), (["--approve"], "approved"), (["--approve-always"], "approved")])
def test_harness_denies_by_default_and_approves_with_flag(flag, word):
    harness = Path(__file__).resolve().parents[1] / "scripts" / "acp_harness.py"
    with fa.FakeA2A(token=None) as fake:
        fake.script = _approval_script
        out = subprocess.run(
            [sys.executable, str(harness), "--prompt", "run it", *flag, "--", "--url", fake.url],
            capture_output=True, text=True, timeout=60,
        )
    assert out.returncode == 0, out.stderr
    expect = {"": "→ DENY (deny)", "--approve": "→ ALLOW (approve)", "--approve-always": "→ ALLOW ALWAYS (approve_session)"}
    assert expect[flag[0] if flag else ""] in out.stdout
    assert f"resumed: {word}" in out.stdout
    assert fake.requests[1]["parts"][0]["text"] == word
    assert bool((fake.requests[1].get("metadata") or {}).get("bypass_permissions")) == (flag == ["--approve-always"])


def test_harness_prints_a_failed_turn_and_exits_nonzero():
    harness = Path(__file__).resolve().parents[1] / "scripts" / "acp_harness.py"
    with fa.FakeA2A(token=None) as fake:
        fake.script = lambda ctx, msg: [fa.task(ctx), fa.done(ctx, state="TASK_STATE_FAILED", reason="gateway down")]
        out = subprocess.run([sys.executable, str(harness), "--prompt", "x", "--", "--url", fake.url],
                             capture_output=True, text=True, timeout=60)
    assert out.returncode == 1
    assert "<<< error -32603: protoAgent: gateway down" in out.stdout


def test_harness_send_now_steers_through_the_real_stdio_shim():
    harness = Path(__file__).resolve().parents[1] / "scripts" / "acp_harness.py"
    with fa.FakeA2A(token=None) as fake:
        fake.script = lambda ctx, msg: [
            fa.task(ctx),
            fa.text(ctx, "Reading A.", append=False),
            fa.after_steer(fake, lambda t: fa.text(ctx, f" Now: {t}", append=True)),
            fa.done(ctx),
        ]
        out = subprocess.run(
            [sys.executable, str(harness), "--prompt", "long task", "--send-now", "do B", "--send-now-after", "0.5",
             "--", "--url", fake.url, "--steer-grace", "3"],
            capture_output=True, text=True, timeout=60,
        )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "stopReason: cancelled" in out.stdout and "stopReason: end_turn" in out.stdout
    assert "↪ steering: do B" in out.stdout and "Now: do B" in out.stdout
    assert fake.cancels == [] and len(fake.requests) == 1


def test_harness_list_and_load_through_stdio():
    harness = Path(__file__).resolve().parents[1] / "scripts" / "acp_harness.py"
    with fa.FakeA2A(token=None) as fake:
        fake.sessions = [{"session_id": "chat-zed-1-aaa", "last_updated": "2026-09-24T10:00:00", "turn_count": 1}]
        fake.turns["chat-zed-1-aaa"] = [{
            "task_id": "t0", "state": "TASK_STATE_COMPLETED", "text": "The answer is 42.", "status": {}, "artifacts": [],
            "history": [{"role": "ROLE_USER", "parts": [{"text": "What is the answer?"}]}],
        }]
        fake.script = lambda ctx, msg: [fa.task(ctx), fa.text(ctx, "Still 42.", append=False), fa.done(ctx)]
        out = subprocess.run(
            [sys.executable, str(harness), "--list", "--load", "chat-zed-1-aaa", "--prompt", "and again?", "--", "--url", fake.url],
            capture_output=True, text=True, timeout=60,
        )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "chat-zed-1-aaa" in out.stdout and "'What is the answer?'" in out.stdout
    assert "user_message_chunk" in out.stdout and "The answer is 42." in out.stdout
    assert "Still 42." in out.stdout and fake.requests[0]["contextId"] == "chat-zed-1-aaa"


def test_handoff_claim_through_stdio_replays_after_the_session_new_response():
    """A client drops session/update for a session id it hasn't been handed yet, so the
    claimed chat's replay must arrive AFTER the session/new response."""
    harness = Path(__file__).resolve().parents[1] / "scripts" / "acp_harness.py"
    with fa.FakeA2A(token=None) as fake:
        sid = "chat-1790294944966-qkf93w"
        fake.handoff = {"session_id": sid, "project": None, "path": None, "line": None, "title": "Console chat"}
        fake.turns[sid] = [{"task_id": "t0", "state": "TASK_STATE_COMPLETED", "text": "Earlier answer.", "status": {},
                            "artifacts": [], "history": [{"role": "ROLE_USER", "parts": [{"text": "Earlier question"}]}]}]
        fake.script = lambda ctx, msg: [fa.task(ctx), fa.text(ctx, "Continued.", append=False), fa.done(ctx)]
        out = subprocess.run([sys.executable, str(harness), "--prompt", "carry on", "--", "--url", fake.url],
                             capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    new_at = out.stdout.index(f"session/new → {sid}")
    assert out.stdout.index("Earlier question") > new_at
    assert out.stdout.index("Continuing your console chat") > new_at
    assert "Continued." in out.stdout and fake.requests[0]["contextId"] == sid
