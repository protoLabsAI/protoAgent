"""Redacted header goldens and wire-level failure isolation for passive capture."""

import importlib
import random
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from graph.providers.usage import headers as h
from observability import usage_store

CLAUDE = {
    "anthropic-ratelimit-unified-5h-utilization": "0.25",
    "anthropic-ratelimit-unified-5h-reset": "1792756800",
    "anthropic-ratelimit-unified-5h-status": "allowed",
    "anthropic-ratelimit-unified-7d-utilization": "0.81",
    "anthropic-ratelimit-unified-7d-reset": "1792756800",
    "anthropic-ratelimit-unified-7d-status": "rejected",
    "anthropic-ratelimit-unified-representative-claim": "7d",
}
CODEX = {f"x-codex-{slot}-{key}": value for slot, minutes, percent in
         (("primary", "300", "25"), ("secondary", "10080", "81"))
         for key, value in (("used-percent", percent), ("window-minutes", minutes), ("reset-at", "1792756800"))}


@pytest.fixture
def store(monkeypatch):
    result = usage_store.UsageStore({})
    monkeypatch.setattr(usage_store, "default_store", lambda: result)
    return result


@pytest.mark.parametrize("provider,parser,golden", [
    ("anthropic-oauth", h.parse_anthropic_headers, CLAUDE),
    ("openai-codex", h.parse_codex_headers, CODEX),
])
def test_goldens_and_merge(provider, parser, golden, store):
    windows = parser({k.upper(): v for k, v in golden.items()})
    assert [w.kind for w in windows] == ["session", "weekly"]
    assert [w.window_seconds for w in windows] == [18000, 604800]
    assert [w.used_pct for w in windows] == [25, 81]
    assert all(w.resets_at.isoformat() == "2026-10-23T12:00:00+00:00" for w in windows)
    if provider == "anthropic-oauth":
        assert windows[1].binding
        assert windows[1].status == "blocked"
    h.capture(provider, golden)
    cached = store.cached(provider)
    assert cached["source"] == "headers"
    assert cached["windows"] == [w.to_dict() for w in windows]
    # Same observations replace the existing window IDs, never duplicate them.
    h.capture(provider, golden)
    assert len(store.cached(provider)["windows"]) == 2


def test_weekly_primary_and_partial():
    golden = {k: v for k, v in CODEX.items() if "primary" in k}
    golden["x-codex-primary-window-minutes"] = "10080"
    assert h.parse_codex_headers(golden)[0].kind == "weekly"
    assert h.parse_anthropic_headers({**CLAUDE, "anthropic-ratelimit-unified-5h-reset": "bad"})[0].kind == "weekly"


def test_capture_fuzz_never_raises(store, monkeypatch):
    rng = random.Random(4432)
    garbage = [None, [], 17, object(), {}, "bad", "x" * 100000, "☃", "nan", "inf", "1e999", "-inf"]
    for _ in range(200):
        garbage.append("".join(chr(rng.randrange(0x110000)) for _ in range(rng.randrange(50))))
    for value in garbage:
        for provider, parser, golden in (("anthropic-oauth", h.parse_anthropic_headers, CLAUDE),
                                         ("openai-codex", h.parse_codex_headers, CODEX)):
            assert parser(value) == []
            h.capture(provider, value)
            for key in golden:
                h.capture(provider, {**golden, key: value})
        h.anthropic_response_hook(SimpleNamespace(headers=value))
    monkeypatch.setattr(store, "merge_windows", Mock(side_effect=RuntimeError("private detail")))
    h.capture("anthropic-oauth", CLAUDE)
    h.capture("openai-codex", CODEX)


