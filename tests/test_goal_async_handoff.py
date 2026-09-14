"""Async handoff — a goal drive PAUSES on a self-resume trigger instead of spinning (ADR 0079).

The failure this fixes: a long, delegated goal (roxy's marketing spike) burned its whole
iteration budget synchronously and gave up (`exhausted`) because it couldn't hand the async
work off and yield. Now, when the agent has queued a watch/schedule that resumes this session,
the drive loop pauses (goal stays active) and the trigger's fire continues it later.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

import runtime.state as rs

chat_mod = importlib.import_module("server.chat")


# ── unit: _awaiting_self_resume ────────────────────────────────────────────
@pytest.fixture
def clear_triggers(monkeypatch):
    monkeypatch.setattr(rs.STATE, "watch_controller", None, raising=False)
    monkeypatch.setattr(rs.STATE, "scheduler", None, raising=False)
    yield


def test_awaiting_false_when_nothing_queued(clear_triggers):
    assert chat_mod._awaiting_self_resume("s") is False
    assert chat_mod._awaiting_self_resume("") is False


def test_awaiting_true_for_active_watch_on_this_session(clear_triggers, monkeypatch):
    w = SimpleNamespace(status="active", run_session="s")
    monkeypatch.setattr(rs.STATE, "watch_controller", SimpleNamespace(list_watches=lambda: [w]), raising=False)
    assert chat_mod._awaiting_self_resume("s") is True
    # ...but not for a watch targeting a different session, or a finished watch
    other = SimpleNamespace(status="active", run_session="other")
    met = SimpleNamespace(status="met", run_session="s")
    monkeypatch.setattr(rs.STATE, "watch_controller", SimpleNamespace(list_watches=lambda: [other, met]), raising=False)
    assert chat_mod._awaiting_self_resume("s") is False


def test_awaiting_true_for_pending_schedule_on_this_session(clear_triggers, monkeypatch):
    job = SimpleNamespace(context_id="s")
    monkeypatch.setattr(rs.STATE, "scheduler", SimpleNamespace(list_jobs=lambda: [job]), raising=False)
    assert chat_mod._awaiting_self_resume("s") is True
    monkeypatch.setattr(
        rs.STATE, "scheduler", SimpleNamespace(list_jobs=lambda: [SimpleNamespace(context_id="elsewhere")]),
        raising=False,
    )
    assert chat_mod._awaiting_self_resume("s") is False


# ── the job that fired THIS turn is not a future resume (#3494 re-review) ─────
# The scheduler deletes a fired one-shot only after the turn it started returns, so while
# the goal loop runs, that job is still listed. Counting it paused the drive on a trigger
# that was about to vanish, and nothing ever resumed the goal.
# A one-shot that is already due unless a test says otherwise: a date far in the past, so
# "due" never depends on when the suite runs.
def _job(job_id, *, context_id="s", schedule="2000-01-01T00:00:00+00:00", next_fire=None):
    return SimpleNamespace(
        id=job_id, context_id=context_id, schedule=schedule, next_fire=next_fire or schedule, prompt="p"
    )


def _fired_by(job_id):
    from graph.middleware.request_context import request_metadata_scope

    return request_metadata_scope({"scheduler_job_id": job_id, "origin": "scheduler"})


@pytest.mark.parametrize("job_id", ["wait:s", "watch-w1"])
def test_the_one_shot_firing_this_turn_is_not_awaited(clear_triggers, monkeypatch, job_id):
    monkeypatch.setattr(rs.STATE, "scheduler", SimpleNamespace(list_jobs=lambda: [_job(job_id)]), raising=False)
    with _fired_by(job_id):
        assert chat_mod._awaiting_self_resume("s") is False
    # Outside that turn the same row is an ordinary pending resume.
    assert chat_mod._awaiting_self_resume("s") is True


@pytest.mark.parametrize("next_fire", [None, "", "not-a-date"])
def test_an_unreadable_next_fire_on_the_firing_job_counts_as_pending(clear_triggers, monkeypatch, next_fire):
    # _is_spent_firing_job's documented fail-safe: a next_fire it can't read is treated as a
    # pending resume, never as the spent one-shot. Pinned so a refactor can't flip it quietly.
    job = SimpleNamespace(id="wait:s", context_id="s", schedule="2000-01-01T00:00:00+00:00", next_fire=next_fire, prompt="p")
    monkeypatch.setattr(rs.STATE, "scheduler", SimpleNamespace(list_jobs=lambda: [job]), raising=False)
    with _fired_by("wait:s"):
        assert chat_mod._awaiting_self_resume("s") is True


def test_another_pending_job_still_counts_while_one_fires(clear_triggers, monkeypatch):
    jobs = [_job("watch-w1"), _job("wait:s", schedule="2099-01-01T00:00:00+00:00")]
    monkeypatch.setattr(rs.STATE, "scheduler", SimpleNamespace(list_jobs=lambda: jobs), raising=False)
    with _fired_by("watch-w1"):
        assert chat_mod._awaiting_self_resume("s") is True


def test_a_wait_rescheduled_mid_turn_under_the_same_id_still_counts(clear_triggers, monkeypatch):
    # `wait` keeps ONE job per session (`wait:<session>`): a turn woken by it that waits
    # again re-adds the same id with a future fire time (#2751). That row is a real resume.
    later = _job("wait:s", schedule="2099-01-01T00:00:00+00:00")
    monkeypatch.setattr(rs.STATE, "scheduler", SimpleNamespace(list_jobs=lambda: [later]), raising=False)
    with _fired_by("wait:s"):
        assert chat_mod._awaiting_self_resume("s") is True


def test_a_recurring_job_firing_this_turn_still_counts(clear_triggers, monkeypatch):
    # A cron job rolls forward when claimed, so it will fire into this session again.
    cron = _job("nightly", schedule="0 9 * * *", next_fire="2099-01-01T09:00:00+00:00")
    monkeypatch.setattr(rs.STATE, "scheduler", SimpleNamespace(list_jobs=lambda: [cron]), raising=False)
    with _fired_by("nightly"):
        assert chat_mod._awaiting_self_resume("s") is True


def test_an_active_watch_still_counts_while_its_reaction_fires(clear_triggers, monkeypatch):
    # A `change` watch stays active after it reacts: it will trip again.
    w = SimpleNamespace(status="active", run_session="s")
    monkeypatch.setattr(rs.STATE, "watch_controller", SimpleNamespace(list_watches=lambda: [w]), raising=False)
    monkeypatch.setattr(rs.STATE, "scheduler", SimpleNamespace(list_jobs=lambda: [_job("watch-w1")]), raising=False)
    with _fired_by("watch-w1"):
        assert chat_mod._awaiting_self_resume("s") is True


# ── live: the drive loop pauses instead of exhausting ──────────────────────
class _ToolFake(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        message = next(self.messages)
        yield ChatGenerationChunk(message=AIMessageChunk(content=message.content or ""))


class _FakeJudge:
    async def ainvoke(self, _messages, config=None):  # noqa: ARG002
        return type("R", (), {"content": json.dumps({"met": False, "reason": "still working"})})()


@pytest.mark.asyncio
async def test_goal_drive_pauses_on_handoff_instead_of_exhausting(monkeypatch):
    from graph.config import LangGraphConfig
    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore
    from langgraph.checkpoint.memory import MemorySaver

    cfg = LangGraphConfig(goal_max_iterations=8)
    # Only ONE model message is consumed: the kickoff turn. If the drive DIDN'T pause it would
    # loop for continuations and exhaust the iterator — so reaching a clean 'done' proves the pause.
    fake = _ToolFake(messages=iter([AIMessage(content="Delegated the build; set a watch on its PR.")]))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph

        g = create_agent_graph(cfg, include_subagents=False, checkpointer=MemorySaver())
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
    import tempfile

    ctrl = GoalController(cfg, GoalStore(tempfile.mkdtemp()))
    monkeypatch.setattr(rs.STATE, "goal_controller", ctrl, raising=False)
    monkeypatch.setattr("graph.llm.create_llm", lambda *a, **k: _FakeJudge())
    # The verifier never passes; the ONLY way this ends cleanly is the async-handoff pause.
    monkeypatch.setattr(chat_mod, "_awaiting_self_resume", lambda sid: True)

    frames = [f async for f in chat_mod._chat_langgraph_stream("/goal ship the redesign", "h1")]
    kinds = [k for k, _ in frames]

    assert "done" in kinds
    assert any(k == "tool_start" and "paused" in str(p).lower() for k, p in frames)
    # The goal is PAUSED (still active), not exhausted — it will resume when the trigger fires.
    goal = ctrl.active_goal("h1")
    assert goal is not None and goal.status == "active"
    assert goal.iteration < goal.max_iterations  # did not spin to the cap
