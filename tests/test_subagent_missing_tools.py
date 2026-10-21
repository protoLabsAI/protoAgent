"""Fail-fast at dispatch when a delegation prompt names tools the subagent won't have (#4327).

A delegated ``researcher`` asked to use ``read_file``/``pdf_look`` (bound to the LEAD, never
to the researcher) used to run to completion and come back "blocked": ``_subagent_tools``
silently dropped the ungranted names and the dispatch proceeded regardless. The guard scans
the PROMPT for lead tool names absent from the resolved subagent toolset and refuses BEFORE
the subagent graph is invoked. This does NOT change what a subagent inherits — only whether
we start. Builds the ``task``/``task_batch`` tools directly and fakes ``_run_subagent`` so no
real LLM runs, mirroring tests/test_task_batch.py.
"""

from __future__ import annotations

import asyncio

from langchain_core.tools import tool

import graph.agent as agent_mod
from graph.agent import _dispatch_tool_gap, _prompt_missing_tools, _subagent_tools
from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY
from tools.lg_tools import HITL_TOOL_NAMES, get_all_tools

RESEARCHER_TOOLS = list(SUBAGENT_REGISTRY["researcher"].tools)


def _stub(name: str):
    """A minimal real BaseTool carrying ``name`` — the guard only reads ``.name``."""

    @tool(name)
    def _t() -> str:
        """stub tool"""
        return ""

    return _t


def _lead_tools(extra: list[str] | None = None) -> list:
    return [_stub(n) for n in [*RESEARCHER_TOOLS, *(extra or [])]]


def _task_tools(monkeypatch, *, all_tools, lead_tool_names=None, recorder=None):
    """Build {name: tool} with ``_run_subagent`` replaced by a fake that records calls."""

    async def fake_run(**kwargs):
        if recorder is not None:
            recorder.append(kwargs)
        return f"OUT:{kwargs['description']}"

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent_mod, "_run_subagent", fake_run)
    tools = agent_mod._build_task_tools(
        LangGraphConfig(), all_tools, background_mgr=None, lead_tool_names=lead_tool_names
    )
    return {t.name: t for t in tools}


def _call(task, **args) -> str:
    out = asyncio.run(task.ainvoke({"name": "task", "args": args, "id": "tc-1", "type": "tool_call"}))
    return getattr(out, "content", out)


def _call_batch(task_batch, tasks) -> str:
    out = asyncio.run(
        task_batch.ainvoke({"name": "task_batch", "args": {"tasks": tasks}, "id": "tb-1", "type": "tool_call"})
    )
    return getattr(out, "content", out)


# ── _prompt_missing_tools (the scan) ──────────────────────────────────────────


def test_scan_flags_lead_tool_absent_from_subagent():
    missing = _prompt_missing_tools(
        "First use read_file to open docs/spec.md, then summarize it.",
        lead_tool_names=[*RESEARCHER_TOOLS, "read_file", "pdf_look"],
        sub_tool_names=RESEARCHER_TOOLS,
    )
    assert missing == ["read_file"]


def test_scan_flags_multiple_in_first_mention_order():
    missing = _prompt_missing_tools(
        "Run pdf_look on the attachment, then read_file the extracted text.",
        lead_tool_names=[*RESEARCHER_TOOLS, "read_file", "pdf_look"],
        sub_tool_names=RESEARCHER_TOOLS,
    )
    assert missing == ["pdf_look", "read_file"]


def test_scan_ignores_tool_the_subagent_has():
    missing = _prompt_missing_tools(
        "Use web_search to find the latest, then current_time to stamp it.",
        lead_tool_names=[*RESEARCHER_TOOLS, "read_file"],
        sub_tool_names=RESEARCHER_TOOLS,
    )
    assert missing == []


def test_scan_ignores_prose_and_names_the_lead_lacks():
    # "file" / "read" are prose; "frobnicate" is no tool at all — none are lead tools.
    missing = _prompt_missing_tools(
        "Please read the file carefully and frobnicate the result.",
        lead_tool_names=[*RESEARCHER_TOOLS, "read_file"],
        sub_tool_names=RESEARCHER_TOOLS,
    )
    assert missing == []


def test_scan_matches_whole_identifiers_only():
    # read_file embedded in a longer identifier must NOT match (word boundary).
    missing = _prompt_missing_tools(
        "The thread_filename and read_filename are unrelated.",
        lead_tool_names=[*RESEARCHER_TOOLS, "read_file"],
        sub_tool_names=RESEARCHER_TOOLS,
    )
    assert missing == []


# ── r1: a prompt naming a missing lead tool refuses, graph never invoked ───────


