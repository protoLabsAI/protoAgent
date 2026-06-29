"""ADR 0022: the Activity provenance feed — store + terminal-hook wiring."""

from __future__ import annotations

import server
from a2a_impl.executor import TurnOutcome
from activity.store import ActivityLog


def test_log_add_and_recent_newest_first(tmp_path):
    log = ActivityLog(str(tmp_path / "a.db"))
    log.add(context_id="system:activity", origin="scheduler", trigger="daily-brief", text="all green")
    log.add(context_id="system:activity", origin="inbox", trigger="ops@", priority="now", text="deploy ok")
    rows = log.recent()
    assert [r["origin"] for r in rows] == ["inbox", "scheduler"]  # newest first
    assert rows[0]["trigger"] == "ops@" and rows[0]["priority"] == "now"


def test_log_drops_empty_text(tmp_path):
    log = ActivityLog(str(tmp_path / "a.db"))
    assert log.add(context_id="c", origin="x", text="   ") is None
    assert log.recent() == []


def test_log_empty_origin_becomes_operator(tmp_path):
    log = ActivityLog(str(tmp_path / "a.db"))
    log.add(context_id="c", origin="", text="a reply")
    assert log.recent()[0]["origin"] == "operator"


def test_terminal_hook_logs_provenance_and_tags_event(tmp_path, monkeypatch):
    log = ActivityLog(str(tmp_path / "a.db"))
    monkeypatch.setattr(server.STATE, "activity_log", log)
    published: list = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))

    out = TurnOutcome(
        task_id="t1",
        context_id=server.ACTIVITY_CONTEXT,
        state="completed",
        text="<scratch_pad>thinking</scratch_pad>Overnight: 3 PRs merged.",
        origin="scheduler",
        trigger="daily-brief",
        priority="",
    )
    server._a2a_terminal(out)

    rows = log.recent()
    assert rows and rows[0]["origin"] == "scheduler" and rows[0]["trigger"] == "daily-brief"
    assert rows[0]["text"] == "Overnight: 3 PRs merged."  # scratch_pad stripped via extract_output
    # The live event carries provenance too. (A terminal turn also publishes a
    # `turn.usage` event — ADR 0051 — so match the activity.message by topic, not order.)
    activity = next((d for ev, d in published if ev == "activity.message"), None)
    assert activity is not None
    assert activity["origin"] == "scheduler" and activity["trigger"] == "daily-brief"


def test_terminal_hook_ignores_non_activity_context(tmp_path, monkeypatch):
    log = ActivityLog(str(tmp_path / "a.db"))
    monkeypatch.setattr(server.STATE, "activity_log", log)
    server._a2a_terminal(TurnOutcome(task_id="t", context_id="a-chat", state="completed", text="hi"))
    assert log.recent() == []


def test_terminal_hook_surfaces_scheduler_resume_into_chat(tmp_path, monkeypatch):
    # bd-k02: a wait/scheduled resume that lands in a CHAT session (not Activity)
    # is pushed as chat.resumed so an open chat tab can show it live.
    log = ActivityLog(str(tmp_path / "a.db"))
    monkeypatch.setattr(server.STATE, "activity_log", log)
    published: list = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))

    server._a2a_terminal(
        TurnOutcome(
            task_id="t9",
            context_id="chat-xyz",
            state="completed",
            text="Ship arrived; sold the ore.",
            origin="scheduler",
            trigger="wait-resume",
        )
    )
    resumed = next((d for ev, d in published if ev == "chat.resumed"), None)
    assert resumed is not None
    assert resumed["session_id"] == "chat-xyz"
    assert resumed["text"] == "Ship arrived; sold the ore."  # extract_output applied
    assert resumed["task_id"] == "t9"
    assert log.recent() == []  # a chat-session turn, not logged to the Activity feed


def test_terminal_hook_skips_non_scheduler_chat_turn(tmp_path, monkeypatch):
    # An operator/A2A chat turn the browser already streamed must NOT be re-pushed.
    log = ActivityLog(str(tmp_path / "a.db"))
    monkeypatch.setattr(server.STATE, "activity_log", log)
    published: list = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))
    server._a2a_terminal(
        TurnOutcome(
            task_id="t",
            context_id="chat-xyz",
            state="completed",
            text="<output>hi</output>",
            origin="operator",
        )
    )
    assert not any(ev == "chat.resumed" for ev, _ in published)
