"""Process memory ceiling (#3365).

No OS-level RSS ceiling exists to lean on — Darwin refuses ``setrlimit(RLIMIT_RSS)``
outright, so launchd's resource-limit keys can't express one, and Linux ignores it.
A leak therefore has no backstop below the host running out of memory. These tests
pin the in-process substitute, and in particular that it stays *quiet and harmless*
by default: off unless configured, and warning-only unless exiting is asked for.
"""

from __future__ import annotations

import itertools
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from infra.memory_guard import EXIT_CODE, MemoryCeiling, read_rss_bytes

pytestmark = pytest.mark.platform_sensitive

_MB = 1024 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("incognito", [True, False, None])
@pytest.mark.parametrize("destination", ["private", "shared", "file", "background"])
async def test_incognito_prevents_memory_writes(tmp_path, monkeypatch, incognito, destination):
    from knowledge.memories import MemoryStore
    from knowledge.shared_profile import SharedProfileStore
    from knowledge.store import KnowledgeStore
    from tools.memory_tools import _build_memory_tools

    knowledge = KnowledgeStore(db_path=str(tmp_path / "agent.db"))
    memory = MemoryStore(db_path=str(tmp_path / "agent.db"))
    shared = SharedProfileStore(tmp_path / "commons")
    monkeypatch.setattr("runtime.state.STATE.shared_profile_store", shared, raising=False)
    background = SimpleNamespace(spawn_work=AsyncMock(return_value="job-test"))
    config = SimpleNamespace(memory_shared_profile=destination == "shared")
    tools = {t.name: t for t in _build_memory_tools(
        knowledge, config, memory_store=memory, background_mgr=background,
    )}
    state = {"session_id": "incognito-test"}
    if incognito is not None:
        state["incognito"] = incognito
    content = "The operator prefers concise answers."
    if destination in {"private", "shared"}:
        args = {"content": content, "kind": "profile", "state": state}
        if destination == "shared":
            args["shared"] = True
        result = await tools["memory_save"].ainvoke(args)
    else:
        document = tmp_path / "notes.txt"
        document.write_text(content)
        source = "https://example.com/notes" if destination == "background" else str(document)
        result = await tools["knowledge_ingest"].ainvoke({"source": source, "state": state})

    if incognito:
        assert result == "This is an incognito chat, so nothing is saved to memory."
    else:
        assert result.startswith({
            "private": "Saved memory #", "shared": "Proposed to the shared operator profile",
            "file": "Ingested ", "background": "Ingesting ",
        }[destination])
    assert len(memory.list()) == int(not incognito and destination == "private")
    assert len(shared.list()) == int(not incognito and destination == "shared")
    assert knowledge.stats()["total"] == int(not incognito and destination == "file")
    assert background.spawn_work.await_count == int(not incognito and destination == "background")


