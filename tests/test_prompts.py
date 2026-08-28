"""System-prompt SOUL resolution — a fleet member must load ITS OWN persona.

Regression guard for the member-identity bug: ``build_system_prompt`` used to read
``{workspace}/SOUL.md`` with ``workspace`` defaulting to the hub root ``/sandbox``. A
fleet member (spawned directly by the supervisor, not via entrypoint.sh) inherited that
default and so loaded the HUB's SOUL file — a placeholder for the member — leaving it with
no identity. The fix reads the instance's canonical ``config/SOUL.md``
(``instance_paths().soul_path``, PROTOAGENT_HOME-aware) first.
"""

from __future__ import annotations

from pathlib import Path

from graph.prompts import build_system_prompt


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_member_loads_own_config_soul_over_hub_workspace(monkeypatch, tmp_path):
    """The instance's own config/SOUL.md wins over the (hub-default) {workspace}/SOUL.md."""
    home = tmp_path / "member"
    _write(home / "config" / "SOUL.md", "# Identity\nI am Matt, the design-system engineer.")
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    # The hub's default workspace carries a DIFFERENT (placeholder) SOUL.
    hub = tmp_path / "hub"
    _write(hub / "SOUL.md", "# Soul\nReplace this file.")

    prompt = build_system_prompt(workspace=str(hub), include_subagents=False)

    assert "I am Matt, the design-system engineer." in prompt
    assert "Replace this file." not in prompt


def test_falls_back_to_workspace_soul_when_no_instance_soul(monkeypatch, tmp_path):
    """Backward-compat: with no instance config/SOUL.md, the legacy {workspace}/SOUL.md is used."""
    home = tmp_path / "member"
    (home / "config").mkdir(parents=True)  # instance root exists but no SOUL.md
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    hub = tmp_path / "hub"
    _write(hub / "SOUL.md", "# Identity\nLegacy runtime persona.")

    prompt = build_system_prompt(workspace=str(hub), include_subagents=False)

    assert "Legacy runtime persona." in prompt


# --- Operating model doctrine (ADR 0079, #3190 capability-derived) ----------

# All operating-model tool names for convenience.
_ALL_OM_TOOLS = frozenset({
    "set_goal", "update_goal_plan", "abandon_goal",
    "task_create", "task_update", "task_close",
    "schedule_task", "create_watch", "wait",
    "current_time",  # always-bound filler
})


def test_operating_model_with_all_capabilities():
    """When all operating-model tools are bound, the full doctrine ships — the
    OODA framing + all four composable primitives + the async-handoff rule."""
    prompt = build_system_prompt(include_subagents=False, bound_tool_names=_ALL_OM_TOOLS)
    assert "# Operating model" in prompt
    for step in ("Observe", "Orient", "Decide", "Act"):
        assert step in prompt
    for tool in ("set_goal", "task_create", "schedule_task", "create_watch", "update_goal_plan"):
        assert tool in prompt
    assert "Do not spin" in prompt
    assert "<working_state>" in prompt


def test_operating_model_omits_unbound_capabilities():
    """When only tasks are bound, goal/schedule/watch tools are absent from the
    prompt — the agent gets instructions only for what it can actually use."""
    tasks_only = frozenset({"task_create", "task_update", "task_close", "current_time"})
    prompt = build_system_prompt(include_subagents=False, bound_tool_names=tasks_only)
    assert "# Operating model" in prompt
    assert "task_create" in prompt
    assert "set_goal" not in prompt
    assert "schedule_task" not in prompt
    assert "create_watch" not in prompt
    assert "wait" not in prompt


def test_operating_model_absent_when_no_autonomous_tools():
    """A stripped deployment with none of the autonomous primitives gets no
    operating model section at all — a shorter, honest prompt."""
    minimal = frozenset({"current_time", "calculator", "web_search"})
    prompt = build_system_prompt(include_subagents=False, bound_tool_names=minimal)
    assert "# Operating model" not in prompt
    assert "set_goal" not in prompt
    assert "Do not spin" not in prompt


def test_operating_model_legacy_none_emits_everything():
    """Legacy callers that don't pass bound_tool_names get the full section."""
    prompt = build_system_prompt(include_subagents=False, bound_tool_names=None)
    assert "# Operating model" in prompt
    for tool in ("set_goal", "task_create", "schedule_task", "create_watch"):
        assert tool in prompt


