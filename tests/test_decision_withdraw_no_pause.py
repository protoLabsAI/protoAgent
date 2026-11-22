"""#5678 3b: the agent withdraws its own no-pause decision without starting a turn."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from graph import decision_resume
from graph.config import LangGraphConfig
from graph.decisions import DecisionStore
from runtime.state import STATE
from tools.lg_tools import withdraw_decision

OPTIONS = [{"id": "hold", "label": "Keep holding"}, {"id": "go", "label": "Release"}]
NOT_OWNED = "Error: decision does not belong to this agent or session"


@pytest.fixture
def store(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 11, 22, 12, tzinfo=UTC))
    ledger = DecisionStore(":memory:", clock=lambda: clock.now)
    monkeypatch.setattr(STATE, "decision_store", ledger)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(identity_name="release-agent"))
    return ledger


@pytest.fixture
def resume(monkeypatch):
    fake = AsyncMock(return_value=True)
    monkeypatch.setattr(decision_resume, "resume_parked_turn", fake)
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ["s", "other-session"])
async def test_owned_no_pause_withdraw_closes_record_without_resume(store, resume, session_id):
    # Owned by the same session, or by the same agent from another of its sessions.
    rec = store.create("s", "Release?", OPTIONS, agent="release-agent", thread_id="s")
    result = await withdraw_decision.ainvoke(
        {"decision_id": rec.id, "reason": "Build failed", "state": {"session_id": session_id}}
    )
    assert result == f"Decision {rec.id} withdrawn: Build failed"
    after = store.get(rec.id)
    assert (after.state, after.withdraw_reason) == ("withdrawn", "Build failed")
    resume.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["", "someone-else"])
async def test_unowned_no_pause_withdraw_is_refused_and_changes_nothing(store, resume, agent):
    rec = store.create("s", "Release?", OPTIONS, agent=agent, thread_id="s")
    before = store.get(rec.id)
    result = await withdraw_decision.ainvoke(
        {"decision_id": rec.id, "reason": "nope", "state": {"session_id": "other"}}
    )
    assert result == NOT_OWNED
    assert store.get(rec.id) == before
    assert store.get(rec.id).pending
    resume.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{"thread_id": "s", "interrupt_id": "int-1"}, {}])
async def test_paused_withdraw_still_runs_the_clearing_resume(store, resume, fields):
    # A pause-id record, and a legacy record with no thread (also paused), both clear
    # their parked checkpoint with the cancelled resume.
    rec = store.create("s", "Release?", OPTIONS, agent="release-agent", **fields)
    result = await withdraw_decision.ainvoke(
        {"decision_id": rec.id, "reason": "obsolete", "state": {"session_id": "s"}}
    )
    assert result == f"Decision {rec.id} withdrawn: obsolete"
    assert store.get(rec.id).state == "withdrawn"
    resume.assert_awaited_once_with("s", decision_resume.CANCELLED_RESUME)
