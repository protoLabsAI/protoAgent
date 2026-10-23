"""Session affinity on the actual OpenAI request, with a shared model client."""

import asyncio
import json
import re

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
    assert re.fullmatch(r"[0-9a-f]{32}", seen["a1"][key])  # LiteLLM's id shape, no raw thread id
    assert key not in seen["standalone"]
    assert seen["explicit"][key] == "caller-key"
    assert seen["a1"]["x-test"] == "preserved"
    assert seen["a1"]["user-agent"] == _GATEWAY_UA


# --- #4463: where the id comes from, and who never sends it -----------------------------

_LITELLM_SESSION_ID = re.compile(r"^[a-zA-Z0-9_\-]{8,}$")  # LiteLLM 1.83 `_SESSION_ID_VALUE_RE`


def _payload_headers(model, config=None):
    """The extra headers ``model`` would put on the wire for one request."""
    from graph.llm import SESSION_AFFINITY_HEADER

    def build(_):
        return model._get_request_payload("hi").get("extra_headers") or {}

    headers = RunnableLambda(build).invoke(None, config=config) if config else build(None)
    return headers.get(SESSION_AFFINITY_HEADER)


def _gateway_model():
    return _ReasoningChatOpenAI(model="protolabs/smart", api_key="test", base_url="https://gateway.test/v1")


def _thread(tid):
    return {"configurable": {"thread_id": tid}}


def test_same_thread_same_id_across_turns_and_threads_differ():
    model = _gateway_model()
    turn1 = _payload_headers(model, _thread("a2a:session-one"))
    turn2 = _payload_headers(model, _thread("a2a:session-one"))
    other = _payload_headers(model, _thread("a2a:session-two"))
    assert turn1 and turn1 == turn2
    assert other and other != turn1
    assert _LITELLM_SESSION_ID.match(turn1) and len(turn1) == 32
    assert "session-one" not in turn1  # hashed, never the raw thread id


def test_no_session_no_header():
    assert _payload_headers(_gateway_model()) is None
    assert _payload_headers(_gateway_model(), {"configurable": {}}) is None


async def test_out_of_graph_call_falls_back_to_the_turns_session_id():
    """An aux call made inside a turn's session scope but outside any graph run."""
    from observability import tracing

    model = _gateway_model()
    async with tracing.trace_session("sess-abc"):
        first = _payload_headers(model)
        second = _payload_headers(model)
    async with tracing.trace_session("sess-xyz"):
        other = _payload_headers(model)
    assert first and first == second and _LITELLM_SESSION_ID.match(first)
    assert other and other != first
    assert _payload_headers(model) is None  # scope closed: no session, no header


async def test_graph_thread_id_wins_over_the_session_scope():
    """Inside a graph run the conversation's thread id is the key, so out-of-scope calls
    on the same thread (subagents, resumed turns) agree with the lead's."""
    from observability import tracing

    model = _gateway_model()
    bare = _payload_headers(model, _thread("a2a:t1"))
    async with tracing.trace_session("sess-abc"):
        scoped = _payload_headers(model, _thread("a2a:t1"))
    assert bare == scoped


def test_codex_backend_never_gets_the_gateway_header():
    from graph.providers.codex_client import build_codex_client

    codex = build_codex_client(
        issuer="https://chatgpt.com/backend-api/codex",
        model="gpt-5-codex",
        api_key="x",
        use_responses_api=True,
        output_version="responses/v1",
        store=False,
    )
    assert _payload_headers(codex, _thread("a2a:t1")) is None