class _ToolFake(GenericFakeChatModel):
    """A scripted chat model dropped into ``create_agent`` via the patched
    ``graph.agent.create_llm`` — replays preset AIMessages (incl. tool calls) over the
    streaming path the subagent graph drives. Mirrors ``test_subagent_stream_isolation``."""

    def bind_tools(self, tools, **kwargs):
        return self

    def _chunk(self):
        msg = next(self.messages)
        return ChatGenerationChunk(
            message=AIMessageChunk(
                content=msg.content,
                tool_call_chunks=[
                    {"name": tc["name"], "args": json.dumps(tc["args"]), "id": tc["id"], "index": i}
                    for i, tc in enumerate(getattr(msg, "tool_calls", None) or [])
                ],
            )
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        yield self._chunk()

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        import asyncio

        await asyncio.sleep(0)
        yield self._chunk()


@pytest.mark.asyncio
@pytest.mark.parametrize("incognito", [True, False])
@pytest.mark.parametrize("dispatch", ["inline", "slash", "sdk"])
async def test_incognito_subagent_delegation_writes_no_memory(tmp_path, monkeypatch, incognito, dispatch):
    """ADR 0132 S1 / ADR 0069 D3b — the subagent leak the first fix missed.

    A subagent the lead delegates through the in-graph ``task`` path runs in a FRESH
    sub-graph seeded by ``_run_subagent_inner``. The parent chat's ``incognito`` flag must
    ride onto that sub-graph's state, or ``memory_save`` inside the subagent reads no flag
    (``state.get("incognito")`` is ``None``) and writes a memory the incognito chat was
    meant to leave no trace of. Drives the REAL subagent sub-graph with a scripted fake
    model and a live ``MemoryStore``; the only difference between the two runs is the flag,
    so the pair is non-vacuous (the non-incognito run proves the save actually fires)."""
    import graph.agent as agent_mod
    from graph.config import LangGraphConfig
    from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig
    from knowledge.memories import MemoryStore
    from langchain_core.messages import AIMessage
    from tools.memory_tools import _build_memory_tools

    memory = MemoryStore(db_path=str(tmp_path / "agent.db"))
    config = LangGraphConfig()
    # memory_save bound to the live store; the full memory toolset is the tool_map the
    # delegated subagent resolves its allowlist from.
    tool_map = {t.name: t for t in _build_memory_tools(None, config, memory_store=memory)}

    # A subagent explicitly granted the write tool (write tools are opt-in, #4327); its lone
    # scripted turn calls memory_save, then it answers.
    monkeypatch.setitem(
        SUBAGENT_REGISTRY,
        "memsaver",
        SubagentConfig(name="memsaver", description="saves a memory", system_prompt="Save it.", tools=["memory_save"]),
    )
    fake = _ToolFake(
        messages=itertools.chain(
            iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "memory_save",
                                "args": {"content": "The operator prefers concise answers.", "kind": "profile"},
                                "id": "m1",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="handled the request"),
                ]
            ),
            itertools.repeat(AIMessage(content="handled the request")),
        )
    )
    monkeypatch.setattr(agent_mod, "create_llm", lambda *a, **k: fake)

    if dispatch == "inline":
        result = await agent_mod._run_subagent_inner(
            config=config,
            tool_map=tool_map,
            available_subagents="memsaver",
            description="save a memory",
            prompt="save the operator's preference for concise answers",
            subagent_type="memsaver",
            session_id="chat-1",
            incognito=incognito,
        )

        assert "memsaver completed" in str(result)
    else:
        from runtime.state import STATE
        from server.chat_dispatch import _PreTurn, _pre_turn_dispatch

        monkeypatch.setattr(STATE, "graph_config", config)
        monkeypatch.setattr(STATE, "goal_controller", None)
        monkeypatch.setattr(STATE, "plugin_chat_commands", {})
        monkeypatch.setattr(agent_mod, "get_all_tools", lambda *a, **k: list(tool_map.values()))
        if dispatch == "sdk":
            import asyncio

            from graph import sdk
            from observability import tracing

            # Workflow steps copy the turn context into their own tasks. Exercise
            # the real SDK, subgraph and memory tool against the live store.
            async with tracing.trace_session("chat-1", incognito=incognito):
                result = await asyncio.create_task(sdk.run_subagent(
                    "memsaver", "save the operator preference", description="save a memory",
                    extra_tools=[],
                ))
            assert "memsaver completed" in result
        else:
            pre = _PreTurn("/memsaver save the operator preference")
            frames = [frame async for frame in _pre_turn_dispatch(pre, "chat-1", {"incognito": incognito})]
            assert pre.handled
            assert frames[-1][0] == "done"

    # An incognito chat leaves NO memory trail — even one written by a delegated subagent;
    # a normal chat writes exactly the one profile memory the subagent saved.
    saved = memory.list()
    assert len(saved) == (0 if incognito else 1)
    if not incognito:
        assert "concise answers" in saved[0].content


def test_disabled_by_default() -> None:
    """0 is off — the repo's convention for a threshold knob."""
    guard = MemoryCeiling(0)
    assert guard.enabled is False
    assert guard.evaluate(500 * _MB) == ("none", "")


def test_under_the_ceiling_is_silent() -> None:
    guard = MemoryCeiling(100)
    assert guard.evaluate(99 * _MB)[0] == "none"


def test_breach_warns_but_does_not_exit_by_default() -> None:
    """THE default-safety property: crossing the ceiling never kills the process
    unless the operator explicitly asked for that."""
    guard = MemoryCeiling(100)
    action, message = guard.evaluate(150 * _MB)
    assert action == "warn"
    assert "150" in message and "100" in message


def test_breach_exits_only_when_opted_in() -> None:
    guard = MemoryCeiling(100, exit_on_breach=True)
    action, message = guard.evaluate(150 * _MB)
    assert action == "exit"
    assert str(EXIT_CODE) in message


def test_breach_is_reported_once_per_episode() -> None:
    """A ceiling crossed at a 60s cadence must not emit a line a minute forever."""
    guard = MemoryCeiling(100)
    assert guard.evaluate(150 * _MB)[0] == "warn"
    assert guard.evaluate(160 * _MB)[0] == "none"
    assert guard.evaluate(900 * _MB)[0] == "none"


def test_recovery_rearms_the_warning() -> None:
    """Dropping back under is a genuine recovery, so a later breach is news again."""
    guard = MemoryCeiling(100)
    assert guard.evaluate(150 * _MB)[0] == "warn"
    assert guard.evaluate(50 * _MB)[0] == "none"  # recovered
    assert guard.evaluate(150 * _MB)[0] == "warn"  # new episode


