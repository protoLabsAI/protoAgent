"""``max_turns`` is a budget of TOOL ROUNDS, not LangGraph super-steps.

The subagent runner used to pass ``SubagentConfig.max_turns`` straight through as
LangGraph's ``recursion_limit``. That limit counts one step per graph NODE executed:
``__start__``, ``model``, ``tools``, and every middleware ``before_model`` /
``after_model`` hook, each compiled into its own node. So a subagent got about half its
``max_turns`` as tool rounds on a bare stack, and about a third once #3199 added a
``before_model`` node to the subagent stack. ``max_turns=4`` then hard-stopped right
after its first tool result. The ``coder`` face, whose whole job is "call
``coder_solve`` once, relay it", could no longer do that, and pr-reviewer-plugin#119's
structural lane is the visible case.

These tests drive the REAL runner: real ``create_agent``, the real subagent middleware
stack, a real tool. Only the chat model is scripted, so the budget is measured against
the graph that actually runs, not a mock of it. They go through every funnel that
applies ``max_turns``: ``_run_subagent`` directly, the ``task`` / ``task_batch`` tools,
and ``run_manual_subagent`` (console fan-out, slash commands, ``sdk.run_subagent`` and
therefore every workflows-plugin step).
"""

from __future__ import annotations

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

import graph.agent as agent_mod
from graph.config import LangGraphConfig
from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig

PROBE = "budget-probe"


class _ScriptedModel(BaseChatModel):
    """Makes ``rounds`` tool rounds (one ``ping`` call each), then answers in text."""

    rounds: int = 0
    calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        i = self.calls
        self.calls += 1
        if i < self.rounds:
            msg = AIMessage(content="", tool_calls=[{"name": "ping", "args": {}, "id": f"call-{i}"}])
        else:
            msg = AIMessage(content="FINAL ANSWER")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-rounds"


@pytest.fixture
def probe(monkeypatch):
    """Register a probe subagent and script its model. Returns ``arm(max_turns, rounds)``,
    which (re)configures both and hands back the tool-execution log and the models built."""
    executed: list[str] = []
    models: list[_ScriptedModel] = []
    state = {"rounds": 0}

    @tool
    def ping() -> str:
        """Return pong."""
        executed.append("ping")
        return "pong"

    def _create_llm(*_a, **_k):
        m = _ScriptedModel(rounds=state["rounds"])
        models.append(m)
        return m

    monkeypatch.setattr(agent_mod, "create_llm", _create_llm)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def arm(max_turns: int, rounds: int):
        state["rounds"] = rounds
        monkeypatch.setitem(
            SUBAGENT_REGISTRY,
            PROBE,
            SubagentConfig(name=PROBE, description="d", system_prompt="p", tools=["ping"], max_turns=max_turns),
        )
        return ping, executed, models

    return arm


async def _run(ping, config=None) -> str:
    return await agent_mod._run_subagent(
        config=config or LangGraphConfig(),
        tool_map={"ping": ping},
        available_subagents=PROBE,
        description="budget",
        prompt="go",
        subagent_type=PROBE,
    )


def _completed(out: str) -> bool:
    return out.startswith(f"[{PROBE} completed: budget]") and "FINAL ANSWER" in out


# ── the direct runner ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("max_turns", [1, 2, 4, 8])
async def test_max_turns_n_allows_n_tool_rounds_then_an_answer(probe, max_turns):
    ping, executed, models = probe(max_turns, rounds=max_turns)
    out = await _run(ping)
    assert _completed(out), out
    assert len(executed) == max_turns
    assert models[-1].calls == max_turns + 1  # N tool rounds + the answering call


def _assert_stopped_after_round(out: str, executed: list, models: list, max_turns: int) -> None:
    """Round N+1 is refused. Its model call runs (it might have been the answer), and
    LangGraph lets one node past the limit execute before raising, so that round's tools
    may run. But the model NEVER sees their results (no call N+2), and the existing
    hard-stop salvage reports the lane instead of a completion."""
    assert "hard-stopped at max_turns" in out, out
    assert models[-1].calls == max_turns + 1
    assert max_turns <= len(executed) <= max_turns + 1


@pytest.mark.parametrize("max_turns", [1, 2, 4, 8])
async def test_max_turns_is_still_a_limit(probe, max_turns):
    ping, executed, models = probe(max_turns, rounds=max_turns + 1)
    out = await _run(ping)
    _assert_stopped_after_round(out, executed, models, max_turns)


async def test_prompt_capture_stack_keeps_the_same_budget(probe):
    ping, executed, _ = probe(3, rounds=3)
    cfg = LangGraphConfig()
    cfg.prompt_capture_enabled = True
    assert _completed(await _run(ping, cfg))
    assert len(executed) == 3


# ── the budget follows the stack, whatever gets added to it later ───────────────


class _ExtraBeforeModel(AgentMiddleware):
    def before_model(self, state, runtime):
        return None


class _ExtraAfterModel(AgentMiddleware):
    def after_model(self, state, runtime):
        return None


