"""Workflow receipt provenance through real native tool middleware and persistence."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

import plugins.workflows as wf
from graph.middleware.audit import NativeToolReceiptMiddleware
from plugins.workflows.run_state import WorkflowRunStore

pytestmark = pytest.mark.platform_sensitive


class Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@tool
async def native_read(repo: str, path: str) -> str:
    """Read a fixture in a repository."""
    await asyncio.sleep(0)
    if path == "error":
        return ToolMessage(content="missing file", status="error", tool_call_id="same")
    return "" if path == "empty" else "PRIVATE FILE BODY"


def recipe(*steps):
    return {
        "name": "receipts",
        "steps": [{"id": sid, "subagent": "reader", "prompt": sid, **opts} for sid, opts in steps],
    }


def setup(monkeypatch, tmp_path, spec, runner):
    monkeypatch.setattr(wf.sdk, "subagent_types", lambda: {"reader"})
    monkeypatch.setattr(wf.sdk, "config", lambda: SimpleNamespace(subagent_max_concurrency=3))
    monkeypatch.setattr(wf.sdk, "run_subagent", runner)
    return SimpleNamespace(get=lambda name: spec), WorkflowRunStore(tmp_path)


async def native_runner(subagent_type, prompt, *, description, check_prompt_tools, session_id, profile=None):
    assert description.startswith("workflow receipts:")
    assert session_id.startswith("workflow:")
    messages = []
    if prompt != "empty":
        for args in (
            {"repo": "wrong/repo", "path": prompt},
            {"path": prompt},
            {"repo": "right/repo", "path": "empty"},
            {"repo": "right/repo", "path": "error"},
        ):
            messages.append(AIMessage(content="", tool_calls=[{"name": "native_read", "args": args, "id": "same"}]))
    messages.append(AIMessage(content="I reviewed everything using invented_tool"))
    graph = create_agent(
        Model(messages=iter(messages)), tools=[native_read], middleware=[NativeToolReceiptMiddleware()]
    )
    result = await graph.ainvoke({"messages": [{"role": "user", "content": prompt}]})
    return result["messages"][-1].content


async def test_native_parallel_calls_and_empty_capture(monkeypatch, tmp_path):
    reg, store = setup(monkeypatch, tmp_path, recipe(("a", {}), ("b", {}), ("empty", {})), native_runner)
    result = await wf._execute(reg, "receipts", {}, run_store=store)
    assert result["output"] == "I reviewed everything using invented_tool"
    receipts = result["tool_receipts"]
    assert receipts["schema_version"] == 1 and receipts["source"] == "native_audit"
    ids = set()
    for sid in ("a", "b"):
        receipt = receipts["steps"][sid]
        assert receipt["capture_status"] == "captured"
        assert receipt["capture_supported"]
        assert receipt["run_id"] == result["run_id"]
        assert receipt["execution_id"] == receipts["execution_id"]
        calls = result["tool_calls"][sid]
        assert calls == receipt["entries"]
        assert len(calls) == 4
        assert calls[0]["args"] == {"repo": "wrong/repo", "path": sid}
        assert "repo" not in calls[1]["args"]
        assert [c["result_state"] for c in calls] == ["success", "tool_error", "success", "tool_error"]
        assert calls[2]["result_metadata"]["content_items"] == 0
        assert all(c["step_id"] == sid and c["tool_call_id"] == "same" for c in calls)
        ids.update(c["execution_id"] for c in calls)
    assert len(ids) == 8
    assert receipts["steps"]["empty"]["capture_status"] == "captured"
    assert result["tool_calls"]["empty"] == []
    assert "PRIVATE FILE BODY" not in json.dumps(receipts)
    assert "invented_tool" not in json.dumps(receipts)
    assert store.load(result["run_id"])["tool_receipts"] == receipts


async def test_resume_keeps_historical_receipts_separate(monkeypatch, tmp_path):
    spec = recipe(("a", {}), ("b", {"gate": "human", "depends_on": ["a"]}))
    reg, store = setup(monkeypatch, tmp_path, spec, native_runner)
    first = await wf._execute(reg, "receipts", {}, run_store=store)
    assert first["paused"]
    assert first["tool_receipts"]["steps"]["b"]["capture_status"] == "not_executed"
    second = await wf._resume(reg, first["run_id"], "approve", run_store=store)
    envelope = second["tool_receipts"]
    assert envelope["execution_id"] != first["tool_receipts"]["execution_id"]
    assert envelope["steps"]["a"]["capture_status"] == "not_executed"
    assert second["tool_calls"]["a"] == []
    assert second["steps"]["a"] == first["steps"]["a"]
    assert envelope["previous_executions"][0]["steps"]["a"] == first["tool_receipts"]["steps"]["a"]
    assert envelope["steps"]["b"]["capture_status"] == "captured"
    previous_ids = {e["execution_id"] for e in first["tool_calls"]["a"]}
    resumed_ids = {e["execution_id"] for e in second["tool_calls"]["b"]}
    assert len(resumed_ids) == 4 and previous_ids.isdisjoint(resumed_ids)
    assert all(e["step_id"] == "b" and e["run_id"] == first["run_id"] for e in second["tool_calls"]["b"])
    assert store.load(first["run_id"])["tool_receipts"] == envelope


async def test_unsupported_and_seeded(monkeypatch, tmp_path):
    async def text_only(*args, **kwargs):
        return "native_read({repo: 'right/repo'})"

    reg, store = setup(monkeypatch, tmp_path, recipe(("a", {}), ("b", {})), text_only)
    result = await wf._execute(reg, "receipts", {}, run_store=store, seed_outputs={"a": "old"})
    assert result["tool_calls"] == {"a": [], "b": []}
    assert result["tool_receipts"]["steps"]["a"]["capture_status"] == "not_executed"
    assert result["tool_receipts"]["steps"]["b"]["capture_status"] == "unsupported"


@pytest.mark.parametrize("timeout", [False, True])
async def test_interruption_persists_partial(monkeypatch, tmp_path, timeout):
    entered = asyncio.Event()
    middleware = NativeToolReceiptMiddleware()

    async def hanging(*args, **kwargs):
        async def handler(request):
            entered.set()
            await asyncio.Event().wait()
            return ToolMessage(content="", tool_call_id="same")

        await middleware.awrap_tool_call(SimpleNamespace(tool_call={"name": "read", "id": "same", "args": {}}), handler)

    reg, store = setup(monkeypatch, tmp_path, recipe(("a", {"timeout": 0.02} if timeout else {})), hanging)
    task = asyncio.create_task(wf._execute(reg, "receipts", {}, run_store=store))
    await entered.wait()
    if timeout:
        result = await task
        assert result["degraded"] == ["a"]
        assert not result["failed"]
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    receipt = store.load(store.run_id)["tool_receipts"]["steps"]["a"]
    assert receipt["capture_status"] == "partial"
    assert receipt["entries"][0]["result_state"] == "cancelled"


def test_old_records_and_bounded_history(tmp_path):
    store = WorkflowRunStore(tmp_path)
    run_id = store.start("old", steps=[{"id": "a"}])
    assert store.load(run_id)["tool_receipts"]["steps"]["a"]["capture_status"] == "unsupported"
    for _ in range(12):
        store.begin_receipts([{"id": "a"}])
    envelope = store.load(run_id)["tool_receipts"]
    assert len(envelope["previous_executions"]) == 8
    assert envelope["dropped_execution_count"] == 3


async def test_bounded_redacted_evidence_survives_finish(monkeypatch, tmp_path):
    middleware = NativeToolReceiptMiddleware()

    async def many(*args, **kwargs):
        async def handler(request):
            return ToolMessage(content="PRIVATE BODY", tool_call_id="same")

        for _ in range(130):
            await middleware.awrap_tool_call(
                SimpleNamespace(
                    tool_call={
                        "name": "read",
                        "id": "same",
                        "args": {"password": "PRIVATE SECRET", "body": "PRIVATE BODY", "path": "x" * 600},
                    }
                ),
                handler,
            )
        return "text stays unchanged"

    reg, store = setup(monkeypatch, tmp_path, recipe(("a", {})), many)
    result = await wf._execute(reg, "receipts", {}, run_store=store)
    receipt = result["tool_receipts"]["steps"]["a"]
    assert receipt["capture_status"] == "partial"
    assert len(receipt["entries"]) <= 128
    assert receipt["dropped_count"] >= 2
    assert receipt["truncated_fields_count"] > 0
    record = store.path(result["run_id"]).read_text()
    assert "PRIVATE" not in record
    assert store.load(result["run_id"])["tool_calls"] == result["tool_calls"]
    assert result["output"] == "text stays unchanged"


async def test_reject_never_fabricates_calls(monkeypatch, tmp_path):
    reg, store = setup(monkeypatch, tmp_path, recipe(("a", {"gate": "human"})), native_runner)
    first = await wf._execute(reg, "receipts", {}, run_store=store)
    result = await wf._resume(reg, first["run_id"], "reject", run_store=store)
    assert result["failed"] == ["a"]
    assert result["tool_calls"] == {"a": []}
    assert result["tool_receipts"]["steps"]["a"]["capture_status"] == "not_executed"
