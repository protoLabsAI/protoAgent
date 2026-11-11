"""Native receipts observe handlers, never model claims or audit summaries."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from graph import sdk
from graph.middleware import audit
from graph.middleware.audit import AuditMiddleware, NativeToolReceiptMiddleware


@pytest.fixture(autouse=True)
def quiet_audit(monkeypatch):
    monkeypatch.setattr(
        audit,
        "_observability",
        lambda: (
            audit._NoopAudit,
            audit._NoopTracing,
            audit._NoopMetrics,
        ),
    )
    monkeypatch.setattr(audit, "_record_tool_telemetry", lambda *args: None)


def request(args=None):
    return SimpleNamespace(tool_call={"name": "read", "id": "repeated", "args": args or {}})


@pytest.mark.parametrize("state", ["success", "tool_error", "exception", "cancelled"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_each_physical_execution(state, asynchronous):
    middleware = NativeToolReceiptMiddleware()
    count = 0
    result = ToolMessage(
        content="actual result", tool_call_id="repeated", status="error" if state == "tool_error" else "success"
    )

    def handler(req):
        nonlocal count
        count += 1
        if state == "exception":
            raise ValueError("private error body")
        if state == "cancelled":
            raise asyncio.CancelledError()
        return result

    async def ahandler(req):
        return handler(req)

    with sdk.capture_tool_receipts() as capture:
        for _ in range(2):
            try:
                output = (
                    await middleware.awrap_tool_call(request(), ahandler)
                    if asynchronous
                    else middleware.wrap_tool_call(request(), handler)
                )
                assert output is result
            except (ValueError, asyncio.CancelledError):
                assert state in ("exception", "cancelled")
    envelope = capture.snapshot()
    assert count == 2
    assert envelope["schema_version"] == 1
    assert envelope["source"] == "native_audit"
    assert envelope["capture_status"] == "captured"
    entries = envelope["entries"]
    assert [e["result_state"] for e in entries] == [state, state]
    assert [e["sequence"] for e in entries] == [1, 2]
    assert len({e["execution_id"] for e in entries}) == 2
    assert all(e["tool_call_id"] == "repeated" and e["duration_ms"] >= 0 for e in entries)
    assert all(e["invocation_id"] == envelope["invocation_id"] for e in entries)
    assert "private error body" not in json.dumps(envelope)


@pytest.mark.parametrize("status", ["success", "error"])
@pytest.mark.parametrize("content", ["Error handling guide", "actual result"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_tool_message_native_status_takes_precedence_over_content(status, content, asynchronous):
    middleware = NativeToolReceiptMiddleware()
    result = ToolMessage(content=content, tool_call_id="repeated", status=status)

    async def handler(_):
        return result

    with sdk.capture_tool_receipts() as capture:
        output = (
            await middleware.awrap_tool_call(request(), handler)
            if asynchronous
            else middleware.wrap_tool_call(request(), lambda _: result)
        )
    assert output is result
    envelope = capture.snapshot()
    assert envelope["capture_status"] == "captured"
    assert len(envelope["entries"]) == 1
    entry = envelope["entries"][0]
    assert entry["result_state"] == ("tool_error" if status == "error" else "success")
    assert entry["result_metadata"]["status"] == status
    assert content not in json.dumps(envelope)


@pytest.mark.parametrize("calls", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_agent_support_without_tools_or_model_claims(calls, asynchronous):
    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    @tool
    def native_read(path: str) -> str:
        """Read a fixture."""
        return "full file body must never be exposed"

    messages = [AIMessage(content="I called native_read many times")]
    if calls:
        messages.insert(
            0,
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "native_read", "args": {"path": "fixture"}, "id": "native-1"},
                ],
            ),
        )
    graph = create_agent(Model(messages=iter(messages)), tools=[native_read], middleware=[AuditMiddleware(), NativeToolReceiptMiddleware()])
    with sdk.capture_tool_receipts() as capture:
        inputs = {"messages": [{"role": "user", "content": "go"}]}
        if asynchronous:
            await graph.ainvoke(inputs)
        else:
            await asyncio.to_thread(graph.invoke, inputs)
    envelope = capture.snapshot()
    assert envelope["capture_status"] == "captured"
    assert len(envelope["entries"]) == int(calls)
    assert "full file body" not in json.dumps(envelope)


async def test_scopes_nested_concurrent_and_context_threads():
    middleware = NativeToolReceiptMiddleware()

    async def run(name):
        with sdk.capture_tool_receipts() as capture:
            await asyncio.sleep(0)
            await asyncio.to_thread(middleware.wrap_tool_call, request({"owner": name}), lambda _: "ok")
            with sdk.capture_tool_receipts() as nested:
                middleware.wrap_tool_call(request({"owner": "nested"}), lambda _: "ok")
            middleware.wrap_tool_call(request({"owner": name}), lambda _: "ok")
        assert len(nested.snapshot()["entries"]) == 1
        return capture.snapshot()

    left, right = await asyncio.gather(run("left"), run("right"))
    assert left["invocation_id"] != right["invocation_id"]
    for expected, envelope in (("left", left), ("right", right)):
        assert [entry["args"]["owner"] for entry in envelope["entries"]] == [expected] * 2


async def test_close_rejects_late_results_and_resets_after_cancel():
    middleware = NativeToolReceiptMiddleware()
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(_):
        entered.set()
        await release.wait()
        return "late result"

    with sdk.capture_tool_receipts() as outer:
        with sdk.capture_tool_receipts() as inner:
            task = asyncio.create_task(middleware.awrap_tool_call(request(), handler))
            await entered.wait()
        before = inner.snapshot()
        release.set()
        await task
        assert inner.snapshot() == before
        assert before["capture_status"] == "partial"
        assert before["dropped_count"] == 1
        with pytest.raises(asyncio.CancelledError):
            with sdk.capture_tool_receipts() as cancelled:
                middleware.before_agent({}, None)
                raise asyncio.CancelledError()
        middleware.wrap_tool_call(request(), lambda _: "parent")
    assert len(outer.snapshot()["entries"]) == 1
    assert cancelled.snapshot()["capture_status"] == "partial"
    with sdk.capture_tool_receipts() as unsupported:
        pass
    assert unsupported.snapshot()["capture_status"] == "unsupported"


def test_redaction_bounds_and_unknown_objects():
    class Unknown:
        def __str__(self):
            raise AssertionError("must never stringify unknown objects")

    secret = "sk-" + "x" * 80
    deep = {"api_key": "buried"}
    for _ in range(20):
        deep = {"nested": deep}
    args = {
        "nested": {"api-key": "private"},
        "summary": "a" * 499 + " " + secret,
        "deep": deep,
        "unknown": Unknown(),
        "body": "file body",
        "password": "private",
        "file-contents": "private file",
        "fields": list(range(80)),
        "unicode": "é" * 600,
    }
    result = ToolMessage(
        content=[
            {"type": "text", "text": "private file"},
            {"type": "image_url", "image_url": {"url": "private image"}},
        ],
        artifact={"path": "some/file", "api_key": "private", "body": "private file"},
        tool_call_id="repeated",
    )
    with sdk.capture_tool_receipts() as capture:
        NativeToolReceiptMiddleware().wrap_tool_call(request(args), lambda _: result)
    envelope = capture.snapshot()
    serialized = json.dumps(envelope)
    for forbidden in (secret, "sk-xxx", "private", "buried", "file body", "Unknown"):
        assert forbidden not in serialized
    entry = envelope["entries"][0]
    assert entry["args"]["nested"]["api-key"] == "[REDACTED]"
    assert len(entry["args"]["unicode"].encode()) <= envelope["limits"]["field_bytes"]
    assert entry["result_metadata"]["artifact"]["path"] == "some/file"
    assert envelope["capture_status"] == "partial"
    assert envelope["partial_count"] == 1
    assert entry["dropped_fields"] > 0
    envelope["entries"].clear()
    assert len(capture.snapshot()["entries"]) == 1


def test_entry_and_aggregate_limits():
    middleware = NativeToolReceiptMiddleware()
    with sdk.capture_tool_receipts() as capture:
        for _ in range(150):
            middleware.wrap_tool_call(request({str(i): "x" * 500 for i in range(8)}), lambda _: "ok")
    envelope = capture.snapshot()
    assert envelope["capture_status"] == "partial"
    assert len(envelope["entries"]) <= envelope["limits"]["entries"]
    assert (
        sum(len(json.dumps(e, ensure_ascii=True).encode()) for e in envelope["entries"])
        <= envelope["limits"]["aggregate_bytes"]
    )
    assert len(envelope["entries"]) + envelope["dropped_count"] == 150
    with sdk.capture_tool_receipts() as oversized:
        middleware.wrap_tool_call(request({str(i): "x" * 512 for i in range(32)}), lambda _: "ok")
    assert oversized.snapshot()["dropped_count"] == 1
    assert oversized.snapshot()["entries"] == []


async def test_sdk_subagent_preserves_text_and_reports_no_calls(monkeypatch):
    import graph.agent as agent
    from graph.config import LangGraphConfig
    from graph.subagents.config import SUBAGENT_REGISTRY, SubagentConfig

    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    def checked_create_agent(*args, **kwargs):
        assert isinstance(kwargs["middleware"][-1], NativeToolReceiptMiddleware)
        return create_agent(*args, **kwargs)

    monkeypatch.setattr(agent, "create_agent", checked_create_agent)
    monkeypatch.setattr(sdk.STATE, "graph_config", LangGraphConfig(filesystem_enabled=False))
    monkeypatch.setattr(
        agent,
        "create_llm",
        lambda *a, **kw: Model(
            messages=iter([AIMessage(content="Unchanged text.")]),
            disable_streaming=True,
        ),
    )
    monkeypatch.setattr(agent, "get_all_tools", lambda *a, **kw: [])
    monkeypatch.setattr(agent, "build_subagent_prompt", lambda *a, **kw: "Research.")
    monkeypatch.setitem(
        SUBAGENT_REGISTRY,
        "receipt-test",
        SubagentConfig(
            name="receipt-test",
            description="research",
            system_prompt="Research.",
            tools=[],
        ),
    )
    with sdk.capture_tool_receipts() as capture:
        output = await sdk.run_subagent("receipt-test", "go", description="test", extra_tools=[])
    assert output == "[receipt-test completed: test]\n\nUnchanged text."
    assert capture.snapshot()["capture_status"] == "captured"
    assert capture.snapshot()["entries"] == []


def test_capture_failure_does_not_replace_handler_outcome(monkeypatch):
    from graph import tool_receipts

    def broken_redactor(_):
        raise RuntimeError("private failure")

    monkeypatch.setattr(tool_receipts, "redact", broken_redactor)
    result = ToolMessage(content="ok", tool_call_id="repeated")
    with sdk.capture_tool_receipts() as capture:
        assert NativeToolReceiptMiddleware().wrap_tool_call(request(), lambda _: result) is result
    assert capture.snapshot()["capture_status"] == "partial"
    assert capture.snapshot()["dropped_count"] == 1


async def test_shared_scope_thread_mutation_and_start_sequence():
    middleware = NativeToolReceiptMiddleware()
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow(_):
        started.set()
        await finish.wait()
        return "ok"

    with sdk.capture_tool_receipts() as capture:
        first = asyncio.create_task(middleware.awrap_tool_call(request(), slow))
        await started.wait()
        await asyncio.gather(
            *[asyncio.to_thread(middleware.wrap_tool_call, request(), lambda _: "ok") for _ in range(32)]
        )
        finish.set()
        await first
    entries = capture.snapshot()["entries"]
    assert len(entries) == 33
    assert [entry["sequence"] for entry in entries] == list(range(1, 34))
    assert len({entry["execution_id"] for entry in entries}) == 33
    assert capture.snapshot()["pending_count"] == 0


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"isError": True, "body": "private output", "count": 2}, "tool_error"),
        ({"status": "error", "path": "fixture"}, "tool_error"),
        ("Error: private output", "tool_error"),
        (object(), "unknown"),
    ],
)
def test_structured_and_unknown_results(result, expected):
    with sdk.capture_tool_receipts() as capture:
        assert NativeToolReceiptMiddleware().wrap_tool_call(request(), lambda _: result) is result
    envelope = capture.snapshot()
    assert envelope["entries"][0]["result_state"] == expected
    assert "private output" not in json.dumps(envelope)


async def test_task_cancel_records_execution_before_scope_reset():
    entered = asyncio.Event()
    captures = []

    async def handler(_):
        entered.set()
        await asyncio.Event().wait()

    async def run():
        with sdk.capture_tool_receipts() as capture:
            captures.append(capture)
            await NativeToolReceiptMiddleware().awrap_tool_call(request(), handler)

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    envelope = captures[0].snapshot()
    assert envelope["closed"]
    assert envelope["capture_status"] == "partial"
    assert len(envelope["entries"]) == 1
    assert envelope["entries"][0]["result_state"] == "cancelled"


def test_unknown_result_fields_cannot_expose_file_bodies():
    result = ToolMessage(
        content="private file",
        tool_call_id="repeated",
        artifact={
            "files": [
                {
                    "path": "fixture.txt",
                    "lines": ["private file"],
                    "novel_payload_key": "private file",
                    "bytes": "private file",
                }
            ],
            "line_count": 1,
        },
    )
    with sdk.capture_tool_receipts() as capture:
        NativeToolReceiptMiddleware().wrap_tool_call(request(), lambda _: result)
    envelope = capture.snapshot()
    artifact = envelope["entries"][0]["result_metadata"]["artifact"]
    assert artifact["files"][0]["path"] == "fixture.txt"
    assert artifact["line_count"] == 1
    assert "private file" not in json.dumps(envelope)
    assert envelope["capture_status"] == "partial"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("behavior", ["deny", "retry", "rewrite"])
async def test_downstream_policy_and_retry_capture_only_handler_executions(behavior, asynchronous):
    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    executions = []

    @tool(response_format="content_and_artifact")
    def native_read(path: str) -> tuple:
        """Read a fixture."""
        executions.append(path)
        return "private body", {"path": path}

    class PluginPolicy(AgentMiddleware):
        def replacement(self, request):
            return ToolMessage(content="policy response", tool_call_id=request.tool_call["id"], status="error")

        def wrap_tool_call(self, request, handler):
            if behavior == "deny":
                return self.replacement(request)
            result = handler(request)
            if behavior == "retry":
                result = handler(request.override(tool_call={**request.tool_call, "args": {"path": "retry"}}))
            return self.replacement(request) if behavior == "rewrite" else result

        async def awrap_tool_call(self, request, handler):
            if behavior == "deny":
                return self.replacement(request)
            result = await handler(request)
            if behavior == "retry":
                result = await handler(request.override(tool_call={**request.tool_call, "args": {"path": "retry"}}))
            return self.replacement(request) if behavior == "rewrite" else result

    # Use the production lead assembly: plugin policy is downstream of Audit.
    from graph.agent import _build_middleware
    from graph.config import LangGraphConfig

    middleware = _build_middleware(
        LangGraphConfig(
            memory_middleware=False, knowledge_middleware=False, compaction_enabled=False,
            tools_deferred_enabled=False, enforcement_enabled=False, skills_enabled=False,
        ),
        extra_middleware=[PluginPolicy()],
    )
    assert isinstance(middleware[-1], NativeToolReceiptMiddleware)
    if not asynchronous:
        # The production model chain has async-only hooks. Exercise the sync
        # tool chain with the same assembled audit/policy/receipt ordering.
        middleware = [m for m in middleware if isinstance(
            m, (AuditMiddleware, PluginPolicy, NativeToolReceiptMiddleware)
        )]
    graph = create_agent(
        Model(messages=iter([
            AIMessage(content="", tool_calls=[{"name": "native_read", "args": {"path": "original"}, "id": "same"}]),
            AIMessage(content="done"),
        ])),
        tools=[native_read],
        middleware=middleware,
    )
    with sdk.capture_tool_receipts() as capture:
        inputs = {"messages": [{"role": "user", "content": "go"}]}
        if asynchronous:
            await graph.ainvoke(inputs)
        else:
            await asyncio.to_thread(graph.invoke, inputs)
    envelope = capture.snapshot()
    expected = [] if behavior == "deny" else ["original", "retry"] if behavior == "retry" else ["original"]
    assert executions == expected
    assert envelope["capture_status"] == "captured"
    assert [e["args"]["path"] for e in envelope["entries"]] == expected
    assert [e["result_metadata"]["artifact"]["path"] for e in envelope["entries"]] == expected
    assert all(e["result_state"] == "success" for e in envelope["entries"])
    assert all(e["tool_call_id"] == "same" for e in envelope["entries"])
