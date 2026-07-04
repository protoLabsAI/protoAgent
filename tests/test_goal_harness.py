"""Goal harness (ADR 0028/0067/0073) — drive EVERY goal "version" through the real
``GoalController.evaluate()`` with made-up, deterministic inputs and assert the lifecycle
outcome. This is the automated stand-in for hand-testing the goal form: it proves the system
works end to end for each verifier type + the completion contract.

Hermetic + CI-safe (no model, no network): ``command``/``test``/``data`` run for real (real
shell + real file); ``ci`` and ``llm`` are faked at their single reach-out point
(``tools.gh_cli.run_gh`` / ``graph.llm.create_llm``). Matrix per verifier type — MET → the
goal finishes ``achieved``; NOT-MET → the drive loop ``continue``s — plus the completion
contract injection, the contract-less backward-compat path, and budget exhaustion.

Run:  ``uv run python -m pytest tests/test_goal_harness.py -v``
"""

from __future__ import annotations

import json

import pytest

from graph.config import LangGraphConfig
from graph.goals.controller import GoalController
from graph.goals.store import GoalStore


def _ctrl(tmp_path, **overrides) -> GoalController:
    return GoalController(LangGraphConfig(**overrides), GoalStore(tmp_path))


def _set(c: GoalController, session: str, condition: str, verifier: dict, **kw) -> None:
    """Set an operator goal and assert it took (surfaces any goal-mode gating)."""
    ok, msg = c.set_goal_operator(session, condition, verifier, **kw)
    assert ok, f"set_goal_operator refused the goal: {msg}"


# ── fakes for the two non-deterministic verifiers ──────────────────────────
class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    """Stands in for the goal-eval model — returns the judge JSON the llm verifier parses."""

    def __init__(self, met: bool) -> None:
        self._met = met

    async def ainvoke(self, _messages, config=None):  # noqa: ARG002 — signature parity
        return _FakeResp(json.dumps({"met": self._met, "reason": "faked judge verdict"}))


@pytest.fixture
def fake_llm(monkeypatch):
    def _install(met: bool) -> None:
        monkeypatch.setattr("graph.llm.create_llm", lambda *a, **k: _FakeLLM(met))

    return _install


@pytest.fixture
def fake_ci(monkeypatch):
    def _install(green: bool) -> None:
        async def _run_gh(*_a, **_k):
            return (0 if green else 1, "checks: all green" if green else "checks: failing", "")

        monkeypatch.setattr("tools.gh_cli.run_gh", _run_gh)

    return _install


# ── the matrix: each verifier type, MET → achieved / NOT-MET → continue ─────
@pytest.mark.asyncio
async def test_command_met_achieves(tmp_path):
    c = _ctrl(tmp_path)
    _set(c, "s", "build green", {"type": "command", "command": "exit 0"})
    d = await c.evaluate("s", last_text="ran the build")
    assert d.action == "done" and d.state.status == "achieved"


@pytest.mark.asyncio
async def test_command_not_met_continues(tmp_path):
    c = _ctrl(tmp_path)
    _set(c, "s", "build green", {"type": "command", "command": "exit 1"})
    d = await c.evaluate("s", last_text="still broken")
    assert d.action == "continue"


@pytest.mark.asyncio
async def test_test_verifier_met_achieves(tmp_path):
    c = _ctrl(tmp_path)
    _set(c, "s", "tests pass", {"type": "test", "command": "exit 0"})
    d = await c.evaluate("s", last_text="green")
    assert d.action == "done" and d.state.status == "achieved"


@pytest.mark.asyncio
async def test_data_met_achieves(tmp_path):
    f = tmp_path / "out.txt"
    f.write_text("the DEPLOY succeeded\n")
    c = _ctrl(tmp_path)
    _set(c, "s", "deploy done", {"type": "data", "path": str(f), "contains": "DEPLOY"})
    d = await c.evaluate("s", last_text="wrote the file")
    assert d.action == "done" and d.state.status == "achieved"


