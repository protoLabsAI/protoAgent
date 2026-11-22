"""A private steer never reaches tracing, prompt capture or the fleet trace export (#5403).

Nothing here binds a fake source reader: every test runs against the production default
(``graph.public_memory.production_source``) and the records the private-steer path really
writes — the operator-owned partition task (``private_intake.record_private_steer``) and
the session mark (``private_sessions.mark``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from graph import private_sessions, public_memory
from graph.middleware.prompt_capture import PromptCaptureMiddleware
from graph.middleware.request_context import request_metadata_scope
from observability import trace_export, tracing
from observability.prompt_snapshots import prompt_snapshots
from runtime.state import STATE

CANARY = "PRIVATE_STEER_CANARY"
PRIVATE = "chat-steered"
PUBLIC = "chat-ordinary"


@pytest.fixture(autouse=True)
def _production_reader(tmp_path, monkeypatch):
    # The whole point: the reader under test is the module default, never a test double.
    assert public_memory._source_reader is public_memory.production_source
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path / "memory"))
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(private_steering_enabled=True), raising=False)
    monkeypatch.setattr(STATE, "a2a_task_engine", None, raising=False)
    monkeypatch.setenv("PROTOAGENT_FLEET_TRACE_EXPORT", str(tmp_path / "export"))
    trace_export._reset_for_test()
    trace_export.init()
    yield
    trace_export._reset_for_test()
    assert public_memory._source_reader is public_memory.production_source


@pytest.fixture
def langfuse(monkeypatch):
    """A recording Langfuse client; returns the turn's root span."""
    client, span, scope = MagicMock(), MagicMock(), MagicMock()
    span.trace_id = "c" * 32
    scope.__enter__ = MagicMock(return_value=span)
    scope.__exit__ = MagicMock(return_value=None)
    client.start_as_current_observation.return_value = scope
    monkeypatch.setattr(tracing, "_langfuse", client)
    monkeypatch.setattr(tracing, "_enabled", True)
    return client, span


