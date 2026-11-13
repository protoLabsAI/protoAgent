"""A turn parked on a backgrounded command is chased, not returned (bd-572g1).

Claude Code moves a foreground command past its ~10-minute Bash budget to the background
("Command running in background with ID: <id>. Output is being written to:
…/tasks/<id>.output. … To check interim output, use Read on that file path." in the tool
result) and tells the agent it will be notified when it finishes. In the CLI that
notification wakes the agent; over ACP nothing does — the agent ends its turn with a
"waiting" note, and the client used to take ``end_turn`` as final and return that note as
the answer (2026-11-12, card bd-bkvgk: ~14 coder sessions in a row, each ending 0.1s
before its own notification landed).

These drive a real ``AcpClient`` against a scripted agent subprocess, varying only how the
agent behaves after it announces the backgrounded command.
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from plugins.coding_agent.acp_client import AcpClient

# A minimal ACP agent over stdio. argv[1] picks the scenario; argv[2] logs each
# session/prompt text, one JSON line per prompt, so tests can assert the follow-ups.
# Every round reports the same usage, so chased turns must show it SUMMED. The notice
# mirrors the real one (bd-bkvgk transcripts): it ends its sentence right after the id —
# which must come out bare, without the '.' — and embeds the id in the output file path,
# which is how a Read of that file references the command.
_AGENT = r"""
import json, sys

mode, log_path = sys.argv[1], sys.argv[2]
OUT_FILE = "/tmp/claude-tasks/bash_1.output"
NOTICE = ("Command running in background with ID: bash_1. Output is being written to: "
          + OUT_FILE + ". You will be notified when it completes. To check interim "
          "output, use Read on that file path.")

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": upd}})

def chunk(sid, text):
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

def tool(sid, tid, title, text, raw=None, status="completed"):
    update(sid, {"sessionUpdate": "tool_call", "toolCallId": tid, "title": title,
                 "kind": "execute", **({"rawInput": raw} if raw else {})})
    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": status,
                 "content": [{"type": "content", "content": {"type": "text", "text": text}}]})

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
        if mode == "many_tools":
            # A first round with MORE tool calls than the ref sidecar's 128-entry cap,
            # all before the long command is backgrounded.
            for i in range(130):
                tool(sid, "f%d" % i, "Read prep step %d" % i, "ok")
        # The long command gets backgrounded mid-turn; the agent reads interim output
        # (plain stdout, the id only in the file path) and STILL ends its turn waiting —
        # the exact bd-bkvgk shape. The interim read must never count as completion.
        tool(sid, "t1", "Bash(python -m pytest tests/ -q)", NOTICE)
        tool(sid, "t2", "Read", "collecting 312 items ...", raw={"file_path": OUT_FILE})
        chunk(sid, "Waiting for the test suite to finish.")
    elif mode in ("resolves", "many_tools"):
        # Nudged, the agent polls its background-task output tool: the id rides only the
        # START event's rawInput, the structural done sign only the update's output.
        tool(sid, "t3", "BashOutput", "<status>completed</status> exit code 0 - 12 passed",
             raw={"bash_id": "bash_1"})
        chunk(sid, "Suite finished: 12 passed.")
    elif mode == "reads_early" and prompts == 2:
        # First nudge: the agent only Reads the output file — the EARLY output of the
        # still-running command, plain stdout — and ends on ANOTHER waiting note.
        tool(sid, "t3", "Read", "collecting 312 items ...", raw={"file_path": OUT_FILE})
        chunk(sid, "Still waiting on the suite.")
    elif mode == "reads_early":
        # Second nudge: the structural poll the nudge points the agent at.
        tool(sid, "t4", "BashOutput", "<status>completed</status> exit code 0 - 12 passed",
             raw={"bash_id": "bash_1"})
        chunk(sid, "Suite finished: 12 passed.")
    elif mode == "stall":
        # The nudged round never answers — the chase must die by the turn's own deadline.
        continue
    else:  # mode == "stubborn" — checks, is SHOWN still-running, ends waiting anyway
        tool(sid, "t%d0" % prompts, "Bash(sleep 45; tail " + OUT_FILE + ")",
             "<tool_use_error>Blocked: sleep 45 followed by: tail " + OUT_FILE + "</tool_use_error>",
             status="failed")
        tool(sid, "t%d1" % prompts, "Read", "collecting 312 items ...",
             raw={"file_path": OUT_FILE})
        tool(sid, "t%d2" % prompts, "BashOutput", "<status>running</status>",
             raw={"bash_id": "bash_1"})
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


async def test_a_nudged_read_of_early_output_plus_a_waiting_note_is_not_accepted(tmp_path):
    """A nudged round that only Reads the command's output file sees plain stdout —
    early output of a still-running command, indistinguishable from a finished run's —
    so ending that round on another waiting note must NOT clear the command: accepting
    checked-then-answered returned that second note as the answer (review finding on
    this PR). The chase goes on until the structural poll, and ITS round's text wins."""
    client, log = _client(tmp_path, "reads_early")
    try:
        reply = await client.prompt("run the suite", timeout=30.0)
    finally:
        await client.close()

    assert reply == "Suite finished: 12 passed."
    # The original prompt, the nudge answered by read-then-wait, and one more nudge.
    assert len(_prompts(log)) == 3
    assert client.last_stop_reason == "end_turn"
    assert client.unfinished_reason() is None
    # All three rounds' usage adds up.
    assert client._turn_usage == {"inputTokens": 300, "outputTokens": 21}