def test_unreadable_rss_is_not_a_breach() -> None:
    """A platform with no reader must not be mistaken for a process at 0 bytes —
    reading nothing has to mean 'no opinion', never 'under the ceiling' and
    certainly never 'exit'."""
    guard = MemoryCeiling(100, exit_on_breach=True)
    assert guard.evaluate(None) == ("none", "")


@pytest.mark.parametrize("bad", ["", None, "not-a-number", -5])
def test_garbage_ceiling_disables_rather_than_raises(bad) -> None:
    """The value arrives from YAML, so it can be anything. A guard that raises on
    config would take down the loop it exists to protect."""
    assert MemoryCeiling(bad).enabled is False


@pytest.mark.skipif(
    not (sys.platform == "darwin" or sys.platform.startswith("linux")),
    reason="no RSS reader wired for this platform (guard self-disables there)",
)
def test_reads_a_plausible_rss_on_supported_platforms() -> None:
    rss = read_rss_bytes()
    assert rss is not None
    # A live CPython running a test suite is comfortably inside this range; the
    # point is to catch a unit mix-up (KB read as bytes, or pages as bytes).
    assert 5 * _MB < rss < 20_000 * _MB, f"implausible RSS: {rss}"


def test_config_knob_defaults_to_off() -> None:
    """Wiring check: the dataclass default must keep the guard inert, so merely
    upgrading never changes how an existing deployment behaves."""
    from graph.config import LangGraphConfig

    cfg = LangGraphConfig()
    assert cfg.memory_ceiling_mb == 0
    assert cfg.memory_ceiling_exit is False
    assert MemoryCeiling(cfg.memory_ceiling_mb, exit_on_breach=cfg.memory_ceiling_exit).enabled is False


# ── Config values arrive from YAML, so "anything at all" is the input domain ──
# (#3373 review). Both knobs must fail SAFE: the ceiling disables, and the exit
# flag stays off, rather than raising into the loop or arming a process kill.


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_ceiling_disables_rather_than_raising(bad) -> None:
    """int(float('inf')) raises OverflowError — which the guard must absorb."""
    assert MemoryCeiling(bad).enabled is False


@pytest.mark.parametrize("truthy_string", ["true", "True", " TRUE "])
def test_quoted_true_is_honored(truthy_string) -> None:
    """A quoted YAML bool is the one realistic mistake worth accepting."""
    assert MemoryCeiling(100, exit_on_breach=truthy_string).exit_on_breach is True


@pytest.mark.parametrize("falsey", ["false", "False", " false ", "", None, 0])
def test_quoted_false_and_absent_stay_off(falsey) -> None:
    """`bool("false")` is True — which would arm a process kill from a config that
    plainly reads false. This is the whole reason the flag isn't parsed with bool()."""
    assert MemoryCeiling(100, exit_on_breach=falsey).exit_on_breach is False


@pytest.mark.parametrize("junk", ["yes", "1", "on", 1, 2.5, [], {"a": 1}, object()])
def test_unrecognized_exit_values_fail_safe(junk) -> None:
    """Anything not plainly a boolean leaves the process-killing switch OFF."""
    assert MemoryCeiling(100, exit_on_breach=junk).exit_on_breach is False


def test_a_breach_with_a_junk_exit_flag_only_warns() -> None:
    """End to end: the fail-safe parse must actually keep os._exit out of reach."""
    guard = MemoryCeiling(100, exit_on_breach="false")
    assert guard.evaluate(500 * _MB)[0] == "warn"


def test_guard_is_cancelled_before_shutdown_awaits_anything() -> None:
    """The memory guard must be cancelled before the first await in shutdown (#3365).

    It is the only background task that can end the process outright (`os._exit` on
    breach). Every cleanup step in `_scheduler_shutdown` yields to the loop — fleet
    members, mDNS withdrawal, surface stops, scheduler, cache warmer — so a guard
    still running through them can exit mid-teardown and skip the rest. Shutdown is
    also when a long-lived process sits closest to its ceiling, so the race is real.

    Source inspection rather than behavior: the ordering is the invariant, and it
    would otherwise regress silently the next time someone tidies that function
    (this test exists because it already did once). Same approach as
    `tests/test_tracing.py`'s boot-order assertion.
    """
    import inspect
    import re

    import server

    src = inspect.getsource(server._main)
    body = src[src.index("async def _scheduler_shutdown") :]
    body = body[: body.index("\n    @app.on_event") if "\n    @app.on_event" in body else len(body)]

    cancel = body.index("STATE.memory_guard_task.cancel()")
    first_await = re.search(r"\bawait\b", body)
    assert first_await, "shutdown no longer awaits anything — re-check this invariant"
    assert cancel < first_await.start(), (
        "memory_guard_task must be cancelled BEFORE the first await in "
        "_scheduler_shutdown — otherwise a breach mid-teardown os._exit()s and "
        "skips the remaining cleanup."
    )
