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
import time

from plugins.coding_agent.acp_client import _UNFINISHED_STOP_REASONS, AcpClient
from plugins.delegates.adapters import _INCOMPLETE_STOP_REASONS, _mark_incomplete

# A minimal ACP agent over stdio. argv[1] picks the scenario; argv[2] logs each
# session/prompt text, one JSON line per prompt, so tests can assert the follow-ups.
# Every round reports the same usage, so chased turns must show it SUMMED. The notice
# ends its sentence right after the id — the id must come out bare, without the '.'.
_AGENT = r"""
import json, sys

mode, log_path = sys.argv[1], sys.argv[2]
NOTICE = "Command running in background with ID: bash_1. Output is being written to /tmp/bash_1.out"

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
    elif mode == "stall":
        # The nudged round never answers — the chase must die by the turn's own deadline.
        continue
    else:  # mode == "stubborn" — keeps ending its turn to wait, forever
        chunk(sid, "Still waiting on the suite.")
    send({"jsonrpc": "2.0", "id": mid,
          "result": {"stopReason": "end_turn", "usage": {"inputTokens": 100, "outputTokens": 7}}})
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
    # The nudge names the command — the BARE id, not the sentence period after it —
    # and says waiting-by-ending-the-turn doesn't work here.
    assert "bash_1" in prompts[1]
    assert "bash_1." not in prompts[1]
    assert "still running" in prompts[1]
    # A resolved turn is a normal success — no synthetic marker anywhere.
    assert client.last_stop_reason == "end_turn"
    assert client.unfinished_reason() is None
    # Both rounds' usage adds up — the first round is where the coding work happened.
    assert client._turn_usage == {"inputTokens": 200, "outputTokens": 14}


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
    # All four rounds' usage adds up on the parked result too.
    assert client._turn_usage == {"inputTokens": 400, "outputTokens": 28}


async def test_a_followup_that_outlives_the_turn_budget_returns_the_marker_not_an_error(tmp_path):
    """The chase stays inside the turn's EXISTING timeout: each round gets only what is
    left of it, and a round that runs past that comes back as ``background_pending`` —
    before bd-572g1 a normally-ended turn always returned, so the chase must never turn
    one into a raised turn failure."""
    client, log = _client(tmp_path, "stall")
    try:
        reply = await client.prompt("run the suite", timeout=4.0)
    finally:
        await client.close()

    # The follow-up was sent (and received — the agent logs before stalling), timed out
    # against the turn's remaining budget, and degraded to the marker.
    assert len(_prompts(log)) == 2
    assert client.last_stop_reason == "background_pending"
    assert client.unfinished_reason() == "background_pending"
    assert reply == ""  # the waiting note was reset; the stalled round produced nothing
    # The completed first round's usage still reaches telemetry.
    assert client._turn_usage == {"inputTokens": 100, "outputTokens": 7}


async def test_a_spent_deadline_returns_the_marker_without_another_prompt():
    """The between-rounds exit: no time left in the turn's budget means no further
    ``session/prompt``, just the marker (with the chase's accumulated usage)."""
    client = AcpClient.__new__(AcpClient)
    client.name = "coder"
    client._turn_session_id = "s1"
    client._turn_background_pending = {"bash_1"}
    sent = []

    async def never(text, *, timeout):
        sent.append(timeout)
        return {"stopReason": "end_turn"}

    client._prompt_request = never
    result = await client._background_followups(
        {"stopReason": "end_turn", "usage": {"outputTokens": 7}}, time.monotonic() - 1.0
    )
    assert result["stopReason"] == "background_pending"
    assert result["usage"] == {"outputTokens": 7}
    assert sent == []


def _event(text=None, tool_id="t1", raw_input=None):
    """One tool_call_update as ``_handle_update`` hands it to ``_note_background_commands``."""
    update = {"sessionUpdate": "tool_call_update", "toolCallId": tool_id, "status": "completed"}
    if text is not None:
        update["content"] = [{"type": "content", "content": {"type": "text", "text": text}}]
    if raw_input is not None:
        update["rawInput"] = raw_input
    return update


def test_a_finished_poll_of_a_longer_id_does_not_clear_a_prefix_id():
    """``bash_1`` must not be cleared on a prefix hit inside ``bash_10``."""
    client = AcpClient.__new__(AcpClient)
    client._note_background_commands(_event("Command running in background with ID: bash_1"))
    client._note_background_commands(_event("Command running in background with ID: bash_10"))
    client._note_background_commands(
        _event("<status>completed</status> exit code 0", tool_id="t2", raw_input={"bash_id": "bash_10"})
    )
    assert client._turn_background_pending == {"bash_1"}


def test_a_running_poll_with_chatty_stdout_does_not_clear_the_id():
    """A still-running poll whose partial stdout happens to contain completion-ish words
    ('complete', 'failed', even a literal exit code) must not clear — clearing there
    returns the original waiting-answer-accepted bug."""
    client = AcpClient.__new__(AcpClient)
    client._note_background_commands(_event("Command running in background with ID: bash_1"))
    client._note_background_commands(_event(
        "<status>running</status><stdout>test_complete_flow passed; subprocess exit code 1 expected</stdout>",
        tool_id="t2",
        raw_input={"bash_id": "bash_1"},
    ))
    client._note_background_commands(_event(
        "collecting … test_setup_failed deselected, suite not finished",  # bare words, no structure
        tool_id="t3",
        raw_input={"bash_id": "bash_1"},
    ))
    assert client._turn_background_pending == {"bash_1"}


def test_a_notice_ending_its_sentence_stores_the_bare_id_and_a_done_poll_clears_it():
    """'ID: b3k9x2. Output …' must store ``b3k9x2`` — with the period, the poll (which
    names the bare id) could never clear it and a finished command would still be chased."""
    client = AcpClient.__new__(AcpClient)
    client._note_background_commands(_event(
        "Command running in background with ID: b3k9x2. Output is being written to: /tmp/b3k9x2.log"
    ))
    assert client._turn_background_pending == {"b3k9x2"}
    client._note_background_commands(
        _event("<status>completed</status>", tool_id="t2", raw_input={"bash_id": "b3k9x2"})
    )
    assert client._turn_background_pending == set()


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
    # No chase → the single round's usage passes through untouched.
    assert client._turn_usage == {"inputTokens": 100, "outputTokens": 7}


def test_the_delegate_surface_marks_a_background_pending_reply():
    """The delegate reply (prose in a model's context) spells the marker out, and the
    classification pair stays consistent: every incomplete reason books as unfinished."""
    marked = _mark_incomplete("Waiting for the test suite to finish.", "background_pending")
    assert "incomplete reply" in marked
    assert "waiting note" in marked
    assert set(_INCOMPLETE_STOP_REASONS) <= set(_UNFINISHED_STOP_REASONS)
