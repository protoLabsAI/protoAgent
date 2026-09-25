"""One Langfuse trace per workflow run (``sdk.trace_run``).

Every ``run_subagent`` opens a ``subagent:<type>`` span in the CURRENT trace. A run with
no turn around it (Studio, REST — how the QA reviewer drives its panels) had no current
trace, so each step became its own sessionless ROOT: 92 of 100 recent traces in the fleet
project were review-panel steps burying the agents' turns."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from graph import sdk
from observability import tracing
from tests.test_workflows import _GatedReg, _patch_sdk


@pytest.fixture
def fake_langfuse(monkeypatch):
    """Real tracing module, fake client. Records each observation opened as a context
    (session roots and ``trace_span`` boundaries) in order."""
    opened: list[str] = []
    span = MagicMock()
    span.trace_id = "e" * 32

    def start(**kw):
        opened.append(kw["name"])
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=span)
        cm.__exit__ = MagicMock(return_value=None)
        return cm

    fake = MagicMock()
    fake.start_as_current_observation.side_effect = start
    fake.get_current_trace_id.return_value = None
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    return opened, span


def _subagent(seen: list):
    async def run_subagent(subagent_type, prompt, description=""):
        # What the real one does: a boundary span in whatever trace is current.
        seen.append(tracing.current_trace_id())
        with tracing.trace_span(f"subagent:{subagent_type}", as_type="agent"):
            return "<out>"

    return run_subagent


async def test_a_run_with_no_turn_around_it_is_one_trace(tmp_path, monkeypatch, fake_langfuse):
    import plugins.workflows as wf
    from plugins.workflows.run_state import WorkflowRunStore

    opened, span = fake_langfuse
    seen: list = []
    _patch_sdk(monkeypatch, _subagent(seen))

    result = await wf._execute(_GatedReg(), "gated", {"topic": "ai"}, run_store=WorkflowRunStore(tmp_path))

    # The run is the root; its step nests under it instead of being a root of its own.
    assert opened == ["workflow:gated", "subagent:researcher"]
    assert seen == ["e" * 32]
    span.update.assert_any_call(input={"topic": "ai"})
    outcome = [c.kwargs for c in span.update.call_args_list if "output" in c.kwargs][-1]
    assert result["paused"] and outcome["output"] == "paused at gated step 'analyze'"
    assert outcome["metadata"]["paused_step"] == "analyze"


async def test_a_run_inside_a_turn_nests_under_it(tmp_path, monkeypatch, fake_langfuse):
    import plugins.workflows as wf
    from plugins.workflows.run_state import WorkflowRunStore

    opened, _span = fake_langfuse
    _patch_sdk(monkeypatch, _subagent([]))

    async with tracing.trace_session("chat-1", name="chat", input="run the gated workflow"):
        await wf._execute(_GatedReg(), "gated", {"topic": "ai"}, run_store=WorkflowRunStore(tmp_path))

    # ONE root (the turn); the workflow is a span in it, not a second trace.
    assert opened == ["chat", "workflow:gated", "subagent:researcher"]


async def test_trace_run_redacts_and_caps_its_input(monkeypatch, fake_langfuse):
    _opened, span = fake_langfuse
    monkeypatch.setattr(tracing, "MAX_IO_CHARS", 60)
    secret = "sk-" + "Z" * 40

    async with sdk.trace_run("workflow:x", run_id="r1", input={"api_key": secret, "body": "y" * 100}):
        pass

    sent = [c.kwargs["input"] for c in span.update.call_args_list if "input" in c.kwargs][0]
    assert secret not in sent
    assert sent.endswith("more chars]")


async def test_trace_run_is_a_no_op_when_tracing_is_off(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    monkeypatch.setattr(tracing, "_langfuse", None)
    async with sdk.trace_run("workflow:x", run_id="r1", input={"a": 1}) as run:
        run.output("done")


async def test_a_failing_run_raises_its_own_error(tmp_path, monkeypatch, fake_langfuse):
    import plugins.workflows as wf
    from plugins.workflows.run_state import STATUS_FAILED, WorkflowRunStore

    async def boom(*_a, **_k):
        raise KeyError("engine bug")

    _patch_sdk(monkeypatch, _subagent([]))
    monkeypatch.setattr(wf, "execute_workflow", boom)
    store = WorkflowRunStore(tmp_path)

    with pytest.raises(KeyError, match="engine bug"):
        await wf._execute(_GatedReg(), "gated", {"topic": "ai"}, run_store=store)
    assert store.load(store.run_id)["status"] == STATUS_FAILED


async def test_tracing_off_keeps_the_chats_session(monkeypatch):
    monkeypatch.setattr(tracing, "_enabled", False)
    monkeypatch.setattr(tracing, "_langfuse", None)

    async with tracing.trace_session("chat-1", name="chat"):
        async with sdk.trace_run("workflow:x", run_id="run-9"):
            assert tracing.current_session_id() == "chat-1"