def test_r1_missing_tool_refuses_without_invoking_subagent(monkeypatch):
    called: list = []
    tools = _task_tools(monkeypatch, all_tools=_lead_tools(["read_file"]), recorder=called)
    out = _call(tools["task"], description="read spec", prompt="Use read_file to open docs/spec.md.", subagent_type="researcher")
    assert out.startswith("Not dispatched: subagent 'researcher' does not have tool(s) read_file")
    assert "Its tools:" in out
    assert "web_search" in out  # the subagent's real tools are listed, sorted
    assert "Do the step yourself, or pick a subagent that has them." in out
    assert called == []  # _run_subagent (and thus the subagent graph) was never reached


def test_r1_missing_tool_seen_via_full_lead_vocabulary(monkeypatch):
    # Mirrors production: read_file is a filesystem tool appended to the lead set AFTER the
    # task tool's tool_map snapshot. It is absent from all_tools but present in the live
    # vocabulary the graph populates — the guard must still catch it.
    called: list = []
    holder = {*RESEARCHER_TOOLS, "read_file", "pdf_look"}
    tools = _task_tools(monkeypatch, all_tools=_lead_tools(), lead_tool_names=holder, recorder=called)
    out = _call(tools["task"], description="read spec", prompt="Open it with read_file first.", subagent_type="researcher")
    assert "read_file" in out and out.startswith("Not dispatched:")
    assert called == []


# ── r2: a tool the subagent HAS dispatches normally ───────────────────────────


def test_r2_present_tool_dispatches(monkeypatch):
    called: list = []
    tools = _task_tools(monkeypatch, all_tools=_lead_tools(["read_file"]), recorder=called)
    out = _call(
        tools["task"],
        description="survey",
        prompt="Use web_search for the latest, then current_time to stamp it.",
        subagent_type="researcher",
    )
    assert out == "OUT:survey"
    assert len(called) == 1


# ── r3: prose and non-lead names never trigger a refusal ──────────────────────


def test_r3_prose_and_unknown_names_dispatch(monkeypatch):
    called: list = []
    tools = _task_tools(monkeypatch, all_tools=_lead_tools(["read_file"]), recorder=called)
    out = _call(
        tools["task"],
        description="survey",
        prompt="Read the file carefully and frobnicate the summary. Mention read_filename too.",
        subagent_type="researcher",
    )
    assert out == "OUT:survey"
    assert len(called) == 1


# ── r4: task_batch isolates the offending item; siblings still run ─────────────


def test_r4_task_batch_isolates_offending_item(monkeypatch):
    called: list = []
    tools = _task_tools(monkeypatch, all_tools=_lead_tools(["read_file"]), recorder=called)
    out = _call_batch(
        tools["task_batch"],
        [
            {"description": "bad", "prompt": "Use read_file to open it.", "subagent_type": "researcher"},
            {"description": "good", "prompt": "Use web_search to find it.", "subagent_type": "researcher"},
        ],
    )
    assert "Not dispatched" in out and "read_file" in out  # item 1 refused inline
    assert "OUT:good" in out  # item 2 still ran
    assert [k["description"] for k in called] == ["good"]  # only the good item reached _run_subagent


# ── r5: subagent tool inheritance is unchanged ────────────────────────────────


def test_r5_subagent_inheritance_unchanged():
    tool_map = {t.name: t for t in get_all_tools()}
    for name, cfg in SUBAGENT_REGISTRY.items():
        resolved = [t.name for t in _subagent_tools(cfg, tool_map)]
        # The contract _subagent_tools has always had: the allowlist, in order, minus the
        # HITL interrupt tools and anything not bound on this instance. The guard touches
        # none of this, so every registered subagent resolves to exactly the same set.
        expected = [n for n in cfg.tools if n in tool_map and n not in HITL_TOOL_NAMES]
        assert resolved == expected, f"inheritance drifted for subagent '{name}'"


def test_r5_guard_is_read_only_on_resolution():
    # Running the guard must not mutate a subagent's resolved toolset.
    tool_map = {t.name: t for t in _lead_tools(["read_file"])}
    before = [t.name for t in _subagent_tools(SUBAGENT_REGISTRY["researcher"], tool_map)]
    _dispatch_tool_gap(LangGraphConfig(), "researcher", "Use read_file now.", tool_map, set(tool_map) | {"read_file"})
    after = [t.name for t in _subagent_tools(SUBAGENT_REGISTRY["researcher"], tool_map)]
    assert before == after


def test_unknown_subagent_is_not_guarded():
    # An unknown subagent returns None here — the existing unknown-subagent error owns it.
    assert _dispatch_tool_gap(LangGraphConfig(), "nope", "Use read_file.", {}, {"read_file"}) is None
