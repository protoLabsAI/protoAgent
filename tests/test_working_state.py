"""<working_state> injection — the "Observe" step of the autonomous operating model (ADR 0079).

KnowledgeMiddleware injects the agent's OWN live commitments (active goal + plan, open tasks,
active watches, pending schedules) every turn, so the agent observes its durable state instead
of polling for it. These tests exercise the assembly directly with fake STATE surfaces.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import runtime.state as rs
from graph.middleware.knowledge import KnowledgeMiddleware
from graph.middleware.request_context import request_metadata_scope


def _mw() -> KnowledgeMiddleware:
    return KnowledgeMiddleware(knowledge_store=None)


class _GoalCtrl:
    def __init__(self, goal, plan=""):
        self._goal = goal
        self._store = SimpleNamespace(read_plan=lambda sid: plan)

    def active_goal(self, session_id):
        return self._goal


class _Tasks:
    def __init__(self, items):
        self._items = items

    def list(self, *, include_closed=False):
        return self._items


class _Watches:
    def __init__(self, watches):
        self._watches = watches

    def list_watches(self):
        return self._watches


class _Sched:
    def __init__(self, jobs):
        self._jobs = jobs

    def list_jobs(self):
        return self._jobs


@pytest.fixture
def clear_state(monkeypatch):
    for attr in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
        monkeypatch.setattr(rs.STATE, attr, None, raising=False)
    yield


def test_empty_when_nothing_active(clear_state):
    assert _mw()._working_state_block({"session_id": "s"}) == ""


def test_full_block_assembles_all_sections(clear_state, monkeypatch):
    goal = SimpleNamespace(status="active", iteration=2, max_iterations=8, condition="ship the redesign")
    monkeypatch.setattr(rs.STATE, "goal_controller", _GoalCtrl(goal, plan="- [x] scope\n- [ ] build"), raising=False)
    monkeypatch.setattr(
        rs.STATE,
        "tasks_store",
        _Tasks([{"status": "open", "id": "task-1", "priority": 1, "issue_type": "task", "title": "wire the hero"}]),
        raising=False,
    )
    watch = SimpleNamespace(status="active", status_line=lambda: "watch [active] (w1) via plugin: 'ci green'")
    monkeypatch.setattr(rs.STATE, "watch_controller", _Watches([watch]), raising=False)
    job = SimpleNamespace(id="job-9", next_fire="2026-07-09T09:00:00", prompt="follow up on the build")
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched([job]), raising=False)

    block = _mw()._working_state_block({"session_id": "s"})

    assert block.startswith("<working_state>") and block.endswith("</working_state>")
    assert "GOAL [active] (iteration 2/8): ship the redesign" in block
    assert "- [ ] build" in block  # plan (orient)
    assert "task-1" in block and "wire the hero" in block
    assert "watch [active]" in block
    assert "job-9" in block and "follow up on the build" in block


def test_goal_without_plan_nudges_to_record_one(clear_state, monkeypatch):
    goal = SimpleNamespace(status="active", iteration=0, max_iterations=5, condition="do the thing")
    monkeypatch.setattr(rs.STATE, "goal_controller", _GoalCtrl(goal, plan=""), raising=False)
    block = _mw()._working_state_block({"session_id": "s"})
    assert "no plan recorded yet" in block
    assert "update_goal_plan" in block


def test_plan_is_capped(clear_state, monkeypatch):
    from graph.projection import _WS_PLAN_CAP

    goal = SimpleNamespace(status="active", iteration=1, max_iterations=8, condition="c")
    monkeypatch.setattr(rs.STATE, "goal_controller", _GoalCtrl(goal, plan="x" * (_WS_PLAN_CAP + 500)), raising=False)
    block = _mw()._working_state_block({"session_id": "s"})
    assert "[truncated]" in block
    assert len(block) < _WS_PLAN_CAP + 800  # bounded, not the full 2000 chars


def test_only_active_watches_shown(clear_state, monkeypatch):
    active = SimpleNamespace(status="active", status_line=lambda: "watch [active] (w1)")
    met = SimpleNamespace(status="met", status_line=lambda: "watch [met] (w2)")
    monkeypatch.setattr(rs.STATE, "watch_controller", _Watches([active, met]), raising=False)
    block = _mw()._working_state_block({"session_id": "s"})
    assert "w1" in block and "w2" not in block


def test_read_failure_is_skipped_not_raised(clear_state, monkeypatch):
    class _Boom:
        def active_goal(self, sid):
            raise RuntimeError("db gone")

    monkeypatch.setattr(rs.STATE, "goal_controller", _Boom(), raising=False)
    # A task still renders — the goal section is skipped, nothing propagates.
    monkeypatch.setattr(
        rs.STATE,
        "tasks_store",
        _Tasks([{"status": "open", "id": "task-2", "priority": 2, "issue_type": "task", "title": "t"}]),
        raising=False,
    )
    block = _mw()._working_state_block({"session_id": "s"})
    assert "task-2" in block and "GOAL" not in block


# ── the job that started THIS turn is not "another wake" ─────────────────────


def _job(jid, schedule, next_fire, context_id="gh-alerts"):
    return SimpleNamespace(
        id=jid, schedule=schedule, next_fire=next_fire, context_id=context_id, prompt="A GitHub item you were watching"
    )


_PAST = "2026-09-09T14:47:14+00:00"


def test_the_job_that_started_this_turn_is_marked(clear_state, monkeypatch):
    # Regression (2026-09-09): the scheduler deletes a one-shot only after the turn it
    # started returns, so the job is still listed during that turn. Unmarked, the agent
    # read it as a second queued wake and looped until the stall guard fired.
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched([_job("watch-gh-x", _PAST, _PAST)]), raising=False)
    with request_metadata_scope({"scheduler_job_id": "watch-gh-x"}):
        block = _mw()._working_state_block({"session_id": "gh-alerts"})
    assert "watch-gh-x [FIRING NOW — this is the turn you are in, not another wake]" in block


def test_a_due_job_that_did_not_start_this_turn_stays_pending(clear_state, monkeypatch):
    # A `wait` resume for the same chat can come due while the operator's own turn is
    # running. It really is still pending, so it must not read as this turn.
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched([_job("wait-resume", _PAST, _PAST)]), raising=False)
    with request_metadata_scope({"origin": "user"}):
        block = _mw()._working_state_block({"session_id": "gh-alerts"})
    assert "FIRING NOW" not in block and f"wait-resume next={_PAST}" in block


def test_another_jobs_turn_does_not_mark_this_job(clear_state, monkeypatch):
    jobs = [_job("watch-a", _PAST, _PAST), _job("watch-b", _PAST, _PAST)]
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched(jobs), raising=False)
    with request_metadata_scope({"scheduler_job_id": "watch-b"}):
        block = _mw()._working_state_block({"session_id": "gh-alerts"})
    assert f"watch-a next={_PAST}" in block and "watch-b [FIRING NOW" in block


def test_a_cron_turn_is_marked_and_keeps_its_next_fire(clear_state, monkeypatch):
    nxt = "2026-09-21T16:00:00+00:00"
    jobs = [_job("digest", "0 9 * * 1", nxt, context_id=None)]
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched(jobs), raising=False)
    with request_metadata_scope({"scheduler_job_id": "digest"}):
        block = _mw()._working_state_block({"session_id": "activity"})
    assert f"digest [FIRING NOW — this is the turn you are in, not another wake] next={nxt}" in block


def test_no_request_context_marks_nothing(clear_state, monkeypatch):
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched([_job("watch-gh-x", _PAST, _PAST)]), raising=False)
    assert "FIRING NOW" not in _mw()._working_state_block({"session_id": "gh-alerts"})


# ── ADR 0127 D5: this chat's pending actions, in full; other chats a count only ──────

# The 2026-11-07 shape: a multi-step resume instruction whose decisive step ("squash-merge
# on PASS") sat far past the old 60-char preview.
_RELEASE_RESUME = (
    "Continue the protoAgent v0.231.0 minor release. The operator approved it, and Josh's "
    "condition was: once #4801 and #4808 part 1 have both merged, cut the release.\n"
    "1. Read the review panel verdict on release PR #4847.\n"
    "2. If PASS: squash-merge #4847 and push the v0.231.0 tag.\n"
    "3. If WARN or FAIL: do NOT merge — report back to the operator and stop."
)


def _sjob(jid, prompt, *, context_id=None, origin_session=None, schedule="2026-11-07T18:40:00+00:00"):
    return SimpleNamespace(
        id=jid,
        prompt=prompt,
        schedule=schedule,
        next_fire="2026-11-07T18:40:00+00:00",
        context_id=context_id,
        origin_session=origin_session,
    )


def test_this_chats_wait_shows_its_full_instruction_and_that_it_runs_on_its_own(clear_state, monkeypatch):
    job = _sjob("wait:chat-a", _RELEASE_RESUME, context_id="chat-a")
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched([job]), raising=False)

    block = _mw()._working_state_block({"session_id": "chat-a"})

    assert "squash-merge #4847 and push the v0.231.0 tag" in block  # the step the 60-char cut hid
    assert "PENDING SCHEDULES (this chat):" in block
    assert "do NOT merge — report back to the operator and stop." in block
    assert 'runs automatically unless you cancel it with cancel_schedule("wait:chat-a")' in block
    assert "next=2026-11-07T18:40:00+00:00" in block


def test_a_long_instruction_is_capped_with_an_explicit_marker(clear_state, monkeypatch):
    from graph.projection import _WS_INSTRUCTION_CAP

    job = _sjob("wait:chat-a", "y" * (_WS_INSTRUCTION_CAP + 300), context_id="chat-a")
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched([job]), raising=False)
    block = _mw()._working_state_block({"session_id": "chat-a"})
    assert "…[truncated, 300 more chars]" in block
    assert "y" * _WS_INSTRUCTION_CAP in block and "y" * (_WS_INSTRUCTION_CAP + 1) not in block


def test_another_chats_jobs_are_a_count_only(clear_state, monkeypatch):
    jobs = [
        _sjob("wait:chat-b", "merge the other release", context_id="chat-b"),
        _sjob("remind-b", "ping the reviewer", origin_session="chat-b", context_id="chat-b"),
    ]
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched(jobs), raising=False)

    block = _mw()._working_state_block({"session_id": "chat-a"})

    assert "wait:chat-b" not in block and "merge the other release" not in block
    assert "remind-b" not in block
    assert "OTHER CHATS: 2 pending schedules (not shown here" in block
    assert "PENDING SCHEDULES (this chat)" not in block


def test_agent_wide_jobs_are_a_compact_list(clear_state, monkeypatch):
    jobs = [
        # An Activity-thread cron created outside any chat.
        _sjob("digest", "x" * 400, schedule="0 9 * * 1"),
        # A recurring schedule another chat created: it fires into Activity whichever chat is open.
        _sjob("standup", "post the standup", origin_session="chat-b", schedule="0 9 * * 1-5"),
    ]
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched(jobs), raising=False)

    block = _mw()._working_state_block({"session_id": "chat-a"})

    assert "AGENT-WIDE SCHEDULES (not tied to a chat):" in block
    assert "- digest next=" in block and "x" * 121 not in block  # compact preview
    assert "- standup next=" in block and "OTHER CHATS" not in block


def test_a_job_owned_one_shot_with_no_origin_is_agent_wide(clear_state, monkeypatch):
    # Every background job now runs in its own job: execution session (ADR 0129);
    # with no origin chat that session is not a chat membership, so the one-shot
    # must stay on the agent-wide compact list (its pre-isolation visibility)
    # instead of vanishing into every chat's count-only OTHER CHATS bucket.
    job = _sjob("orphan", "x" * 400, context_id="job:e3b0c442-aaaa-bbbb-cccc-000000000000")
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched([job]), raising=False)

    block = _mw()._working_state_block({"session_id": "chat-a"})

    assert "AGENT-WIDE SCHEDULES (not tied to a chat):" in block
    assert "- orphan next=" in block and "x" * 121 not in block  # compact preview
    assert "OTHER CHATS" not in block


def test_a_recurring_schedule_made_in_this_chat_shows_in_full(clear_state, monkeypatch):
    job = _sjob("weekly", _RELEASE_RESUME, origin_session="chat-a", schedule="0 9 * * 1")
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched([job]), raising=False)
    block = _mw()._working_state_block({"session_id": "chat-a"})
    assert "- weekly next=" in block and "(recurring '0 9 * * 1')" in block
    assert "squash-merge #4847" in block


def test_the_firing_job_is_still_marked_and_not_called_pending(clear_state, monkeypatch):
    job = _sjob("wait:chat-a", _RELEASE_RESUME, context_id="chat-a")
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched([job]), raising=False)
    with request_metadata_scope({"scheduler_job_id": "wait:chat-a"}):
        block = _mw()._working_state_block({"session_id": "chat-a"})
    assert "wait:chat-a [FIRING NOW — this is the turn you are in, not another wake]" in block
    assert "runs automatically unless you cancel it" not in block  # it IS this turn
    assert "squash-merge #4847" in block


def test_this_chats_watch_shows_its_full_reaction_others_are_counted(clear_state, monkeypatch):
    def _w(wid, run_session, run_prompt):
        return SimpleNamespace(
            id=wid,
            status="active",
            run_session=run_session,
            run_prompt=run_prompt,
            interval_s=600.0,
            last_checked=1_800_000_000.0,
            status_line=lambda: f"watch [active] ({wid}) via plugin: 'ci green'",
        )

    watches = [_w("w-a", "chat-a", _RELEASE_RESUME), _w("w-b", "chat-b", "other chat reaction")]
    monkeypatch.setattr(rs.STATE, "watch_controller", _Watches(watches), raising=False)

    block = _mw()._working_state_block({"session_id": "chat-a"})

    assert "ACTIVE WATCHES (this chat):" in block
    assert "squash-merge #4847 and push the v0.231.0 tag" in block
    assert 'runs automatically unless you clear it with clear_watch("w-a")' in block
    assert "next check≈" in block
    assert "w-b" not in block and "other chat reaction" not in block
    assert "OTHER CHATS: 1 active watch (not shown here" in block


def test_more_in_chat_entries_than_the_cap_point_at_the_list_tool(clear_state, monkeypatch):
    from graph.projection import _WS_CHAT_ITEM_CAP

    jobs = [_sjob(f"j{i}", f"step {i}", context_id="chat-a") for i in range(_WS_CHAT_ITEM_CAP + 2)]
    monkeypatch.setattr(rs.STATE, "scheduler", _Sched(jobs), raising=False)
    block = _mw()._working_state_block({"session_id": "chat-a"})
    assert "…+2 more (list_schedules shows them all)" in block


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("schedule", ["0 9 * * 1", "2027-01-01T00:00:00+00:00"])
def test_isolated_schedule_full_in_origin_and_execution(clear_state, monkeypatch, tmp_path, schedule):
    from scheduler.local import LocalScheduler

    scheduler = LocalScheduler("projection", invoke_url="http://test", db_dir=tmp_path)
    job = scheduler.add_job(_RELEASE_RESUME, schedule, origin_session="chat-a")
    scheduler = LocalScheduler("projection", invoke_url="http://test", db_dir=tmp_path)
    monkeypatch.setattr(rs.STATE, "scheduler", scheduler)
    for session in ("chat-a", job.context_id):
        block = _mw()._working_state_block({"session_id": session})
        assert all(line in block for line in _RELEASE_RESUME.splitlines())
        assert f'cancel_schedule("{job.id}")' in block
    sibling = _mw()._working_state_block({"session_id": "job:sibling"})
    assert "squash-merge #4847" not in sibling
