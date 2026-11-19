"""Unattended approval policy: allow / ask / never for autonomous origins (ADR 0124 D5, #5541).

Driven through the real non-streaming turn driver against a real ``create_agent`` graph
(in-memory checkpoints, the real tool-call middleware, a scripted model, no gateway) and an
in-memory decisions ledger.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt

from graph import unattended_policy as policy
from graph.config import LangGraphConfig
from graph.decisions import STATE_ANSWERED, DecisionStore
from graph.middleware.unattended_policy import UnattendedPolicyMiddleware
from runtime.state import STATE

goal_loop = importlib.import_module("server.goal_loop")
turn_control = importlib.import_module("server.turn_control")

CONFIG = {"configurable": {"thread_id": "a2a:s-policy"}}
ARGS = {"command": "rm -rf build"}


class ScriptedModel(BaseChatModel):
    """Calls ``gated`` once, then replies with the tool's result."""

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            reply = AIMessage(content=f"Tool said: {last.content}")
        else:
            reply = AIMessage(content="", tool_calls=[{"name": "gated", "args": ARGS, "id": "call-1"}])
        return ChatResult(generations=[ChatGeneration(message=reply)])


@pytest.fixture
def env(monkeypatch):
    """A graph whose one tool pauses for approval, plus the list of commands it really ran."""
    ran: list[str] = []
    cards: list[dict] = []
    cfg = LangGraphConfig()

    @tool
    def gated(command: str) -> str:
        """Run a command after approval."""
        card = {"kind": "approval", "title": "Approve shell command?", "detail": command}
        cards.append(card)
        decision = interrupt(card)
        if not (isinstance(decision, dict) and decision.get("approved") is True) and decision != "approve":
            return f"declined: {command}"
        ran.append(command)
        return f"ran: {command}"

    graph = create_agent(
        ScriptedModel(), [gated], middleware=[UnattendedPolicyMiddleware(cfg)], checkpointer=InMemorySaver()
    )
    store = DecisionStore(":memory:")
    monkeypatch.setattr(STATE, "graph", graph, raising=False)
    monkeypatch.setattr(STATE, "decision_store", store, raising=False)
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(STATE, "graph_config", None, raising=False)
    return type("Env", (), {"cfg": cfg, "ran": ran, "store": store, "graph": graph, "cards": cards})


async def _turn(origin="scheduler", *, resume=False, message="tidy up"):
    from server.turn_sync import _native_turn

    return await _native_turn(
        message, None, session_id="s-policy", config=CONFIG, usage_cb=UsageMetadataCallbackHandler(),
        state_extra={}, tool_fence=None, hitl_resume=resume, incognito=False, origin=origin,
    )


async def _pending(env):
    return [i for t in (await env.graph.aget_state(CONFIG)).tasks for i in t.interrupts]


@pytest.mark.asyncio
async def test_allow_tier_runs_the_tool_without_a_decision(env):
    env.cfg.unattended_allow = ["gated"]
    reply = await _turn()
    assert env.ran == ["rm -rf build"]
    assert reply[-1]["content"] == "Tool said: ran: rm -rf build"
    assert "hitl" not in reply[-1]
    assert env.store.list() == []
    assert await _pending(env) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("never", [[], ["gated"]])
