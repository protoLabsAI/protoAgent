"""`sdk.run_subagent(session_id=…)` stamps the delegation's ledger edge, so a run driven
through the SDK (the workflows plugin's per-step runner) joins back to the caller it
belongs to instead of landing with an empty ``session_id`` (#4612 part 2).

The model is faked at the ``_run_subagent_inner`` boundary — the same seam the existing
ledger-producer tests use — so these exercise the real session_id threading
``sdk.run_subagent`` → ``run_manual_subagent`` → ``_run_subagent`` → ``record_delegation``
without a live gateway call.
"""

from __future__ import annotations

import pytest

import graph.agent as agent
import graph.sdk as sdk
import runtime.state as rs
from graph.config import LangGraphConfig
from observability.ledger_store import LedgerStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    """A throwaway ledger + a real (filesystem-off) config on STATE, as the host wires at
    boot — ``sdk.run_subagent`` reads ``STATE.graph_config`` and records onto
    ``STATE.ledger_store``."""
    store = LedgerStore(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(rs.STATE, "ledger_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(filesystem_enabled=False), raising=False)
    # The model + tool wiring is irrelevant to session_id threading — stub it out so the
    # run never reaches a gateway.
    monkeypatch.setattr(agent, "create_llm", lambda *_a, **_kw: object())
    monkeypatch.setattr(agent, "get_all_tools", lambda *_a, **_kw: [])

    async def _inner(**_kw):
        return "[researcher completed: dig] -- done"

    monkeypatch.setattr(agent, "_run_subagent_inner", _inner)
    return store


async def test_session_id_lands_on_the_delegation_edge(ledger_db):
    # r1: an explicit session_id is stamped on the row the SDK run writes.
    await sdk.run_subagent(
        "researcher",
        "go",
        description="dig into the thing",
        extra_tools=[],
        check_prompt_tools=False,
        session_id="wf-123",
    )

    rows = ledger_db.recent()
    assert len(rows) == 1
    assert rows[0]["to_kind"] == "subagent"
    assert rows[0]["to_name"] == "researcher"
    assert rows[0]["session_id"] == "wf-123"
    assert rows[0]["outcome"] == "ok"


async def test_default_call_leaves_the_session_id_empty(ledger_db):
    # r2: the default keeps the prior behaviour — no session_id, empty edge.
    await sdk.run_subagent(
        "researcher",
        "go",
        description="dig into the thing",
        extra_tools=[],
        check_prompt_tools=False,
    )

    assert ledger_db.recent()[0]["session_id"] == ""


async def test_workflow_step_records_its_run_identity(tmp_path, monkeypatch):
    # r3: a workflow step run through the plugin stamps `workflow:<run_id>` on its edge, so
    # the review-gate subagent joins back to the workflow run (via `task_id = run_id`).
    import plugins.workflows as wf
    from plugins.workflows.run_state import WorkflowRunStore
    from tests.test_workflows import _GatedReg, _patch_sdk

    seen: dict[str, str] = {}

    async def run_subagent(subagent_type, prompt, description="", *, session_id="", **_sdk_kwargs):
        seen[description] = session_id
        return f"<{description.rsplit(':', 1)[-1]}-out>"

    _patch_sdk(monkeypatch, run_subagent)
    store = WorkflowRunStore(tmp_path)
    await wf._execute(_GatedReg(), "gated", {"topic": "ai"}, run_store=store)

    # The one step that ran before the human gate carries the run's identity, non-empty.
    assert seen == {"workflow gated:gather": f"workflow:{store.run_id}"}
    assert store.run_id and seen["workflow gated:gather"] != "workflow:"
