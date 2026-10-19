"""Per-dispatch ACP session policy; independent jobs versus explicit continuations."""

import asyncio
import json
import sys

import pytest

from plugins.coding_agent.acp_client import AcpClient
from plugins.delegates import _build_delegate_to
from plugins.delegates.registry import DelegateRegistry

_AGENT = r"""
import json, sys
counter = 0
for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params", {})
    with open(sys.argv[1], "a") as log:
        log.write(json.dumps({"method": method, "params": params}) + "\n")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
    elif method == "session/new":
        counter += 1
        result = {"sessionId": f"new-{counter}"}
    elif method == "session/load":
        result = {}
    elif method == "session/prompt":
        sid = params["sessionId"]
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": sid}}}}), flush=True)
        result = {"stopReason": "end_turn"}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}), flush=True)
"""


@pytest.fixture
def coder(tmp_path, monkeypatch):
    import plugins.coding_agent as pool
    from runtime.state import STATE

    script = tmp_path / "agent.py"
    script.write_text(_AGENT)
    wire = tmp_path / "wire.jsonl"
    persisted = tmp_path / "session.json"
    client = AcpClient(
        sys.executable,
        [str(script), str(wire)],
        cwd=str(tmp_path),
        name="coder",
        session_id_path=persisted,
        record_runs=False,
    )
    client._persist_session_id("previous-job")
    monkeypatch.setattr(pool, "_client_for", lambda spec: client)
    monkeypatch.setattr(STATE, "background_mgr", None)
    registry = DelegateRegistry(
        [
            {
                "name": "coder",
                "type": "acp",
                "command": sys.executable,
                "workdir": str(tmp_path),
            }
        ]
    )
    return client, registry, wire, persisted


async def _call(registry, **kwargs):
    return await _build_delegate_to(registry).coroutine(
        target="coder",
        query="do this job",
        **kwargs,
    )


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("detached", [False, True])
async def test_independent_background_jobs_are_fresh_even_in_pool(coder, monkeypatch, detached):
    client, registry, wire, persisted = coder
    def forbidden_read():
        raise AssertionError("fresh jobs must not read the persisted session")

    monkeypatch.setattr(client, "_read_persisted_session_id", forbidden_read)
    jobs = []
    if detached:
        from runtime.state import STATE

        class Manager:
            async def spawn_work(self, **kwargs):
                jobs.append(asyncio.create_task(kwargs["work"]()))
                return str(len(jobs))

        monkeypatch.setattr(STATE, "background_mgr", Manager())
    try:
        first = await _call(registry, background=True)
        second = await _call(registry, background=True)
        if detached:
            first, second = await asyncio.gather(*jobs)
        assert (first, second) == ("new-1", "new-2")
        # A deliberate continuation uses the last job's session, without replaying it.
        continuation = await _call(registry, background=True, fresh_session=False)
        if detached:
            continuation = await jobs[-1]
        assert continuation == "new-2"
        methods = [json.loads(line)["method"] for line in wire.read_text().splitlines()]
        assert methods.count("session/new") == 2
        assert "session/load" not in methods
        assert json.loads(persisted.read_text())["sessionId"] == "new-2"
    finally:
        await client.close()


@pytest.mark.platform_sensitive
async def test_foreground_and_board_continuations_reuse_persisted_session(coder):
    client, registry, wire, _ = coder
    try:
        assert await _call(registry) == "previous-job"
        assert await registry.dispatch("coder", "fix review findings", conversation_key="board-item") == "previous-job"
        methods = [json.loads(line)["method"] for line in wire.read_text().splitlines()]
        assert methods.count("session/load") == 1
        assert "session/new" not in methods
    finally:
        await client.close()


async def test_explicit_resume_is_forwarded_without_fresh_policy(monkeypatch):
    from plugins.delegates.registry import ADAPTERS
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "background_mgr", None)
    seen = []

    async def dispatch(d, query, *, timeout=None, item_id=None, resume_task_id=None):
        seen.append((query, resume_task_id))
        return "same task"

    monkeypatch.setattr(ADAPTERS["a2a"], "dispatch", dispatch)
    registry = DelegateRegistry([{"name": "coder", "type": "a2a", "url": "http://peer.example/a2a"}])
    assert await _call(registry, background=True, resume_task_id="original-task", fresh_session=True) == "same task"
    assert seen == [("do this job", "original-task")]


@pytest.mark.parametrize("state", ["TASK_STATE_INPUT_REQUIRED", "TASK_STATE_WORKING"])
async def test_background_resume_continues_or_collects_original_peer_task(monkeypatch, state):
    import httpx
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "background_mgr", None)
    monkeypatch.setattr("security.policy.check_url", lambda *a, **kw: None)
    calls = []

    def respond(request):
        if request.method != "POST":
            return httpx.Response(404)
        body = json.loads(request.content)
        calls.append(body)
        task = {"id": "original", "contextId": "original-context"}
        if len(calls) == 1:
            task["status"] = {"state": state}
        else:
            task["status"] = {"state": "TASK_STATE_COMPLETED"}
            task["artifacts"] = [{"parts": [{"text": "finished original"}]}]
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"task": task}})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw),
    )
    registry = DelegateRegistry([{"name": "coder", "type": "a2a", "url": "http://peer.example/a2a"}])
    assert await _call(registry, background=True, resume_task_id="original") == "finished original"
    assert calls[0]["method"] == "GetTask"
    if state == "TASK_STATE_INPUT_REQUIRED":
        assert [c["method"] for c in calls] == ["GetTask", "SendMessage"]
        message = calls[1]["params"]["message"]
        assert message["taskId"] == "original"
        assert message["contextId"] == "original-context"
    else:
        assert all(c["method"] == "GetTask" for c in calls)
        assert all(c["params"]["id"] == "original" for c in calls)