async def test_never_tier_and_no_policy_are_refused_without_parking(env, never):
    env.cfg.unattended_never = never
    reply = await _turn()
    assert env.ran == []
    assert "Refused by the unattended approval policy: gated was not run" in reply[-1]["content"]
    assert ("never tier" in reply[-1]["content"]) is bool(never)
    assert ("default is deny" in reply[-1]["content"]) is not never
    assert "hitl" not in reply[-1]
    assert env.store.list() == []
    assert await _pending(env) == []
    # An explicit never refuses before the tool body runs at all.
    assert len(env.cards) == (0 if never else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("option,ran", [("approve", True), ("deny", False)])
async def test_ask_tier_parks_one_decision_and_resumes_on_the_answer(env, option, ran):
    env.cfg.unattended_ask = ["gated"]
    reply = await _turn()
    assert env.ran == []
    (rec,) = env.store.list()
    assert rec.pending
    assert "gated" in rec.question and '"command": "rm -rf build"' in rec.question
    assert "Tool: gated" in rec.context and "Approve shell command?" in rec.context
    assert [o["id"] for o in rec.options] == ["approve", "deny"]
    hitl = reply[-1]["hitl"]
    assert hitl["kind"] == "decision" and hitl["decision_id"] == rec.id
    # A later unattended fire on the same chat holds behind the pending decision: still one.
    await _turn(message="next scheduled fire")
    assert len(env.store.list()) == 1 and env.ran == []

    # The decisions route resumes the parked turn as the operator, with this exact text.
    done = await _turn("", resume=True, message=f"Operator chose: {rec.option_label(option)} ({option})")
    assert env.ran == (["rm -rf build"] if ran else [])
    assert done[-1]["content"] == f"Tool said: {'ran' if ran else 'declined'}: rm -rf build"
    assert env.store.get(rec.id).state == STATE_ANSWERED
    assert len(env.store.list()) == 1
    assert await _pending(env) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["unattended_allow", "unattended_ask", "unattended_never"])
async def test_operator_turn_approvals_are_unchanged(env, tier):
    setattr(env.cfg, tier, ["gated"])
    reply = await _turn("")
    (pending,) = await _pending(env)
    assert pending.value == {"kind": "approval", "title": "Approve shell command?", "detail": "rm -rf build"}
    assert "hitl" not in reply[-1] and env.store.list() == [] and env.ran == []
    done = await _turn("", resume=True, message="approve")
    assert env.ran == ["rm -rf build"]
    assert done[-1]["content"] == "Tool said: ran: rm -rf build"


def test_hub_allows_only_starting_a_dead_member_by_default(monkeypatch):
    cfg = LangGraphConfig()
    assert (cfg.unattended_allow, cfg.unattended_ask, cfg.unattended_never) == ([], [], [])
    assert policy.resolve_tier(cfg, "fleet_member_restart", "start", hub=True) == policy.ALLOW
    assert policy.resolve_tier(cfg, "fleet_member_restart", "restart", hub=True) is None
    assert policy.resolve_tier(cfg, "fleet_member_restart", hub=True) is None
    assert policy.resolve_tier(cfg, "fleet_member_restart", "start", hub=False) is None
    for other in ("run_command", "self_restart", "delete_file", "ask_human"):
        assert policy.resolve_tier(cfg, other, hub=True) is None
    assert policy.HUB_ALLOW == ("fleet_member_restart:start",)
    # The hub is whatever is not a fleet member, read live.
    monkeypatch.setattr("graph.workspaces.manager.is_workspace_member", lambda: True)
    assert policy.resolve_tier(cfg, "fleet_member_restart", "start") is None
    monkeypatch.setattr("graph.workspaces.manager.is_workspace_member", lambda: False)
    assert policy.resolve_tier(cfg, "fleet_member_restart", "start") == policy.ALLOW
    # An operator entry for the tool replaces the built-in allowance.
    cfg.unattended_ask = ["fleet_member_restart"]
    assert policy.resolve_tier(cfg, "fleet_member_restart", "start", hub=True) == policy.ASK


def test_tier_precedence_action_beats_tool_and_stricter_wins():
    cfg = LangGraphConfig(
        unattended_allow=["t:start", "both", "plain"], unattended_ask=["t", "both"], unattended_never=["t:wipe", "x"]
    )
    assert policy.resolve_tier(cfg, "t", "start") == policy.ALLOW
    assert policy.resolve_tier(cfg, "t", "wipe") == policy.NEVER
    assert policy.resolve_tier(cfg, "t", "other") == policy.ASK
    assert policy.resolve_tier(cfg, "both") == policy.ASK
    assert policy.resolve_tier(cfg, "plain") == policy.ALLOW
    assert policy.resolve_tier(cfg, "unlisted") is None
    assert policy.has_action_entries(cfg, "t") and not policy.has_action_entries(cfg, "x")


