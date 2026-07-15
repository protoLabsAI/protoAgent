"""The guarded self-authored persona tool — ``edit_soul`` (ADR 0079/0081).

Three axes:
  - the pure section-editor (`_apply_soul_section_edit`): replace / append / create,
    case-insensitivity, and markdown section-boundary semantics;
  - config gating (`get_all_tools(soul_edit_enabled=...)`), lead-only;
  - the tool end-to-end: it writes via config_io (so #1691 snapshots for free) and drives
    the injected reload callback, degrading gracefully when there's none.

Isolation for the write path mirrors test_soul_history: PROTOAGENT_HOME → tmp box root
(the autouse conftest fixture re-resolves instance_paths per test), seed neutralized.
"""

from __future__ import annotations

from pathlib import Path

from graph import config_io
from tools.lg_tools import (
    _apply_soul_section_edit,
    _build_soul_editor_tool,
    get_all_tools,
)

SAMPLE = """# Persona

I am a helpful agent.

## Voice

Terse and dry.

### Nuance

Occasionally warm.

## Values

Honesty above all.
"""


# ---------------------------------------------------------------------------
# (a) the pure section editor
# ---------------------------------------------------------------------------


def test_replace_section_swaps_only_that_section_body():
    out = _apply_soul_section_edit(SAMPLE, "Values", "Kindness, then honesty.", "replace")
    assert "Kindness, then honesty." in out
    assert "Honesty above all." not in out
    # untouched siblings survive verbatim.
    assert "Terse and dry." in out and "## Voice" in out


def test_replace_is_case_insensitive_on_the_heading():
    out = _apply_soul_section_edit(SAMPLE, "vOiCe", "New voice.", "replace")
    assert "New voice." in out and "Terse and dry." not in out


def test_replace_section_replaces_its_whole_subtree_up_to_next_sibling():
    # Replacing a level-2 section replaces everything under it — including the deeper
    # ### Nuance subsection — until the next same-or-higher heading (## Values).
    out = _apply_soul_section_edit(SAMPLE, "Voice", "Just terse.", "replace")
    assert "Just terse." in out
    assert "Occasionally warm." not in out  # the ### Nuance subtree went with it
    assert "Honesty above all." in out  # ## Values (next sibling) is preserved


def test_deeper_heading_bounds_at_same_or_higher_level():
    # Editing the level-3 Nuance stops at ## Values (level 2 <= 3), not before it.
    out = _apply_soul_section_edit(SAMPLE, "Nuance", "Rarely warm.", "replace")
    assert "Rarely warm." in out
    assert "Occasionally warm." not in out
    assert "## Values" in out and "Honesty above all." in out


def test_append_keeps_existing_body():
    out = _apply_soul_section_edit(SAMPLE, "Values", "Curiosity too.", "append")
    assert "Honesty above all." in out and "Curiosity too." in out
    # order preserved: existing before appended.
    assert out.index("Honesty above all.") < out.index("Curiosity too.")


def test_missing_section_is_created_at_end():
    out = _apply_soul_section_edit(SAMPLE, "Humor", "Deadpan.", "replace")
    assert out.rstrip().endswith("## Humor\n\nDeadpan.".rstrip()) or "## Humor" in out
    assert "Deadpan." in out
    # nothing existing was disturbed.
    assert "Honesty above all." in out and "Terse and dry." in out


def test_append_to_missing_section_creates_it():
    out = _apply_soul_section_edit("# Persona\n\nHi.\n", "Voice", "Warm.", "append")
    assert "## Voice" in out and "Warm." in out


def test_result_ends_with_single_trailing_newline():
    out = _apply_soul_section_edit(SAMPLE, "Values", "x", "replace")
    assert out.endswith("\n") and not out.endswith("\n\n")


# ---------------------------------------------------------------------------
# (b) config gating — lead-only, off by default
# ---------------------------------------------------------------------------


