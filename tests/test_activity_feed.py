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


def test_resumed_chat_turn_carries_state_and_error(tmp_path, monkeypatch):
    # A server-fired turn that CRASHED must arrive labelled as a failure, carrying the
    # reason. It used to publish only its partial narration, so the console rendered a
    # normal bubble that trailed off mid-sentence — indistinguishable from the agent
    # finishing, while the real cause sat unread in the task's terminal status.message.
    monkeypatch.setattr(server.STATE, "activity_log", ActivityLog(str(tmp_path / "a.db")))
    published: list = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))

    server._a2a_terminal(
        TurnOutcome(
            task_id="t10",
            context_id="chat-xyz",
            state="failed",
            text="Grail Knight picks the ball up.",
            origin="scheduler",
            trigger="bloodbowl-turn-h1t1-away-turn",
            error="Recursion limit of 200 reached without hitting a stop condition.",
        )
    )
    resumed = next((d for ev, d in published if ev == "chat.resumed"), None)
    assert resumed is not None
    assert resumed["state"] == "failed"
    assert "Recursion limit of 200" in resumed["error"]
    assert resumed["text"] == "Grail Knight picks the ball up."  # the partial is kept, not dropped


def test_resumed_chat_turn_publishes_a_failure_that_said_nothing(tmp_path, monkeypatch):
    # The case that used to vanish: a turn that crashed BEFORE emitting any text. The old
    # empty-text guard returned early, so nothing reached the console at all — the operator
    # saw the typing indicator clear and no trace that a turn had ever run.
    monkeypatch.setattr(server.STATE, "activity_log", ActivityLog(str(tmp_path / "a.db")))
    published: list = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))

    server._a2a_terminal(
        TurnOutcome(
            task_id="t11",
            context_id="chat-xyz",
            state="failed",
            text="",
            origin="scheduler",
            trigger="nightly",
            error="the model gateway refused the request",
        )
    )
    resumed = next((d for ev, d in published if ev == "chat.resumed"), None)
    assert resumed is not None
    assert resumed["state"] == "failed"
    assert resumed["error"] == "the model gateway refused the request"
    assert resumed["text"] == ""


def test_resumed_chat_turn_still_drops_a_silent_completed_turn(tmp_path, monkeypatch):
    # The restraint control for the pair above — widening the guard must not make an
    # ORDINARY quiet turn publish an empty bubble. A completed turn with nothing to say
    # is still dropped; so is a failure with no reason to give.
    monkeypatch.setattr(server.STATE, "activity_log", ActivityLog(str(tmp_path / "a.db")))
    published: list = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))

    server._a2a_terminal(
        TurnOutcome(task_id="t12", context_id="chat-xyz", state="completed", text="  ", origin="scheduler")
    )
    server._a2a_terminal(
        TurnOutcome(task_id="t13", context_id="chat-xyz", state="failed", text="", origin="scheduler", error="")
    )
    assert not any(ev == "chat.resumed" for ev, _ in published)


def test_resumed_chat_turn_defaults_to_completed(tmp_path, monkeypatch):
    # An ordinary resume keeps its old shape plus an explicit state, so a console that
    # branches on it treats the untouched happy path as a success.
    monkeypatch.setattr(server.STATE, "activity_log", ActivityLog(str(tmp_path / "a.db")))
    published: list = []
    monkeypatch.setattr(server._event_bus, "publish", lambda ev, data: published.append((ev, data)))

    server._a2a_terminal(
        TurnOutcome(task_id="t14", context_id="chat-xyz", state="completed", text="done", origin="scheduler")
    )
    resumed = next((d for ev, d in published if ev == "chat.resumed"), None)
    assert resumed is not None
    assert resumed["state"] == "completed"
    assert resumed["error"] == ""