def test_answers_translate_only_for_a_policy_decision():
    asked = policy.ask_payload({"kind": "approval", "title": "T"}, tool="gated", args=ARGS, call_id="c")
    assert policy.resume_value(asked, "Operator chose: Approve (approve)") == {"approved": True, "decision": "approve"}
    for text in ("Operator chose: Deny (deny)", "Decision cancelled by the operator.", "sure, approve (not)", ""):
        assert policy.resume_value(asked, text)["approved"] is False
    # request_decision's own payload, and a question the policy turned into a decision, pass through.
    assert policy.resume_value({"kind": "decision", "question": "q"}, "Operator chose: A (approve)") == (
        "Operator chose: A (approve)"
    )
    question = policy.ask_payload({"question": "Which branch?"}, tool="ask_human", args={})
    assert question["question"] == "Which branch?" and question["options"] == []
    assert policy.resume_value(question, "main") == "main"
    # Secrets in the arguments never reach the decision text.
    shown = policy.ask_payload({"kind": "approval"}, tool="t", args={"api_key": "sk-" + "a" * 40})["question"]
    assert "a" * 40 not in shown


def test_driver_policy_approves_an_allowed_call_once_then_refuses(monkeypatch):
    monkeypatch.setattr(turn_control, "_MAX_UNATTENDED_REFUSALS", 1)
    stamped = policy.allow_payload({"kind": "approval"}, tool="t", args={}, action="start", call_id="c1")
    auto = goal_loop.UnattendedPolicy(True)
    assert auto.on_interrupt(stamped) == goal_loop.ALLOW
    assert auto.answer(stamped) == {"approved": True, "decision": "approve", "unattended": True, "action": "start"}
    assert auto.answered == 0  # an approval spends no refusal
    # The same call pausing again did not accept the approval: refused, then given up on.
    assert auto.on_interrupt(stamped) == goal_loop.ANSWER
    assert auto.answer(stamped) is turn_control._AUTONOMOUS_HITL_SENTINEL
    assert auto.on_interrupt(stamped) == goal_loop.GIVE_UP
    # An unstamped pause is refused, and an attended turn parks whatever the stamp says.
    assert goal_loop.UnattendedPolicy(True).on_interrupt({"kind": "approval"}) == goal_loop.ANSWER
    assert goal_loop.UnattendedPolicy(False).on_interrupt(stamped) == goal_loop.PARK
    with goal_loop.UnattendedPolicy(False).scope():
        assert not policy.is_unattended_turn()
    with auto.scope():
        assert policy.is_unattended_turn()
    assert not policy.is_unattended_turn()


@pytest.mark.asyncio
@pytest.mark.parametrize("tier,ran,parked", [("unattended_allow", True, False), (None, False, False),
                                             ("unattended_ask", False, True)])
async def test_streaming_driver_applies_the_same_tiers(env, tier, ran, parked):
    chat_mod = importlib.import_module("server.chat")
    if tier:
        setattr(env.cfg, tier, ["gated"])
    frames = [
        f async for f in chat_mod._chat_langgraph_stream("tidy up", "s-policy", request_metadata={"origin": "scheduler"})
    ]
    assert env.ran == (["rm -rf build"] if ran else [])
    asks = [payload for kind, payload in frames if kind == "input_required"]
    assert [a.get("kind") for a in asks] == (["decision"] if parked else [])
    assert len(env.store.list()) == int(parked)
    if not parked:
        assert frames[-1][0] == "done"
        results = [m.content for m in (await env.graph.aget_state(CONFIG)).values["messages"]
                   if isinstance(m, ToolMessage)]
        assert len(results) == 1
        assert results[0].startswith("ran: rm -rf build" if ran else "Refused by the unattended approval policy")
    assert not policy.is_unattended_turn()
