"""Context architecture characterization tests — pin current behavior before v2.

These tests document the EXISTING context lifecycle (ADR 0107 Phase 0):
how KnowledgeMiddleware composes the per-turn context frame, how context
frames are tagged, what sections they contain, and how the stable prefix
is composed. They are regression baselines — v2 changes will update them
deliberately, not silently.

No network, no model calls, no checkpointer — pure unit tests against the
middleware and context-frame modules.
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from graph.context_frame import (
    CONTEXT_FRAME_KWARG,
    context_frame_message,
    is_context_frame,
)
from graph.middleware.knowledge import KnowledgeMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal knowledge store stub — enough for KnowledgeMiddleware."""

    def __init__(self, *, hot: str = "", hot_entries: list | None = None, results: list | None = None):
        self._hot = hot
        self._hot_entries = hot_entries
        self._results = results or []

    def get_hot_memory(self, max_chars: int = 6000) -> str:
        return self._hot

    def get_hot_memory_entries(self, max_chars: int = 6000) -> list[tuple[int, str]]:
        if self._hot_entries is not None:
            return self._hot_entries
        if self._hot:
            return [(1, self._hot)]
        return []

    def search(self, query: str, k: int = 5, **kwargs) -> list[dict]:
        return self._results


def _mw(store=None, **kwargs) -> KnowledgeMiddleware:
    return KnowledgeMiddleware(knowledge_store=store, **kwargs)


def _state_with_human(text: str = "hello") -> dict:
    """Minimal state dict with one human message — the trigger for before_agent."""
    return {"messages": [HumanMessage(content=text)]}


# ---------------------------------------------------------------------------
# context_frame module
# ---------------------------------------------------------------------------


class TestContextFrame:
    """The context_frame_message() / is_context_frame() contract."""

    def test_frame_is_human_message(self):
        frame = context_frame_message("test content")
        assert isinstance(frame, HumanMessage)

    def test_frame_tagged_with_kwarg(self):
        frame = context_frame_message("test content")
        assert frame.additional_kwargs.get(CONTEXT_FRAME_KWARG) is True

    def test_frame_wrapped_in_injected_context_envelope(self):
        frame = context_frame_message("inner stuff")
        assert frame.content.startswith("<injected_context>")
        assert frame.content.endswith("</injected_context>")
        assert "inner stuff" in frame.content

    def test_is_context_frame_true_for_frames(self):
        frame = context_frame_message("x")
        assert is_context_frame(frame) is True

    def test_is_context_frame_false_for_regular_messages(self):
        msg = HumanMessage(content="just a message")
        assert is_context_frame(msg) is False

    def test_is_context_frame_false_for_ai_messages(self):
        msg = AIMessage(content="response")
        assert is_context_frame(msg) is False


# ---------------------------------------------------------------------------
# KnowledgeMiddleware.before_agent — context frame composition
# ---------------------------------------------------------------------------


class TestBeforeAgent:
    """before_agent() returns a messages state update containing a context frame."""

    def test_returns_messages_with_context_frame(self, monkeypatch):
        """before_agent returns a dict with a 'messages' key containing a frame."""
        mw = _mw(store=_FakeStore(hot="Agent is a helpful assistant"))
        # Suppress prior-sessions disk read
        monkeypatch.setattr(mw, "load_memory", lambda *a, **kw: "")
        result = mw.before_agent(_state_with_human(), runtime=None)
        assert result is not None
        msgs = result.get("messages", [])
        assert len(msgs) == 1
        assert is_context_frame(msgs[0])

    def test_frame_contains_injected_memory_section(self, monkeypatch):
        """The frame includes an <injected_memory> envelope when memory is injected."""
        mw = _mw(store=_FakeStore(hot="Always remember: be helpful"))
        monkeypatch.setattr(mw, "load_memory", lambda *a, **kw: "")
        result = mw.before_agent(_state_with_human(), runtime=None)
        frame = result["messages"][0]
        assert "<injected_memory>" in frame.content
        assert "Always remember: be helpful" in frame.content

    def test_frame_contains_working_state_when_active(self, monkeypatch):
        """The frame includes <working_state> when the agent has active commitments."""
        import runtime.state as rs

        goal = SimpleNamespace(
            status="active", iteration=1, max_iterations=5, condition="test goal"
        )
        goal_ctrl = SimpleNamespace(
            active_goal=lambda sid: goal,
            _store=SimpleNamespace(read_plan=lambda sid: "step 1"),
        )
        monkeypatch.setattr(rs.STATE, "goal_controller", goal_ctrl, raising=False)
        # Clean up other state surfaces
        for attr in ("tasks_store", "watch_controller", "scheduler"):
            monkeypatch.setattr(rs.STATE, attr, None, raising=False)

        mw = _mw(store=None)
        monkeypatch.setattr(mw, "load_memory", lambda *a, **kw: "")
        # session_id is required for working-state to resolve the active goal
        state = {**_state_with_human(), "session_id": "test-session"}
        result = mw.before_agent(state, runtime=None)
        frame = result["messages"][0]
        assert "<working_state>" in frame.content
        assert "test goal" in frame.content

    def test_clears_legacy_context_channel(self, monkeypatch):
        """before_agent always clears the legacy context channel (#2774)."""
        mw = _mw(store=_FakeStore(hot="x"))
        monkeypatch.setattr(mw, "load_memory", lambda *a, **kw: "")
        result = mw.before_agent(_state_with_human(), runtime=None)
        assert result.get("context") == ""
        assert result.get("context_sections") == []

    def test_no_recompose_on_context_frame_input(self, monkeypatch):
        """When the last message is already a context frame, no new frame is composed."""
        mw = _mw(store=None)
        monkeypatch.setattr(mw, "load_memory", lambda *a, **kw: "")
        existing_frame = context_frame_message("prior context")
        state = {"messages": [HumanMessage(content="hi"), existing_frame]}
        result = mw.before_agent(state, runtime=None)
        assert result is None

    def test_no_compose_on_ai_message_last(self, monkeypatch):
        """When the last message is an AI response (re-entry), no frame is composed."""
        mw = _mw(store=None)
        monkeypatch.setattr(mw, "load_memory", lambda *a, **kw: "")
        state = {"messages": [HumanMessage(content="hi"), AIMessage(content="sure")]}
        result = mw.before_agent(state, runtime=None)
        assert result is None

    def test_incognito_suppresses_memory(self, monkeypatch):
        """Incognito threads get no memory injection (ADR 0069 D3b)."""
        store = _FakeStore(
            hot="secret fact",
            results=[{"id": 1, "preview": "rag hit", "source_type": "operator", "domain": "general"}],
        )
        mw = _mw(store=store)
        monkeypatch.setattr(mw, "load_memory", lambda *a, **kw: "prior session data")
        state = {**_state_with_human("query"), "incognito": True}
        result = mw.before_agent(state, runtime=None)
        if result and "messages" in result:
            content = result["messages"][0].content
            assert "secret fact" not in content
            assert "rag hit" not in content
            assert "prior session" not in content