@pytest.fixture
async def steer(tmp_path, monkeypatch, request):
    """Record a private steer on ``PRIVATE`` the way the live path does. ``recorded`` leaves
    only the partition task (the steer is persisted before any turn starts); ``marked``
    leaves only the session mark the runner writes as the private turn begins."""
    if request.param == "marked":
        private_sessions.mark(PRIVATE)
        yield
        return
    from a2a.server.context import ServerCallContext

    from a2a_impl import private_intake, stores

    engine = stores.make_sqlite_engine(str(tmp_path / "tasks.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    monkeypatch.setattr(STATE, "a2a_task_engine", engine, raising=False)
    operator = ServerCallContext(state={"principal_kind": "shared-operator-bearer"})
    await private_intake.record_private_steer(store, session_id=PRIVATE, text=CANARY, msg_id="m1", context=operator)
    yield
    await engine.dispose()


class _Request:
    """Minimal ModelRequest stand-in (the test_prompt_capture_middleware shape)."""

    def __init__(self, system):
        self.model = SimpleNamespace(model_name="claude-opus-4-7")
        self.system_message = SystemMessage(content=system)
        self.state = {}

    def override(self, **changes):
        request = _Request(self.system_message.content)
        for key, value in changes.items():
            setattr(request, key, value)
        return request


class _Checkpointer:
    def __init__(self, messages):
        self._messages = messages

    def get_tuple(self, config):
        return SimpleNamespace(checkpoint={"channel_values": {"messages": self._messages, "incognito": False}})


def _outcome(session_id, task_id):
    return SimpleNamespace(
        context_id=session_id, task_id=task_id, state="completed", models=["claude-opus-4-8"],
        origin="scheduler", trigger="job-9", priority="", cost_usd=0.02, duration_ms=1500,
        llm_calls=2, tool_calls=1,
    )


async def _turn(session_id, task_id, text):
    """One ordinary (``incognito=False``) turn carrying ``text`` through every content path:
    the trace's input, a tool call's args/result, the answer, the captured system prompt and
    the checkpoint the fleet export reads."""
    async with tracing.trace_session(session_id, name="chat", input=text, incognito=False):
        tracing.trace_tool_call("lookup", {"q": text}, f"result for {text}", 5, True)
        with request_metadata_scope({"a2a.task_id": task_id}):
            PromptCaptureMiddleware().wrap_model_call(
                _Request(f"SYSTEM {text}"), lambda r: SimpleNamespace(result=[AIMessage(content="ok")])
            )
        tracing.set_session_output(f"answer to {text}")
    messages = [HumanMessage(content=text), AIMessage(content=f"answer to {text}")]
    trace_export.export_turn(
        _outcome(session_id, task_id), checkpointer=_Checkpointer(messages), graph_config=SimpleNamespace(thinking="on")
    )


def _exported(tmp_path) -> str:
    return "".join(path.read_text() for path in (tmp_path / "export").glob("*.jsonl"))


def _traced(client, span) -> str:
    return repr(span.update.call_args_list) + repr(client.start_observation.call_args_list)


@pytest.mark.parametrize("steer", ["recorded", "marked"], indirect=True)
async def test_a_private_steer_reaches_no_trace_capture_or_export(steer, langfuse, tmp_path):
    client, span = langfuse
    source = public_memory.source_for(PRIVATE)
    assert source is not None and source.traceable is False and source.messages == ()
    assert public_memory.content_public(PRIVATE) is False

    # The steered turn itself, then a later ordinary turn that still carries the steer in
    # its context: neither has the incognito flag, and neither records any content.
    await _turn(PRIVATE, "task-steer", CANARY)
    await _turn(PRIVATE, "task-later", f"follow-up quoting {CANARY}")

    assert client.start_as_current_observation.call_count == 2  # the turns ARE traced…
    assert CANARY not in _traced(client, span)  # …without content
    tool = client.start_observation.call_args.kwargs
    assert (tool["input"], tool["output"]) == (None, None)
    assert not any("input" in call.kwargs or "output" in call.kwargs for call in span.update.call_args_list)
    assert prompt_snapshots().calls_for_task("task-steer") == []
    assert prompt_snapshots().calls_for_task("task-later") == []
    assert _exported(tmp_path) == ""


@pytest.mark.parametrize("steer", ["recorded", "marked"], indirect=True)
async def test_a_conversation_with_no_private_steer_is_traced_as_before(steer, langfuse, tmp_path):
    """Another conversation's private steer (same store, same process) changes nothing here."""
    client, span = langfuse
    assert public_memory.source_for(PUBLIC) is None
    assert public_memory.content_public(PUBLIC) is True

    await _turn(PUBLIC, "task-public", "status?")

    span.update.assert_any_call(input="status?")
    span.update.assert_any_call(output="answer to status?")
    tool = client.start_observation.call_args.kwargs
    assert (tool["input"], tool["output"]) == ({"q": "status?"}, "result for status?")
    (call,) = prompt_snapshots().calls_for_task("task-public")
    assert "SYSTEM status?" in repr(call)
    assert "answer to status?" in _exported(tmp_path)


async def test_unknown_lineage_fails_closed(langfuse, tmp_path, monkeypatch):
    """Ownership that cannot be read is neither "private" nor "public": the reader raises,
    and every guard records nothing rather than assume the conversation is ordinary."""
    client, span = langfuse
    monkeypatch.setattr(STATE, "a2a_task_engine", object(), raising=False)  # not a readable store

    with pytest.raises(private_sessions.OwnershipUnknown):
        public_memory.source_for(PUBLIC)
    assert public_memory.content_public(PUBLIC) is False

    await _turn(PUBLIC, "task-unknown", "MAYBE_PRIVATE")

    assert "MAYBE_PRIVATE" not in _traced(client, span)
    assert prompt_snapshots().calls_for_task("task-unknown") == []
    assert _exported(tmp_path) == ""


def test_work_outside_a_conversation_is_unpartitioned():
    assert public_memory.source_for("") is None
    assert public_memory.content_public("") is True


@pytest.mark.parametrize("steer", ["marked"], indirect=True)
async def test_session_memory_keeps_its_own_private_steer_guards(steer):
    """The production reader's verdict is for observability. A steered conversation's
    existing card stays readable through ``public_card`` (the human operator may recall it;
    ``private_sessions`` hides it from everyone else), exactly as before the reader was bound."""
    card = {"session_id": PRIVATE, "gist": "written before the steer"}
    assert public_memory.public_card(card) is True
    state = {"messages": [HumanMessage(content="public")]}
    assert public_memory.public_state(state, PRIVATE) == (state, {})