class _ExtraBeforeAndAfterModel(AgentMiddleware):
    def before_model(self, state, runtime):
        return None

    def after_model(self, state, runtime):
        return None


class _ExtraAgentHooks(AgentMiddleware):
    def before_agent(self, state, runtime):
        return None

    def after_agent(self, state, runtime):
        return None


@pytest.fixture
def grown_stack(monkeypatch):
    """Append middleware with every node-producing hook to the REAL subagent stack
    (through the provider-shape seam the runner already calls). This is what #3199 did
    by accident with one ``before_model`` node, and a hand-maintained steps-per-turn
    constant would silently under-budget it."""
    real = agent_mod.provider_shape_middleware

    def _grown(config):
        return [
            *real(config),
            _ExtraBeforeModel(),
            _ExtraAfterModel(),
            _ExtraBeforeAndAfterModel(),
            _ExtraAgentHooks(),
        ]

    monkeypatch.setattr(agent_mod, "provider_shape_middleware", _grown)


@pytest.mark.parametrize("max_turns", [1, 3])
async def test_budget_survives_a_grown_middleware_stack(probe, grown_stack, max_turns):
    ping, executed, _ = probe(max_turns, rounds=max_turns)
    out = await _run(ping)
    assert _completed(out), out
    assert len(executed) == max_turns

    ping, executed, models = probe(max_turns, rounds=max_turns + 1)
    executed.clear()
    out = await _run(ping)
    _assert_stopped_after_round(out, executed, models, max_turns)


# ── a shipped subagent sized to its job ─────────────────────────────────────────


async def test_coder_face_can_call_coder_solve_and_relay(monkeypatch):
    """ADR 0064's ``coder`` face calls ``coder_solve`` exactly once and relays the result,
    and it ships with ``max_turns=4``. Under the raw step budget that one call hard-stopped
    before the relay (no salvageable text, so the lead got a Gap instead of the solution)."""
    from plugins.coder.subagent import build_coder_subagent

    cfg = build_coder_subagent()
    assert cfg is not None
    monkeypatch.setitem(SUBAGENT_REGISTRY, "coder", cfg)

    @tool
    def coder_solve(task: str = "", tests: str = "") -> str:
        """Stand-in for the execution-grounded ladder."""
        return "PASSED 3/3"

    class _Relay(_ScriptedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            i = self.calls
            self.calls += 1
            if i == 0:
                msg = AIMessage(content="", tool_calls=[{"name": "coder_solve", "args": {"task": "x"}, "id": "cs-0"}])
            else:
                msg = AIMessage(content="Solution verified: PASSED 3/3")
            return ChatResult(generations=[ChatGeneration(message=msg)])

    monkeypatch.setattr(agent_mod, "create_llm", lambda *_a, **_k: _Relay())
    out = await agent_mod._run_subagent(
        config=LangGraphConfig(),
        tool_map={"coder_solve": coder_solve},
        available_subagents="coder",
        description="solve",
        prompt="implement x",
        subagent_type="coder",
    )
    assert out.startswith("[coder completed: solve]"), out
    assert "PASSED 3/3" in out


# ── the other funnels: task / task_batch / run_manual_subagent ──────────────────


async def test_task_tool_honours_max_turns(probe):
    ping, executed, _ = probe(3, rounds=3)
    tools = {t.name: t for t in agent_mod._build_task_tools(LangGraphConfig(), [ping])}
    out = await tools["task"].ainvoke(
        {
            "name": "task",
            "args": {"description": "budget", "prompt": "go", "subagent_type": PROBE},
            "id": "tc-budget",
            "type": "tool_call",
        }
    )
    body = getattr(out, "content", out)
    assert _completed(body), body
    assert len(executed) == 3


async def test_task_batch_honours_max_turns_per_member(probe):
    ping, executed, _ = probe(2, rounds=2)
    tools = {t.name: t for t in agent_mod._build_task_tools(LangGraphConfig(), [ping])}
    out = await tools["task_batch"].ainvoke(
        {
            "name": "task_batch",
            "args": {
                "tasks": [
                    {"description": "budget", "prompt": "a", "subagent_type": PROBE},
                    {"description": "budget", "prompt": "b", "subagent_type": PROBE},
                ]
            },
            "id": "tb-budget",
            "type": "tool_call",
        }
    )
    body = getattr(out, "content", out)
    assert body.count(f"[{PROBE} completed: budget]") == 2, body
    assert "hard-stopped" not in body
    assert len(executed) == 4


async def test_run_manual_subagent_honours_max_turns(probe, monkeypatch):
    """The out-of-graph runner behind the console fan-out, ``/<subagent>`` slash commands
    and ``graph.sdk.run_subagent`` (every workflows-plugin step)."""
    ping, executed, _ = probe(3, rounds=3)
    monkeypatch.setattr(agent_mod, "get_all_tools", lambda *_a, **_k: [])
    out = await agent_mod.run_manual_subagent(
        LangGraphConfig(),
        description="budget",
        prompt="go",
        subagent_type=PROBE,
        extra_tools=[ping],
    )
    assert _completed(out), out
    assert len(executed) == 3
