"""Tests for evals/interruptions.py — the per-source interruption counter (#5518).

The counting core is pure, so most cases pass plain row mappings. The reader cases
build real stores under the test instance root (``conftest`` isolates it) — no server.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from evals import interruptions
from evals.interruptions import SOURCES, count_interruptions

END = datetime(2026, 11, 22, 6, 0, tzinfo=UTC)


def _at(hours_before_end: float) -> str:
    return (END - timedelta(hours=hours_before_end)).isoformat()


def _fixture(age: float = 1.0) -> dict:
    """Records for every source, all ``age`` hours before the window end."""
    when = _at(age)
    return {
        "activity": [
            # scheduler: a job with no chat to report to (reported unknown) — its feed row.
            {"created_at": when, "origin": "scheduler", "task_id": "t-feed", "context_id": "job:s1"},
            # scheduler: posted its result card AND has a feed row — one card, not two.
            {"created_at": when, "origin": "scheduler", "task_id": "t-posted", "context_id": "job:s2"},
            # scheduler: a NO_UPDATE run — its feed row is not a card.
            {"created_at": when, "origin": "scheduler", "task_id": "t-quiet", "context_id": "job:s3"},
            {"created_at": when, "origin": "inbox", "task_id": "t-in1", "context_id": "job:i1"},
            {"created_at": when, "origin": "inbox", "task_id": "t-in2", "context_id": "job:i2"},
            {"created_at": when, "origin": "watch", "task_id": "t-w1", "context_id": "job:w1"},
            # Not unattended sources: never counted.
            {"created_at": when, "origin": "operator", "task_id": "t-op", "context_id": "system:activity"},
            {"created_at": when, "origin": "a2a", "task_id": "t-peer", "context_id": "peer-1"},
            # The briefing of a background completion the job ledger already counts.
            {"created_at": when, "origin": "background-resume", "task_id": "t-br", "context_id": "job:b1"},
        ],
        "scheduler_runs": [
            {"job_id": "digest", "task_id": "t-posted", "ended_at": when, "reported": 1},
            {"job_id": "digest", "task_id": "t-quiet", "ended_at": when, "reported": 0},
            {"job_id": "digest", "task_id": "t-feed", "ended_at": when, "reported": None},
            # Posted to its chat with no feed row of its own.
            {"job_id": "report", "task_id": "t-chat-only", "ended_at": when, "reported": 1},
            # A watch reaction that ran in its chat: the ledger is its only record.
            {"job_id": "watch-abc", "task_id": "t-w2", "ended_at": when, "reported": 1},
            {"job_id": "watch-abc", "task_id": "t-w3", "ended_at": when, "reported": None},
        ],
        "background_jobs": [
            {"id": "bg1", "status": "completed", "completed_at": when},
            {"id": "bg2", "status": "failed", "completed_at": when},
            {"id": "bg3", "status": "running", "completed_at": None},
        ],
        "inbox_items": [
            {"id": 1, "created_at": when, "needs_human": True},
            {"id": 2, "created_at": when, "needs_human": False, "retries_exhausted": True},
            {"id": 3, "created_at": when, "needs_human": False},
        ],
        "job_sessions": [
            {"job_id": "digest", "context_id": "job:s1"},
            {"job_id": "watch-abc", "context_id": "job:w9"},
        ],
        "inbox_executions": [{"item_id": 1, "context_id": "job:i1"}],
        "decisions": [
            {"id": "d1", "session_id": "job:s1", "interrupt_id": "x", "created_at": when},
            {"id": "d2", "session_id": "job:w9", "interrupt_id": "x", "created_at": when},
            {"id": "d3", "session_id": "job:i1", "interrupt_id": "x", "created_at": when},
            {"id": "d4", "session_id": "background:bg1", "interrupt_id": "x", "created_at": when},
            # Session known only from the Activity log's context.
            {"id": "d5", "session_id": "job:w1", "interrupt_id": "x", "created_at": when},
            # Restart recovery names the job, not a session.
            {
                "id": "d6", "session_id": "", "created_at": when,
                "interrupt_id": "restart-recovery:" + json.dumps({"job_id": "digest"}),
            },
            # An operator's own chat, and the instance spend cap: no unattended source.
            {"id": "d7", "session_id": "chat-123", "interrupt_id": "x", "created_at": when},
            {"id": "d8", "session_id": "", "interrupt_id": "spend-cap:2026-11", "created_at": when},
        ],
    }


EXPECTED = {
    # t-feed + t-posted (once) + t-chat-only; t-quiet is suppressed.
    "scheduler": {"activity_cards": 3, "needs_you": 2},  # d1, d6
    "inbox": {"activity_cards": 2, "needs_you": 3},  # items 1, 2 + d3
    "watch": {"activity_cards": 2, "needs_you": 2},  # t-w1 feed row + t-w2 posted; d2, d5
    "background": {"activity_cards": 2, "needs_you": 1},  # bg1, bg2; d4
}


def test_counts_cards_and_needs_you_per_source():
    assert count_interruptions(window_end=END, **_fixture()) == EXPECTED


def test_records_older_than_24_hours_are_not_counted():
    counts = count_interruptions(window_end=END, **_fixture(age=25))
    assert counts == {s: {"activity_cards": 0, "needs_you": 0} for s in SOURCES}


def test_window_edges_and_future_records():
    def cards(created_at):
        rows = [{"created_at": created_at, "origin": "inbox", "task_id": "t"}]
        return count_interruptions(window_end=END, activity=rows)["inbox"]["activity_cards"]

    assert cards(_at(0)) == 1  # the end is inclusive
    assert cards(_at(23.99)) == 1
    assert cards(_at(24)) == 0  # the start is exclusive
    assert cards(_at(-0.01)) == 0  # after the window
    assert cards("") == 0 and cards(None) == 0 and cards("not a time") == 0
    # A naive stamp is UTC; a zoned one is converted.
    assert cards("2026-11-22T05:00:00") == 1
    assert cards("2026-11-22T00:00:00-08:00") == 0  # 08:00 UTC, after the end
    assert cards("2026-11-22T05:00:00Z") == 1


def test_source_without_records_reports_zero():
    counts = count_interruptions(
        window_end=END, activity=[{"created_at": _at(1), "origin": "watch", "task_id": "t"}]
    )
    assert set(counts) == set(SOURCES)
    assert counts["watch"] == {"activity_cards": 1, "needs_you": 0}
    for source in ("scheduler", "inbox", "background"):
        assert counts[source] == {"activity_cards": 0, "needs_you": 0}
    assert count_interruptions(window_end=END) == {s: {"activity_cards": 0, "needs_you": 0} for s in SOURCES}


def test_window_end_must_be_a_time():
    with pytest.raises(ValueError):
        count_interruptions(window_end=None)


def test_module_docstring_names_each_record():
    doc = interruptions.__doc__
    for record in ("activity", "occurrences", "reported", "background_jobs", "inbox", "decisions",
                   "job_sessions", "inbox_executions"):
        assert f"``{record}``" in doc, record


# ── the reader ────────────────────────────────────────────────────────────────────


def test_reader_reports_zero_when_the_instance_has_no_stores():
    from infra.paths import instance_paths

    counts = interruptions.read_interruptions(END)
    assert counts == {s: {"activity_cards": 0, "needs_you": 0} for s in SOURCES}
    # Read-only: counting must not create a store the instance never had.
    for name in ("activity", "inbox", "background", "scheduler", "decisions"):
        assert not instance_paths().store(name).exists()


def test_reader_counts_this_instances_stores():
    from activity import ActivityLog
    from graph.decisions import DecisionStore
    from inbox import InboxStore
    from infra.paths import instance_paths

    paths = instance_paths()
    log = ActivityLog(str(paths.store("activity") / "agent.db"))
    log.add(context_id="job:w1", origin="watch", text="disk is full", task_id="t-w1")
    log.add(context_id="system:activity", origin="operator", text="hello", task_id="t-op")

    inbox = InboxStore(str(paths.store("inbox") / "agent.db"))
    inbox.add("card is stuck", priority="now", source="project_board")  # operator-class producer
    plain = inbox.add("fyi", priority="later", source="webhook")
    identity = inbox.execution_identity(int(plain["id"]))

    decisions = DecisionStore()
    decisions.create(session_id=identity["context_id"], question="Ship it?", options=["yes", "no"])
    decisions.create(session_id="job:w1", question="Restart it?", options=["yes", "no"])
    decisions.create(session_id="chat-1", question="Operator chat", options=["yes", "no"])

    now = datetime.now(UTC)
    counts = interruptions.read_interruptions(now + timedelta(minutes=1))
    assert counts["watch"] == {"activity_cards": 1, "needs_you": 1}
    assert counts["inbox"] == {"activity_cards": 0, "needs_you": 2}
    assert counts["scheduler"] == {"activity_cards": 0, "needs_you": 0}
    assert counts["background"] == {"activity_cards": 0, "needs_you": 0}
    # The same stores, a day later: everything has aged out.
    later = interruptions.read_interruptions(now + timedelta(hours=25))
    assert later == {s: {"activity_cards": 0, "needs_you": 0} for s in SOURCES}


def test_cli_prints_the_counts(capsys):
    assert interruptions.main(["--end", END.isoformat()]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["window_end"] == END.isoformat()
    assert set(out["sources"]) == set(SOURCES)