def test_guidelines_omit_wait_when_unbound():
    """The Guidelines section should not reference ``wait`` when it isn't bound."""
    no_wait = frozenset({"current_time", "task_create"})
    prompt = build_system_prompt(include_subagents=False, bound_tool_names=no_wait)
    # The wait guideline starts with "When you're waiting"
    assert "wait(seconds" not in prompt


def test_guidelines_include_wait_when_bound():
    """The Guidelines section references ``wait`` when it is bound."""
    with_wait = frozenset({"current_time", "wait"})
    prompt = build_system_prompt(include_subagents=False, bound_tool_names=with_wait)
    assert "wait(seconds" in prompt


def test_wait_only_produces_clean_handoff_text():
    """When only wait is bound, the handoff sentence has no stray parenthesis."""
    wait_only = frozenset({"wait", "current_time"})
    prompt = build_system_prompt(include_subagents=False, bound_tool_names=wait_only)
    assert "Do not spin" in prompt
    assert "`wait` for a short countdown and END" in prompt
    assert ")" not in prompt.split("Do not spin")[1].split("and END")[0]


def test_watch_only_produces_clean_handoff_text():
    """When only watch is bound, the handoff sentence has no stray parenthesis."""
    watch_only = frozenset({"create_watch", "current_time"})
    prompt = build_system_prompt(include_subagents=False, bound_tool_names=watch_only)
    assert "Do not spin" in prompt
    assert "create_watch" in prompt
    assert "schedule_task" not in prompt


def test_working_state_block_is_referenced_for_observe():
    prompt = build_system_prompt(include_subagents=False, bound_tool_names=_ALL_OM_TOOLS)
    assert "working-state" in prompt.lower() or "<working_state>" in prompt


# --- Subagent roster filtering (ADR 0108 D3) ---------------------------------

def test_subagent_roster_shows_only_lead_visible():
    """The prompt's subagent section lists only lead-visible subagents."""
    prompt = build_system_prompt(include_subagents=True)
    # Lead-visible subagents must appear
    for name in ("researcher", "dream", "distill"):
        assert f"**{name}**" in prompt, f"lead-visible subagent '{name}' missing from prompt"

    # Workflow-internal subagents must NOT appear
    for name in (
        "antagonist", "verifier", "synthesizer",
        "codebase-mapper", "review-finder", "review-synthesizer",
        "self-improve",
    ):
        assert f"**{name}**" not in prompt, (
            f"workflow-only subagent '{name}' should not appear in the lead prompt"
        )


def test_workflow_subagents_still_in_registry():
    """Filtering from the prompt must not remove entries from SUBAGENT_REGISTRY —
    the workflow engine resolves subagents from the registry directly."""
    from graph.subagents.config import SUBAGENT_REGISTRY

    for name in (
        "antagonist", "verifier", "synthesizer",
        "codebase-mapper", "review-finder", "review-synthesizer",
        "self-improve",
    ):
        assert name in SUBAGENT_REGISTRY, (
            f"workflow subagent '{name}' must remain in SUBAGENT_REGISTRY"
        )


# --- SOUL is persona only (#3190) ----------------------------------------------------

def test_template_soul_names_no_capability_tools():
    """The persona must not carry capability doctrine: tool names belong to the
    capability-derived sections, so a deployment that unbinds a tool never keeps a
    stale mention of it in SOUL.md. Guards the repo default and the test fixture."""
    import re
    from pathlib import Path

    from graph.prompts import _GOAL_TOOLS, _SCHEDULE_TOOLS, _TASK_TOOLS, _WAIT_TOOLS, _WATCH_TOOLS

    repo_soul = (Path(build_system_prompt.__code__.co_filename).parent.parent / "config" / "SOUL.md").read_text(
        encoding="utf-8"
    )
    fixture_soul = "# Identity\nFixture persona for prompt-budget measurement.\n"
    names = _GOAL_TOOLS | _SCHEDULE_TOOLS | _TASK_TOOLS | _WATCH_TOOLS | _WAIT_TOOLS
    for text in (repo_soul, fixture_soul):
        for name in names:
            # `wait` is an English word — match only its tool forms (`wait`, wait(...)).
            pattern = rf"`{name}`|\b{name}\(" if name == "wait" else rf"\b{name}\b"
            assert not re.search(pattern, text), f"SOUL mentions capability tool {name!r}"
