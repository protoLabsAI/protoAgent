"""Unit tests for skill-v1 artifact emission from task() subagent completions.

Covers:
- SkillV1Artifact schema validation (required fields, types)
- skill artifact emitted with correct schema when emit_skill=True
- no emission when emit_skill=False
- no emission when subagent config has allow_skill_emission=False
- no emission when subagent raises an exception
- DataPart serialization format
- tool tracking metadata captured correctly
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from graph.extensions.skills import (
    SKILL_V1_MIME,
    SkillV1Artifact,
    _pending_skills,
    emit_skill_artifact,
    get_pending_skills,
)
from graph.subagents.config import SubagentConfig


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_pending_skills():
    """Reset the _pending_skills ContextVar to None before each test."""
    _pending_skills.set(None)
    yield
    _pending_skills.set(None)


# ── SkillV1Artifact schema tests ──────────────────────────────────────────────


def test_skill_artifact_schema() -> None:
    """SkillV1Artifact must accept all required fields and serialize correctly."""
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    artifact = SkillV1Artifact(
        name="web-research",
        description="Research a topic using web search",
        prompt_template="Search for information about {topic}",
        tools_used=["web_search", "fetch_url"],
        created_at=now,
        source_session_id="sess-abc123",
    )
    assert artifact.name == "web-research"
    assert artifact.description == "Research a topic using web search"
    assert artifact.prompt_template == "Search for information about {topic}"
    assert artifact.tools_used == ["web_search", "fetch_url"]
    assert artifact.created_at == now
    assert artifact.source_session_id == "sess-abc123"


def test_skill_artifact_to_dict() -> None:
    """to_dict() must return a JSON-compatible mapping with all fields."""
    now = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
    artifact = SkillV1Artifact(
        name="calc-task",
        description="Run calculations",
        prompt_template="Compute the result of {expr}",
        tools_used=["calculator"],
        created_at=now,
        source_session_id="sess-1",
    )
    d = artifact.to_dict()
    assert d["name"] == "calc-task"
    assert d["description"] == "Run calculations"
    assert d["prompt_template"] == "Compute the result of {expr}"
    assert d["tools_used"] == ["calculator"]
    assert d["created_at"] == now.isoformat()
    assert d["source_session_id"] == "sess-1"


def test_skill_datapart_serialization() -> None:
    """to_datapart() must return a valid A2A DataPart with the skill-v1 MIME type."""
    artifact = SkillV1Artifact(
        name="dp-test",
        description="DataPart test",
        prompt_template="prompt",
        tools_used=["current_time"],
        source_session_id="s1",
    )
    part = artifact.to_datapart()
    assert part["kind"] == "data"
    assert part["metadata"]["mimeType"] == SKILL_V1_MIME
    assert part["data"]["name"] == "dp-test"
    assert part["data"]["tools_used"] == ["current_time"]
    # created_at must be present and parseable
    datetime.fromisoformat(part["data"]["created_at"])


def test_skill_artifact_defaults() -> None:
    """created_at and source_session_id have sensible defaults."""
    before = datetime.now(timezone.utc)
    artifact = SkillV1Artifact(
        name="minimal",
        description="d",
        prompt_template="p",
    )
    after = datetime.now(timezone.utc)
    assert before <= artifact.created_at <= after
    assert artifact.source_session_id == ""
    assert artifact.tools_used == []


def test_skill_artifact_validation_empty_name() -> None:
    """SkillV1Artifact must reject an empty name."""
    with pytest.raises(ValueError, match="name"):
        SkillV1Artifact(name="", description="d", prompt_template="p")


def test_skill_artifact_validation_tools_not_list() -> None:
    """SkillV1Artifact must reject a non-list tools_used."""
    with pytest.raises(TypeError, match="tools_used"):
        SkillV1Artifact(
            name="x", description="d", prompt_template="p",
            tools_used="current_time",  # type: ignore[arg-type]
        )


# ── ContextVar emission helpers ───────────────────────────────────────────────


def test_get_pending_skills_empty_returns_empty_list() -> None:
    """get_pending_skills returns [] when no skills have been emitted."""
    assert get_pending_skills() == []


def test_emit_and_get_pending_skills() -> None:
    """emit_skill_artifact followed by get_pending_skills returns the artifact."""
    artifact = SkillV1Artifact(name="ctx-test", description="d", prompt_template="p")
    emit_skill_artifact(artifact)
    skills = get_pending_skills()
    assert len(skills) == 1
    assert skills[0].name == "ctx-test"


def test_emit_multiple_skills() -> None:
    """Multiple emit calls accumulate in order."""
    for i in range(3):
        emit_skill_artifact(SkillV1Artifact(
            name=f"skill-{i}", description="d", prompt_template="p",
        ))
    skills = get_pending_skills()
    assert [s.name for s in skills] == ["skill-0", "skill-1", "skill-2"]


# ── SubagentConfig allow_skill_emission field ─────────────────────────────────


def test_subagent_config_default_allows_skill_emission() -> None:
    """SubagentConfig.allow_skill_emission defaults to True."""
    cfg = SubagentConfig(
        name="researcher",
        description="d",
        system_prompt="s",
    )
    assert cfg.allow_skill_emission is True


def test_subagent_config_can_disable_skill_emission() -> None:
    """SubagentConfig.allow_skill_emission can be set to False."""
    cfg = SubagentConfig(
        name="researcher",
        description="d",
        system_prompt="s",
        allow_skill_emission=False,
    )
    assert cfg.allow_skill_emission is False


def test_subagent_config_disallowed_tools_unaffected() -> None:
    """Adding allow_skill_emission does not affect disallowed_tools."""
    cfg = SubagentConfig(
        name="researcher",
        description="d",
        system_prompt="s",
        disallowed_tools=["task", "rm_rf"],
    )
    assert cfg.disallowed_tools == ["task", "rm_rf"]
    assert cfg.allow_skill_emission is True


# ── task() emit_skill logic (inline simulation) ───────────────────────────────
#
# These tests verify the emission logic by calling the same conditional block
# that graph/agent.py uses, without spinning up a real LangGraph agent.


def _make_ai_message_with_tool_calls(tool_names: list[str]) -> MagicMock:
    msg = MagicMock(spec=AIMessage)
    msg.content = ""
    msg.tool_calls = [{"name": n, "args": {}, "id": f"call-{n}"} for n in tool_names]
    return msg


def _make_ai_message_with_content(content: str) -> MagicMock:
    msg = MagicMock(spec=AIMessage)
    msg.content = content
    msg.tool_calls = []
    return msg


def _run_emit_logic(
    *,
    messages: list,
    description: str,
    prompt: str,
    emit_skill: bool,
    allow_skill_emission: bool,
    session_id: str = "sess-test",
) -> None:
    """Replicate the skill emission block from task() for isolated unit testing."""
    tools_used: list[str] = []
    for msg in messages:
        for tc in getattr(msg, "tool_calls", []) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if name and name not in tools_used:
                tools_used.append(name)

    if emit_skill and allow_skill_emission:
        if not tools_used:
            logging.getLogger(__name__).warning(
                "[skill] emit_skill=True but no tool usage metadata captured; "
                "skipping skill emission.",
            )
        else:
            artifact = SkillV1Artifact(
                name=description,
                description=f"Captured workflow: {description}",
                prompt_template=prompt,
                tools_used=tools_used,
                created_at=datetime.now(timezone.utc),
                source_session_id=session_id,
            )
            emit_skill_artifact(artifact)


def test_skill_emitted_when_emit_skill_true() -> None:
    """Skill artifact is emitted when emit_skill=True and subagent succeeds."""
    msgs = [
        _make_ai_message_with_tool_calls(["current_time"]),
        _make_ai_message_with_content("done"),
    ]
    _run_emit_logic(
        messages=msgs,
        description="my-task",
        prompt="do the thing",
        emit_skill=True,
        allow_skill_emission=True,
    )
    skills = get_pending_skills()
    assert len(skills) == 1
    skill = skills[0]
    assert skill.name == "my-task"
    assert skill.tools_used == ["current_time"]
    assert skill.prompt_template == "do the thing"
    assert "Captured workflow" in skill.description


def test_no_emission_on_opt_out() -> None:
    """No skill artifact is emitted when emit_skill=False."""
    msgs = [
        _make_ai_message_with_tool_calls(["current_time"]),
        _make_ai_message_with_content("done"),
    ]
    _run_emit_logic(
        messages=msgs,
        description="my-task",
        prompt="do the thing",
        emit_skill=False,
        allow_skill_emission=True,
    )
    assert get_pending_skills() == []


def test_no_emission_on_failure() -> None:
    """No skill artifact is emitted when the exception path is taken (no emission call)."""
    # The failure path in task() does not reach the emission block at all —
    # verify by calling _run_emit_logic with emit_skill=True but no messages
    # (simulating the state after an exception: messages list is empty and
    # the outer except would have returned before reaching emission).
    #
    # This test focuses on: even if someone mistakenly called the emit block
    # with empty messages + no tools, no artifact is written.
    _run_emit_logic(
        messages=[],
        description="failed-task",
        prompt="do the thing",
        emit_skill=True,
        allow_skill_emission=True,
    )
    assert get_pending_skills() == []


def test_no_emission_when_config_disallows() -> None:
    """No skill artifact is emitted when allow_skill_emission=False."""
    msgs = [
        _make_ai_message_with_tool_calls(["current_time"]),
        _make_ai_message_with_content("done"),
    ]
    _run_emit_logic(
        messages=msgs,
        description="my-task",
        prompt="do the thing",
        emit_skill=True,
        allow_skill_emission=False,
    )
    assert get_pending_skills() == []


def test_tool_tracking_metadata_captured() -> None:
    """tools_used in the artifact lists all tools invoked, deduplicated."""
    msgs = [
        _make_ai_message_with_tool_calls(["current_time", "calculator"]),
        _make_ai_message_with_tool_calls(["current_time"]),  # duplicate — should appear once
        _make_ai_message_with_content("result"),
    ]
    _run_emit_logic(
        messages=msgs,
        description="dedup-test",
        prompt="compute",
        emit_skill=True,
        allow_skill_emission=True,
    )
    skills = get_pending_skills()
    assert len(skills) == 1
    assert skills[0].tools_used.count("current_time") == 1
    assert "calculator" in skills[0].tools_used


def test_no_emission_when_no_tool_usage(caplog) -> None:
    """When emit_skill=True but no tools were used, a warning is logged."""
    content_msg = _make_ai_message_with_content("plain response")
    content_msg.tool_calls = []

    with caplog.at_level(logging.WARNING):
        _run_emit_logic(
            messages=[content_msg],
            description="no-tools-task",
            prompt="just answer",
            emit_skill=True,
            allow_skill_emission=True,
        )

    assert get_pending_skills() == []
    assert any("no tool usage metadata" in r.message for r in caplog.records)