def test_get_all_tools_gates_edit_soul_on_flag():
    on = {t.name for t in get_all_tools(soul_edit_enabled=True)}
    off = {t.name for t in get_all_tools(soul_edit_enabled=False)}
    default = {t.name for t in get_all_tools()}
    assert "edit_soul" in on
    assert "edit_soul" not in off
    assert "edit_soul" not in default  # default is OFF (the guardrail)


# ---------------------------------------------------------------------------
# (c) the tool end-to-end
# ---------------------------------------------------------------------------


def _home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    # Neutralize the bundled-seed fallback so the test exercises ONLY the instance SOUL.md.
    monkeypatch.setattr(config_io, "soul_source_path", lambda: tmp_path / "no-seed.md")
    return home


async def test_edit_soul_writes_snapshots_and_reports_live(monkeypatch, tmp_path: Path):
    home = _home(monkeypatch, tmp_path)
    config_io.write_soul(SAMPLE)  # seed a real persona

    calls: list[int] = []

    def fake_reload():
        calls.append(1)
        return True, "reloaded"

    edit_soul = _build_soul_editor_tool(fake_reload)[0]
    msg = await edit_soul.ainvoke({"section": "Voice", "content": "Warm and plain.", "mode": "replace"})

    # persisted
    live = (home / "config" / "SOUL.md").read_text()
    assert "Warm and plain." in live and "Terse and dry." not in live
    # snapshotted the outgoing persona (#1691)
    versions = config_io.list_soul_versions()
    assert any("Terse and dry." in config_io.read_soul_version(v["id"]) for v in versions)
    # drove the reload and said so
    assert calls == [1]
    assert "live for your next turn" in msg
    assert "restorable from Settings" in msg


async def test_edit_soul_emits_operator_notice(monkeypatch, tmp_path: Path):
    # ADR 0081 transparency guardrail: a self-edit surfaces on the event bus so an identity
    # change is never silent (esp. on autonomous turns / a prompt-injection-driven edit).
    _home(monkeypatch, tmp_path)
    config_io.write_soul(SAMPLE)

    from graph.plugins import host

    events: list = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))

    edit_soul = _build_soul_editor_tool(None)[0]
    msg = await edit_soul.ainvoke({"section": "Voice", "content": "Warm and plain.", "mode": "replace"})

    persona_events = [(t, d) for t, d in events if t == "persona.self_edited"]
    assert len(persona_events) == 1
    _, data = persona_events[0]
    assert data["section"] == "Voice" and data["mode"] == "replace" and data["revision"]
    assert "persona" in data["summary"].lower()
    assert "operator has been notified" in msg


async def test_edit_soul_rejected_edit_emits_no_notice(monkeypatch, tmp_path: Path):
    # A refused edit (empty content) must NOT fire the operator notice — only real writes do.
    _home(monkeypatch, tmp_path)
    config_io.write_soul(SAMPLE)

    from graph.plugins import host

    events: list = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))

    edit_soul = _build_soul_editor_tool(None)[0]
    await edit_soul.ainvoke({"section": "Voice", "content": "  ", "mode": "replace"})
    assert not any(t == "persona.self_edited" for t, _ in events)


async def test_edit_soul_without_callback_degrades_gracefully(monkeypatch, tmp_path: Path):
    _home(monkeypatch, tmp_path)
    config_io.write_soul(SAMPLE)

    edit_soul = _build_soul_editor_tool(None)[0]
    msg = await edit_soul.ainvoke({"section": "Values", "content": "Kindness.", "mode": "replace"})
    assert "next reload/restart" in msg
    assert "Kindness." in (config_io.read_soul())


async def test_edit_soul_reload_failure_is_reported_not_raised(monkeypatch, tmp_path: Path):
    _home(monkeypatch, tmp_path)
    config_io.write_soul(SAMPLE)

    def boom():
        raise RuntimeError("reload blew up")

    edit_soul = _build_soul_editor_tool(boom)[0]
    msg = await edit_soul.ainvoke({"section": "Values", "content": "Kindness.", "mode": "replace"})
    # the write still landed; only the reload failed.
    assert "Kindness." in config_io.read_soul()
    assert "next restart" in msg