def _http():
    base = importlib.import_module("anthropic._base_client")
    return getattr(base, "httpx2", None) or importlib.import_module("httpx")


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_hook_stream_no_read_or_extra_request(monkeypatch, store, failure, asynchronous):
    http = _http()
    calls = []
    reads = []

    class Body(http.SyncByteStream, http.AsyncByteStream):
        def __iter__(self):
            reads.append(True)
            yield b"unchanged"

        async def __aiter__(self):
            for chunk in self:
                yield chunk

    def respond(request):
        calls.append(request)
        return http.Response(200, headers=CLAUDE, stream=Body())

    if failure:
        monkeypatch.setattr(h, "capture", Mock(side_effect=RuntimeError("hook failure")))
    cls = http.AsyncClient if asynchronous else http.Client
    original = cls(transport=http.MockTransport(respond))
    client = h.with_anthropic_headers(original, asynchronous=asynchronous)
    assert original.event_hooks["response"] == []
    assert len(client.event_hooks["response"]) == 1
    if asynchronous:
        async with client.stream("GET", "https://example.test") as response:
            assert reads == []
            assert await response.aread() == b"unchanged"
        await client.aclose()
        assert not original.is_closed
        await original.aclose()
    else:
        with client.stream("GET", "https://example.test") as response:
            assert reads == []
            assert response.read() == b"unchanged"
        client.close()
        assert not original.is_closed
        original.close()
    assert len(calls) == 1
    assert reads == [True]
    assert bool(store.cached("anthropic-oauth")) is not failure


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status", [200, 429])
@pytest.mark.parametrize("capture_failure", [False, True])
@pytest.mark.asyncio
async def test_sdk_model_call(monkeypatch, store, asynchronous, status, capture_failure):
    import anthropic
    from graph.providers import anthropic_oauth as ao

    http = _http()
    calls = []
    body = {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-test",
            "content": [{"type": "text", "text": "unchanged"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}}

    def respond(request):
        calls.append(request)
        return http.Response(status, headers=CLAUDE, json=body if status == 200 else
                             {"type": "error", "error": {"type": "rate_limit_error", "message": "limited"}})

    monkeypatch.setattr(ao, "current_oauth_token", lambda **kw: "fake-token")
    monkeypatch.setattr(ao, "_get_default_httpx_client", lambda **kw: http.Client(transport=http.MockTransport(respond)))
    monkeypatch.setattr(ao, "_get_default_async_httpx_client", lambda **kw: http.AsyncClient(transport=http.MockTransport(respond)))
    if capture_failure:
        monkeypatch.setattr(h, "capture", Mock(side_effect=RuntimeError("capture failed")))
    model = ao._OAuthChatAnthropic(model="claude-test", oauth_token="fake-token", api_key="unused", max_retries=0)
    if status == 429:
        with pytest.raises(anthropic.RateLimitError) as exc:
            if asynchronous:
                await model.ainvoke("hello")
            else:
                model.invoke("hello")
        assert exc.value.response.status_code == 429
    else:
        result = await model.ainvoke("hello") if asynchronous else model.invoke("hello")
        assert result.content == "unchanged"
    assert len(calls) == 1
    if not capture_failure:
        assert store.cached("anthropic-oauth")["source"] == "headers"
    model._client.close()
    await model._async_client.close()


def test_codex_end_capture_and_failure(monkeypatch, store):
    from langchain_core.messages import AIMessage
    from server.turn_stream import _on_chat_model_end

    output = AIMessage(content="unchanged", response_metadata={"headers": CODEX})
    event = {"data": {"output": output}}
    state = SimpleNamespace(llm_started={})
    assert list(_on_chat_model_end(state, event, "model", "parent")) == []
    assert store.cached("openai-codex")["source"] == "headers"
    monkeypatch.setattr(h, "capture", Mock(side_effect=RuntimeError("failed")))
    assert list(_on_chat_model_end(state, event, "model", "parent")) == []
    assert output.content == "unchanged"


def test_codex_builder_enables_headers(monkeypatch):
    from graph.config import LangGraphConfig
    from graph.providers import codex_client, openai_codex

    monkeypatch.setattr(openai_codex, "resolve_codex_oauth", lambda: SimpleNamespace(
        base_url="https://example.test", access_token="fake-token", account_id="fake-account"))
    build = Mock()
    monkeypatch.setattr(codex_client, "build_codex_client", build)
    openai_codex.build_codex_llm(LangGraphConfig(), model_name="gpt-5-codex")
    assert build.call_args.kwargs["include_response_headers"] is True


@pytest.mark.parametrize("asynchronous", [False, True])
def test_sdk_429_error_path_without_transport_hook(store, asynchronous):
    import anthropic
    from graph.providers import anthropic_oauth as ao

    http = _http()
    cls = ao._AsyncInteractiveAnthropic if asynchronous else ao._InteractiveAnthropic
    client = cls(api_key="fake-token")
    response = http.Response(429, headers=CLAUDE, request=http.Request("POST", "https://example.test"))
    error = client._make_status_error("limited", body={}, response=response)
    assert isinstance(error, anthropic.RateLimitError)
    assert error.response is response
    assert store.cached("anthropic-oauth")["source"] == "headers"


def test_anthropic_warning_and_representative_alias():
    windows = h.parse_anthropic_headers({**CLAUDE,
        "anthropic-ratelimit-unified-5h-status": "allowed_warning",
        "anthropic-ratelimit-unified-representative-claim": "five_hour"})
    assert windows[0].status == "warning"
    assert windows[0].binding


def test_cached_upstream_pool_is_borrowed():
    import gc
    from graph.providers import anthropic_oauth as ao

    owner = ao._get_default_httpx_client(base_url="https://usage-test.invalid")
    before = {key: list(value) for key, value in owner.event_hooks.items()}
    facade = h.with_anthropic_headers(owner)
    facade.close()
    del facade
    gc.collect()
    assert not owner.is_closed
    assert owner.event_hooks == before


def test_garbage_headers_leave_wire_result_unchanged(monkeypatch):
    http = _http()
    rng = random.Random(4432)
    values = ["", "garbage", "nan", "inf", "1e999", "9" * 100000]
    values.extend("".join(chr(rng.randrange(32, 127)) for _ in range(30)) for _ in range(50))
    requests = []
    observations = []
    monkeypatch.setattr(usage_store, "default_store", lambda: pytest.fail("garbage must not reach cache"))

    def respond(request):
        requests.append(request)
        value = values[len(requests) - 1]
        return http.Response(200, headers={key: value for key in CLAUDE}, content=b"unchanged")

    owner = http.Client(transport=http.MockTransport(respond))
    client = h.with_anthropic_headers(owner)
    for _ in values:
        observations.append(client.get("https://example.test").content)
    assert observations == [b"unchanged"] * len(values)
    assert len(requests) == len(values)
    owner.close()
