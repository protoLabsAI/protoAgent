"""Session affinity on the actual OpenAI request, with a shared model client."""

import asyncio
import json

import httpx
import pytest
from langchain_core.runnables import RunnableLambda

from graph.llm import _GATEWAY_UA, _ReasoningChatOpenAI


@pytest.mark.parametrize("streaming", [False, True])
async def test_thread_affinity_on_wire_is_stable_and_isolated(streaming):
    seen = {}
    retry_headers = []

    async def handler(request):
        body = json.loads(request.content)
        text = body["messages"][0]["content"]
        seen[text] = dict(request.headers)
        assert "extra_headers" not in body
        await asyncio.sleep(0)
        if text == "a1":
            retry_headers.append(dict(request.headers))
            if len(retry_headers) == 1:
                return httpx.Response(503, json={"error": {"message": "temporary failure"}})
        if body.get("stream"):
            chunk = {
                "id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 0,
                "model": "protolabs/smart",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
            }
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": "protolabs/smart",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = _ReasoningChatOpenAI(
            model="protolabs/smart", api_key="test", base_url="https://gateway.test/v1",
            http_async_client=client, default_headers={"User-Agent": _GATEWAY_UA},
            streaming=streaming,
            max_retries=1,
        )

        async def call(text):
            headers = {"x-test": "preserved"}
            if text == "explicit":
                headers["X-LiteLLM-Session-Id"] = "caller-key"
            return await model.ainvoke(text, extra_headers=headers)

        run = RunnableLambda(call)
        await asyncio.gather(
            run.ainvoke("a1", config={"configurable": {"thread_id": "custom/会話"}}),
            run.ainvoke("b1", config={"configurable": {"thread_id": "other"}}),
        )
        await run.ainvoke("a2", config={"configurable": {"thread_id": "custom/会話"}})
        await model.ainvoke("standalone")
        await run.ainvoke("explicit", config={"configurable": {"thread_id": "other"}})

    key = "x-litellm-session-id"
    assert seen["a1"][key] == seen["a2"][key]
    assert len(retry_headers) == 2
    assert retry_headers[0][key] == retry_headers[1][key] == seen["a2"][key]
    assert seen["a1"][key] != seen["b1"][key]
    assert seen["a1"][key].startswith("protoagent:")
    assert seen["a1"][key].isascii()
    assert key not in seen["standalone"]
    assert seen["explicit"][key] == "caller-key"
    assert seen["a1"]["x-test"] == "preserved"
    assert seen["a1"]["user-agent"] == _GATEWAY_UA
