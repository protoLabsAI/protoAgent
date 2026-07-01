"""Goal-mode fresh-context continuation (Ralph-style carved context) — #897 / bd-3hh.

The feature shipped in PR #1057 without tests; this backfills coverage for the
four moving parts of ``fresh_context`` mode:
- the durable plan artifact in ``GoalStore`` (``read_plan`` / ``write_plan``)
- ``fresh_context`` parsing onto ``GoalState`` via ``parse_control``
- the ``GoalState`` field default + ``status_line`` tag
- the fresh-context continuation prompt shape (vs the same-session default)
"""

import pytest

from graph.config import LangGraphConfig
from graph.goals.controller import GoalController
from graph.goals.store import GoalStore, _safe_name
from graph.goals.types import GoalState, VerifyResult


def _ctrl(tmp_path, **overrides):
    return GoalController(LangGraphConfig(**overrides), GoalStore(tmp_path))


# --- durable plan artifact (GoalStore) --------------------------------------


def test_plan_round_trip(tmp_path):
    store = GoalStore(tmp_path)
    store.write_plan("s1", "- [x] step one\n- [ ] step two")
    assert store.read_plan("s1") == "- [x] step one\n- [ ] step two"


def test_read_plan_absent_returns_empty(tmp_path):
    assert GoalStore(tmp_path).read_plan("never-written") == ""


def test_plan_artifact_is_separate_from_state_file(tmp_path):
    store = GoalStore(tmp_path)
    store.set(GoalState(session_id="s2", condition="x"))
    store.write_plan("s2", "the plan")
    # distinct files: <safe>.json (state) vs <safe>.plan.md (plan)
    assert (tmp_path / f"{_safe_name('s2')}.json").exists()
    assert (tmp_path / f"{_safe_name('s2')}.plan.md").exists()


def test_plan_overwrite_is_atomic_with_no_temp_leftovers(tmp_path):
    store = GoalStore(tmp_path)
    store.write_plan("s3", "v1")
    store.write_plan("s3", "v2")
    assert store.read_plan("s3") == "v2"
    assert not list(tmp_path.glob("*.tmp"))


# --- fresh_context parsing (GoalController.parse_control) --------------------


@pytest.mark.asyncio
async def test_parse_sets_fresh_context_true(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "tests pass", "fresh_context": true}', "s")
    assert c.active_goal("s").fresh_context is True


@pytest.mark.asyncio
async def test_parse_defaults_fresh_context_false(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "tests pass"}', "s")
    assert c.active_goal("s").fresh_context is False


@pytest.mark.asyncio
async def test_parse_plain_text_fresh_context_false(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control("/goal make the build green", "s")
    assert c.active_goal("s").fresh_context is False


# --- GoalState field + status line ------------------------------------------


def test_goal_state_default_is_false():
    assert GoalState(session_id="s", condition="x").fresh_context is False


def test_status_line_tags_fresh_context():
    fresh = GoalState(session_id="s", condition="x", fresh_context=True)
    assert "fresh-context" in fresh.status_line()
    plain = GoalState(session_id="s", condition="x")
    assert "fresh-context" not in plain.status_line()


# --- continuation prompt shape ----------------------------------------------


def test_fresh_context_continuation_seeds_from_durable_plan(tmp_path):
    c = _ctrl(tmp_path)
    c._store.write_plan("s", "- [ ] wire the thing")
    state = GoalState(
        session_id="s",
        condition="make it green",
        fresh_context=True,
        iteration=3,
        max_iterations=8,
    )
    result = VerifyResult(met=False, reason="2 tests still failing", evidence="pytest: 2 failed")
    prompt = c._continuation(state, result)
    assert "fresh context" in prompt
    assert "wire the thing" in prompt  # the durable plan is seeded in
    assert "make it green" in prompt  # the goal condition
    assert "2 tests still failing" in prompt  # the last verifier reason
    assert "ONE concrete step" in prompt
    assert "update_goal_plan" in prompt


def test_default_continuation_stays_same_session(tmp_path):
    c = _ctrl(tmp_path)
    state = GoalState(
        session_id="s",
        condition="make it green",
        checklist="- [ ] do the work",
        iteration=1,
        max_iterations=8,
    )
    result = VerifyResult(met=False, reason="nope", evidence="")
    prompt = c._continuation(state, result)
    assert "fresh context" not in prompt  # default path: same-session continuity
    assert "Current plan" in prompt  # the existing template
    assert "do the work" in prompt  # seeded from in-memory checklist, not disk