async def test_edit_soul_rejects_empty_content(monkeypatch, tmp_path: Path):
    _home(monkeypatch, tmp_path)
    config_io.write_soul(SAMPLE)
    edit_soul = _build_soul_editor_tool(None)[0]
    msg = await edit_soul.ainvoke({"section": "Voice", "content": "   ", "mode": "replace"})
    assert msg.startswith("Error:")
    assert config_io.read_soul() == SAMPLE  # unchanged


async def test_edit_soul_rejects_bad_mode(monkeypatch, tmp_path: Path):
    _home(monkeypatch, tmp_path)
    config_io.write_soul(SAMPLE)
    edit_soul = _build_soul_editor_tool(None)[0]
    msg = await edit_soul.ainvoke({"section": "Voice", "content": "x", "mode": "prepend"})
    assert msg.startswith("Error:") and "replace" in msg


async def test_edit_soul_noop_when_identical(monkeypatch, tmp_path: Path):
    _home(monkeypatch, tmp_path)
    config_io.write_soul(SAMPLE)
    edit_soul = _build_soul_editor_tool(None)[0]
    # Values already reads "Honesty above all." — a replace with the same body is a no-op.
    msg = await edit_soul.ainvoke({"section": "Values", "content": "Honesty above all.", "mode": "replace"})
    assert "No change" in msg


async def test_edit_soul_enforces_size_cap(monkeypatch, tmp_path: Path):
    _home(monkeypatch, tmp_path)
    config_io.write_soul(SAMPLE)
    edit_soul = _build_soul_editor_tool(None)[0]
    huge = "x" * (65 * 1024)
    msg = await edit_soul.ainvoke({"section": "Voice", "content": huge, "mode": "replace"})
    assert msg.startswith("Error:") and "cap" in msg
    assert config_io.read_soul() == SAMPLE  # rejected, nothing written


# ---------------------------------------------------------------------------
# (d) integration — the flag threads through create_agent_graph to a BOUND tool
# ---------------------------------------------------------------------------


def _lead_graph_tool_names(*, soul_self_edit_enabled: bool) -> set[str]:
    """Bound tool names on a compiled lead-agent graph (what the MODEL can call).

    Mirrors test_set_goal_tool's helper: stub the LLM so no gateway is needed, then read
    the ToolNode's tool map. Proves config.soul_self_edit_enabled reaches the binding —
    get_all_tools gating alone (test above) doesn't prove create_agent_graph threads it."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **k):
            return self

    fake = _Fake(messages=iter([AIMessage(content="x")]))
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph
        from graph.config import LangGraphConfig

        g = create_agent_graph(LangGraphConfig(soul_self_edit_enabled=soul_self_edit_enabled))
    node = g.nodes["tools"]
    for obj in (node, getattr(node, "runnable", None), getattr(node, "bound", None)):
        tbn = getattr(obj, "tools_by_name", None)
        if tbn:
            return set(tbn.keys())
    raise AssertionError("could not locate the ToolNode tool map")


def test_edit_soul_binds_to_lead_graph_only_when_flag_on():
    assert "edit_soul" in _lead_graph_tool_names(soul_self_edit_enabled=True)
    assert "edit_soul" not in _lead_graph_tool_names(soul_self_edit_enabled=False)


def test_create_agent_graph_accepts_reload_callback():
    # The server injects _reload_langgraph_agent here; a build without it must still work
    # (subagent / eval / script paths) — the tool then degrades to next-reload semantics.
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **k):
            return self

    fake = _Fake(messages=iter([AIMessage(content="x")]))
    sentinel = []
    with patch("graph.agent.create_llm", lambda *a, **k: fake):
        from graph.agent import create_agent_graph
        from graph.config import LangGraphConfig

        g = create_agent_graph(
            LangGraphConfig(soul_self_edit_enabled=True),
            reload_callback=lambda: sentinel.append(1) or (True, "ok"),
        )
    assert g is not None