@pytest.mark.asyncio
async def test_data_not_met_continues(tmp_path):
    f = tmp_path / "out.txt"
    f.write_text("nothing yet\n")
    c = _ctrl(tmp_path)
    _set(c, "s", "deploy done", {"type": "data", "path": str(f), "contains": "DEPLOY"})
    d = await c.evaluate("s", last_text="not yet")
    assert d.action == "continue"


@pytest.mark.asyncio
async def test_ci_green_achieves(tmp_path, fake_ci):
    fake_ci(green=True)
    c = _ctrl(tmp_path)
    _set(c, "s", "PR is green", {"type": "ci", "pr": 1785})
    d = await c.evaluate("s", last_text="pushed the fix")
    assert d.action == "done" and d.state.status == "achieved"


@pytest.mark.asyncio
async def test_ci_red_continues(tmp_path, fake_ci):
    fake_ci(green=False)
    c = _ctrl(tmp_path)
    _set(c, "s", "PR is green", {"type": "ci", "pr": 1785})
    d = await c.evaluate("s", last_text="still red")
    assert d.action == "continue"


@pytest.mark.asyncio
async def test_llm_met_achieves(tmp_path, fake_llm):
    fake_llm(met=True)
    c = _ctrl(tmp_path)
    _set(c, "s", "the doc reads well", {"type": "llm"})
    d = await c.evaluate("s", last_text="rewrote the doc")
    assert d.action == "done" and d.state.status == "achieved"


@pytest.mark.asyncio
async def test_llm_not_met_continues(tmp_path, fake_llm):
    fake_llm(met=False)
    c = _ctrl(tmp_path)
    _set(c, "s", "the doc reads well", {"type": "llm"})
    d = await c.evaluate("s", last_text="first draft")
    assert d.action == "continue"


# ── the completion contract (ADR 0073) flows into the continuation prompt ───
@pytest.mark.asyncio
async def test_contract_injected_into_continuation(tmp_path):
    c = _ctrl(tmp_path)
    _set(
        c,
        "s",
        "refactor the module",
        {"type": "command", "command": "exit 1"},
        outcome="module split cleanly",
        constraints=["do not change the public API"],
        boundaries=["graph/goals/ only"],
        stop_when="a test outside the module fails",
    )
    d = await c.evaluate("s", last_text="mid-refactor")
    assert d.action == "continue"
    assert "do not change the public API" in d.message
    assert "graph/goals/ only" in d.message
    assert "a test outside the module fails" in d.message


@pytest.mark.asyncio
async def test_contractless_goal_has_no_contract_block(tmp_path):
    c = _ctrl(tmp_path)
    _set(c, "s", "x", {"type": "command", "command": "exit 1"})
    d = await c.evaluate("s", last_text="working")
    assert d.action == "continue"
    assert "Contract for this goal" not in d.message  # backward-compat: no contract → no block


# ── the `/goal {json}` chat/eval path now carries the contract too (not just the API) ──
@pytest.mark.asyncio
async def test_goal_json_chat_path_carries_contract(tmp_path):
    c = _ctrl(tmp_path)
    spec = {
        "condition": "ship the fix",
        "verifier": {"type": "llm"},  # llm is chat-safe (command/test/ci are operator-only)
        "constraints": ["no breaking changes"],
        "boundaries": ["graph/ only"],
        "stop_when": "CI goes red",
    }
    reply = await c.parse_control("/goal " + json.dumps(spec), "s")
    assert reply and "goal set" in reply.lower()
    g = c.active_goal("s")
    assert g.constraints == ["no breaking changes"]
    assert g.boundaries == ["graph/ only"]
    assert g.stop_when == "CI goes red"
    assert g.has_contract


# ── lifecycle edge: the budget bounds a never-met goal ─────────────────────
@pytest.mark.asyncio
async def test_budget_exhaustion_finishes(tmp_path):
    c = _ctrl(tmp_path)
    _set(c, "s", "x", {"type": "command", "command": "exit 1"}, max_iterations=1)
    d = await c.evaluate("s", last_text="try 1")
    assert d.action == "done" and d.state.status == "exhausted"