async def test_a_first_round_past_the_ref_cap_still_lets_the_followup_poll_clear(tmp_path):
    """The tool-ref sidecar evicts its OLDEST entries past the 128-entry cap instead of
    refusing new ones: after a first round with more tool calls than the cap, the
    follow-up poll's start event — the only event carrying the id, in its rawInput —
    must still be recorded, or its '<status>completed</status>' update could never be
    matched to the id and the finished command would be chased all the way to
    ``background_pending`` (review finding on this PR)."""
    client, log = _client(tmp_path, "many_tools")
    try:
        reply = await client.prompt("run the suite", timeout=30.0)
    finally:
        await client.close()

    assert reply == "Suite finished: 12 passed."
    assert len(_prompts(log)) == 2  # the one nudge resolved it — no chase to the marker
    assert client.last_stop_reason == "end_turn"
    assert client.unfinished_reason() is None


async def test_an_agent_that_keeps_waiting_gets_the_stop_marker_after_three_followups(tmp_path):
    """(b) The follow-up repeats at most 3 times; after that the turn carries
    ``background_pending`` — a marker callers see — instead of a silent waiting answer.
    Each nudged round here checks the command every way SHORT of a structural done
    sign — a failed (blocked) command naming it, a Read of its interim output, a poll
    structurally showing it still running — and none of those may clear it."""
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
    one into a raised turn failure.

    ~3-4s of REAL time, deliberately: the per-round deadline runs on the event loop's
    real clock inside the request timeout, which a virtual clock can't drive, and there
    is nothing event-based to wait on — the point is that NOTHING arrives and the client
    must time out by itself. The 4s budget must also comfortably outlast subprocess
    spawn + handshake + the instant first round under CI load, or the follow-up is never
    sent and the wrong exit (the spent-deadline one below) is asserted."""
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


def _event(text=None, tool_id="t1", raw_input=None, status="completed"):
    """One tool_call_update as ``_handle_update`` hands it to ``_note_background_commands``."""
    update = {"sessionUpdate": "tool_call_update", "toolCallId": tool_id, "status": status}
    if text is not None:
        update["content"] = [{"type": "content", "content": {"type": "text", "text": text}}]
    if raw_input is not None:
        update["rawInput"] = raw_input
    return update


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        pytest.param(
            [
                dict(text="Command running in background with ID: bash_1"),
                dict(text="Command running in background with ID: bash_10"),
                dict(text="<status>completed</status> exit code 0", tool_id="t2",
                     raw_input={"bash_id": "bash_10"}),
            ],
            {"bash_1"},
            id="a-finished-bash_10-never-clears-prefix-bash_1",
        ),
        pytest.param(
            [
                dict(text="Command running in background with ID: bash_1"),
                # Still-running poll whose partial stdout carries completion-ish words
                # ('complete', 'failed', a literal exit code) — clearing there returns
                # the original waiting-answer-accepted bug.
                dict(text="<status>running</status><stdout>test_complete_flow passed; "
                          "subprocess exit code 1 expected</stdout>",
                     tool_id="t2", raw_input={"bash_id": "bash_1"}),
                # Bare words with no structure never clear either.
                dict(text="collecting … test_setup_failed deselected, suite not finished",
                     tool_id="t3", raw_input={"bash_id": "bash_1"}),
            ],
            {"bash_1"},
            id="running-status-and-bare-words-never-clear",
        ),
        pytest.param(
            [
                # 'ID: b3k9x2. Output …' must store the BARE id — with the period kept,
                # the done poll (which names ``b3k9x2``) could never clear it.
                dict(text="Command running in background with ID: b3k9x2. "
                          "Output is being written to: /tmp/b3k9x2.log"),
                dict(text="<status>completed</status>", tool_id="t2",
                     raw_input={"bash_id": "b3k9x2"}),
            ],
            set(),
            id="sentence-period-notice-stores-the-bare-id",
        ),
        pytest.param(
            [
                dict(text="Command running in background with ID: bash_1. Output is "
                          "being written to: /tmp/tasks/bash_1.output."),
                # A completed Read of the output file: plain stdout, no structural sign.
                # It must NEVER clear — a finished run's stdout is indistinguishable
                # from the early output of one still running.
                dict(text="===== 12 passed in 612.41s =====", tool_id="t2",
                     raw_input={"file_path": "/tmp/tasks/bash_1.output"}),
            ],
            {"bash_1"},
            id="a-plain-stdout-file-read-never-clears",
        ),
    ],
)
def test_clearing_needs_structure_and_the_exact_id(events, expected):
    """Clearing is structural, exact-id, and veto-respecting — and is the ONLY way an
    id is accounted for. One table so the variants stay side by side (tests drive the
    handler on a ``__new__``-built client exactly as ``_handle_update`` would, without
    a subprocess per regex case)."""
    client = AcpClient.__new__(AcpClient)
    for event in events:
        client._note_background_commands(_event(**event))
    assert client._turn_background_pending == expected


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