# ---------------------------------------------------------------------------
# Stable prefix composition
# ---------------------------------------------------------------------------


class TestStablePrefix:
    """The stable prefix (system prompt) is composed deterministically."""

    def test_build_system_prompt_returns_string(self):
        from graph.prompts import build_system_prompt

        prompt = build_system_prompt(include_subagents=False)
        assert isinstance(prompt, str)
        assert len(prompt) > 100  # non-trivial

    def test_build_system_prompt_is_deterministic(self):
        """Two calls produce byte-identical output (the cache discipline contract)."""
        from graph.prompts import build_system_prompt

        a = build_system_prompt(include_subagents=False)
        b = build_system_prompt(include_subagents=False)
        assert a == b

    def test_runtime_context_shares_stable_prefix(self):
        """runtime/context.py uses the same build_system_prompt (D8 baseline)."""
        from graph.prompts import build_system_prompt
        from runtime.context import build_stable_prefix

        native = build_system_prompt(include_subagents=False)
        external = build_stable_prefix(include_subagents=False)
        assert native == external


# ---------------------------------------------------------------------------
# compose_context — the volatile delta
# ---------------------------------------------------------------------------


class TestComposeContext:
    """compose_context() returns the volatile delta as a dict with context + sections."""

    def test_returns_dict_with_context_key(self, monkeypatch):
        mw = _mw(store=_FakeStore(hot="fact"))
        monkeypatch.setattr(mw, "load_memory", lambda *a, **kw: "")
        result = mw.compose_context(_state_with_human(), record=False)
        assert "context" in result
        assert isinstance(result["context"], str)

    def test_returns_context_sections_metadata(self, monkeypatch):
        mw = _mw(store=_FakeStore(hot="fact"))
        monkeypatch.setattr(mw, "load_memory", lambda *a, **kw: "")
        result = mw.compose_context(_state_with_human(), record=False)
        sections = result.get("context_sections", [])
        assert isinstance(sections, list)
        # At least the memory section should be present
        labels = [s["label"] for s in sections]
        assert any("memory" in lbl.lower() for lbl in labels)

    def test_empty_store_returns_empty_context(self, monkeypatch):
        mw = _mw(store=None)
        monkeypatch.setattr(mw, "load_memory", lambda *a, **kw: "")
        # Clear all working-state surfaces
        import runtime.state as rs

        for attr in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
            monkeypatch.setattr(rs.STATE, attr, None, raising=False)
        result = mw.compose_context(_state_with_human(), record=False)
        assert result["context"] == ""
        assert result["context_sections"] == []

    def test_skill_index_outside_memory_envelope(self, monkeypatch):
        """The skills index is NOT memory and sits outside <injected_memory>."""

        class _FakeIndex:
            def skill_summaries(self):
                return [{"name": "test-skill", "description": "A test skill"}]

        mw = _mw(store=None, skills_index=_FakeIndex())
        monkeypatch.setattr(mw, "load_memory", lambda *a, **kw: "")
        import runtime.state as rs

        for attr in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
            monkeypatch.setattr(rs.STATE, attr, None, raising=False)
        result = mw.compose_context(_state_with_human(), record=False)
        ctx = result["context"]
        assert "<available_skills>" in ctx
        assert "test-skill" in ctx
        # Skills are NOT inside the memory envelope
        if "<injected_memory>" in ctx:
            mem_start = ctx.index("<injected_memory>")
            mem_end = ctx.index("</injected_memory>")
            skills_start = ctx.index("<available_skills>")
            assert skills_start > mem_end or skills_start < mem_start
