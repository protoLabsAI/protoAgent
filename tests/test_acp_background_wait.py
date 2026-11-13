"""A turn parked on a backgrounded command is chased, not returned (bd-572g1).

Claude Code moves a foreground command past its ~10-minute Bash budget to the background
("Command running in background with ID: <id>" in the tool result) and tells the agent it
will be notified when it finishes. In the CLI that notification wakes the agent; over ACP
nothing does — the agent ends its turn with a "waiting" note, and the client used to take
``end_turn`` as final and return that note as the answer (2026-11-12, card bd-bkvgk:
~14 coder sessions in a row, each ending 0.1s before its own notification landed).

These drive a real ``AcpClient`` against a scripted agent subprocess, varying only how the
agent behaves after it announces the backgrounded command.
"""

from __future__ import annotations

import json
import sys

from plugins.coding_agent.acp_client import _UNFINISHED_STOP_REASONS, AcpClient
from plugins.delegates.adapters import _INCOMPLETE_STOP_REASONS, _mark_incomplete

# A minimal ACP agent over stdio. argv[1] picks the scenario; argv[2] logs each
# session/prompt text, one JSON line per prompt, so tests can assert the follow-ups.
_AGENT = r"""
import json, sys

mode, log_path = sys.argv[1], sys.argv[2]
NOTICE = "Command running in background with ID: bash_1"

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": upd}})

def chunk(sid, text):
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

def output_block(text):
    return [{"type": "content", "content": {"type": "text", "text": text}}]

prompts = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params", {})
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1}})
        continue
    if method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "s1"}})
        continue
    if method != "session/prompt":
        continue
    prompts += 1
    with open(log_path, "a") as log:
        log.write(json.dumps(params["prompt"][0]["text"]) + "\n")
    sid = params["sessionId"]
    if mode == "plain":
        chunk(sid, "no long commands here")
    elif prompts == 1:
        # The long command gets backgrounded mid-turn; the agent ends its turn waiting.
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1",
                     "title": "Bash(python -m pytest tests/ -q)", "kind": "execute"})
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
                     "status": "completed", "content": output_block(NOTICE)})
        chunk(sid, "Waiting for the test suite to finish.")
    elif mode == "resolves":
        # Nudged, the agent polls the command (input names the id), sees it done, answers.
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t2", "title": "BashOutput",
                     "kind": "execute", "rawInput": {"bash_id": "bash_1"}})
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t2", "status": "completed",
                     "content": output_block("<status>completed</status> exit code 0 - 12 passed")})
        chunk(sid, "Suite finished: 12 passed.")
    else:  # mode == "stubborn" — keeps ending its turn to wait, forever
        chunk(sid, "Still waiting on the suite.")
    send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""


def _client(tmp_path, mode: str) -> tuple[AcpClient, "object"]:
    script = tmp_path / "agent.py"
    script.write_text(_AGENT)
    log = tmp_path / "prompts.jsonl"
    client = AcpClient(
        sys.executable,
        [str(script), mode, str(log)],
        cwd=str(tmp_path),
        name="coder",
        record_runs=False,
    )
    return client, log


def _prompts(log) -> list[str]:
    return [json.loads(line) for line in log.read_text().splitlines()]


async def test_backgrounded_waiting_turn_gets_one_followup_and_its_answer_wins(tmp_path):
    """(a) The 'waiting' turn is not the answer: one follow-up in the SAME session, and
    only the follow-up's text comes back — the waiting note is not concatenated in."""
    client, log = _client(tmp_path, "resolves")
    try:
        reply = await client.prompt("run the suite", timeout=30.0)
    finally:
        await client.close()

    assert reply == "Suite finished: 12 passed."
    prompts = _prompts(log)
    assert len(prompts) == 2
    assert prompts[0] == "run the suite"
    # The nudge names the command and says waiting-by-ending-the-turn doesn't work here.
    assert "bash_1" in prompts[1]
    assert "still running" in prompts[1]
    # A resolved turn is a normal success — no synthetic marker anywhere.
    assert client.last_stop_reason == "end_turn"
    assert client.unfinished_reason() is None


async def test_an_agent_that_keeps_waiting_gets_the_stop_marker_after_three_followups(tmp_path):
    """(b) The follow-up repeats at most 3 times; after that the turn carries
    ``background_pending`` — a marker callers see — instead of a silent waiting answer."""
    client, log = _client(tmp_path, "stubborn")
    try:
        reply = await client.prompt("run the suite", timeout=30.0)
    finally:
        await client.close()

    assert len(_prompts(log)) == 4  # the original prompt + exactly 3 follow-ups
    assert client.last_stop_reason == "background_pending"
    # The reply is the LAST round's text alone, and the marker rides every caller surface:
    # the stop reason, the outcome classifier (books the run failed, not completed), the
    # board's tapped snapshot — but NOT the retry classifier (a retry is sane).
    assert reply == "Still waiting on the suite."
    assert client.unfinished_reason() == "background_pending"
    assert client.dead_end() is None
    assert client.tapped_result(reply).stop_reason == "background_pending"


async def test_a_turn_without_a_background_notice_gets_no_followup(tmp_path):
    """(c) Agents that never produce the notice are unaffected — one prompt, no nudges."""
    client, log = _client(tmp_path, "plain")
    try:
        reply = await client.prompt("quick question", timeout=30.0)
    finally:
        await client.close()

    assert reply == "no long commands here"
    assert len(_prompts(log)) == 1
    assert client.last_stop_reason == "end_turn"


def test_the_delegate_surface_marks_a_background_pending_reply():
    """The delegate reply (prose in a model's context) spells the marker out, and the
    classification pair stays consistent: every incomplete reason books as unfinished."""
    marked = _mark_incomplete("Waiting for the test suite to finish.", "background_pending")
    assert "incomplete reply" in marked
    assert "waiting note" in marked
    assert set(_INCOMPLETE_STOP_REASONS) <= set(_UNFINISHED_STOP_REASONS)
