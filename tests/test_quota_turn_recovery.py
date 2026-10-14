"""Real turn drivers recover on the same thread after a wire-level quota 429."""
import asyncio
import importlib

import anthropic._base_client as sdk_base
import pytest
from langchain_core.messages import AIMessage

from graph.providers.anthropic_oauth import _OAuthChatAnthropic
from runtime import local_turns, turn_activity
from tests._turn_driver_fakes import model_end, model_start, text, turn_result
from tests.test_turn_driver_characterization_sync import env  # noqa: F401 — shared driver fixture


@pytest.mark.parametrize("cancel_backoff", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_quota_failure_then_model_switch_keeps_thread_and_releases_turn(env, monkeypatch, stream, cancel_backoff):  # noqa: F811
    chat = importlib.import_module("server.chat")
    graph = env.install()
    llm = _OAuthChatAnthropic(model="claude-test", oauth_token="test", max_retries=2)
    monkeypatch.setattr(_OAuthChatAnthropic, "_refresh_oauth_token", lambda self: None)
    http = sdk_base.httpx2
    calls = []
    threads = []
    backoff = asyncio.Event()

    def respond(request):
        calls.append(request)
        return http.Response(429, headers={"retry-after": "10" if cancel_backoff else "130287"}, json={
            "error": {"type": "rate_limit_error", "message": "Weekly quota exhausted"},
        })

    async def invoke(state, config):
        threads.append(config["configurable"]["thread_id"])
        if state["model"] != "working":
            await llm.ainvoke("hello")
        return turn_result(AIMessage(content="Recovered"))

    async def events(state, config=None, version=None):
        threads.append(config["configurable"]["thread_id"])
        if state["model"] != "working":
            async for _ in llm.astream("hello"):
                pass
        yield model_start("working")
        yield text("working", "<output>Recovered</output>")
        yield model_end("working")

    monkeypatch.setattr(graph, "ainvoke", invoke)
    monkeypatch.setattr(graph, "astream_events", events)
    async with http.AsyncClient(transport=http.MockTransport(respond)) as client:
        llm._async_client._client = client
        sleep_for_retry = llm._async_client._sleep_for_retry

        async def observed_backoff(**kwargs):
            backoff.set()
            await sleep_for_retry(**kwargs)

        monkeypatch.setattr(llm._async_client, "_sleep_for_retry", observed_backoff)

        async def exhausted_turn():
            if stream:
                return [frame async for frame in chat._chat_langgraph_stream("hello", "recovery", request_metadata={"model": "exhausted"})]
            return await chat.chat("hello", "recovery", model="exhausted", origin="api-chat")

        async with asyncio.timeout(10):
            if cancel_backoff:
                pending = asyncio.create_task(exhausted_turn())
                await backoff.wait()
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                failed = await exhausted_turn()
                if stream:
                    assert any(kind == "error" and "130287" in payload for kind, payload in failed)
                else:
                    assert failed[0]["error"]["quota_exhausted"] is True
                    assert failed[0]["error"]["retry_after"] == 130287
            assert len(calls) == 1
            assert not turn_activity.is_active("recovery")
            assert not local_turns.snapshot("recovery")
            if stream:
                recovered = [frame async for frame in chat._chat_langgraph_stream("continue", "recovery", request_metadata={"model": "working"})]
                assert any(kind == "done" and "Recovered" in payload for kind, payload in recovered)
            else:
                recovered = await chat.chat("continue", "recovery", model="working", origin="api-chat")
                assert recovered[0]["content"] == "Recovered"
    assert threads == ["a2a:recovery", "a2a:recovery"]
    assert not turn_activity.is_active("recovery")
    assert len(calls) == 1  # changing models never calls the exhausted provider again
