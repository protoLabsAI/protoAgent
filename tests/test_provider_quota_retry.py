"""Exercise quota rejection at the real Anthropic SDK HTTP boundary (#4205)."""
import asyncio

import anthropic
import pytest

from graph.providers.anthropic_oauth import _OAuthChatAnthropic
import anthropic._base_client as sdk_base


@pytest.fixture(params=["seconds", "milliseconds", "asctime"] + [
    (bad, fallback) for bad in ("invalid", "nan", "inf", "0", "-1")
    for fallback in ("seconds", "date")
])
def long_retry_headers(request):
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    if request.param == "seconds":
        return {"retry-after": "130287"}
    if request.param == "milliseconds":
        return {"retry-after-ms": "130287000"}
    if request.param == "asctime":
        return {"retry-after": (datetime.now(UTC) + timedelta(hours=36)).strftime("%a %b %d %H:%M:%S %Y")}
    bad, fallback = request.param
    value = "130287" if fallback == "seconds" else format_datetime(datetime.now(UTC) + timedelta(hours=36))
    return {"retry-after-ms": bad, "retry-after": value}


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_long_provider_retry_is_reported_without_sleep(monkeypatch, stream, status, long_retry_headers):
    monkeypatch.setattr(_OAuthChatAnthropic, "_refresh_oauth_token", lambda self: None)
    llm = _OAuthChatAnthropic(model="claude-test", oauth_token="test", max_retries=2)
    http = sdk_base.httpx2
    requests = []

    def respond(request):
        requests.append(request)
        return http.Response(status, headers=long_retry_headers, json={
            "type": "error", "error": {"type": "rate_limit_error", "message": "Weekly quota exhausted"},
        })

    async with http.AsyncClient(transport=http.MockTransport(respond)) as client:
        llm._async_client._client = client
        with pytest.raises(anthropic.RateLimitError if status == 429 else anthropic.InternalServerError, match="Weekly quota exhausted"):
            async with asyncio.timeout(5):
                if stream:
                    async for _ in llm.astream("hello"):
                        pass
                else:
                    await llm.ainvoke("hello")
    assert len(requests) == 1


@pytest.mark.parametrize("stream", [False, True])
def test_sync_long_retry_does_not_sleep(monkeypatch, stream, long_retry_headers):
    monkeypatch.setattr(_OAuthChatAnthropic, "_refresh_oauth_token", lambda self: None)
    llm = _OAuthChatAnthropic(model="claude-test", oauth_token="test", max_retries=2)
    http = sdk_base.httpx2
    calls = []

    def respond(request):
        calls.append(request)
        return http.Response(429, headers=long_retry_headers, json={
            "error": {"type": "rate_limit_error", "message": "Weekly quota exhausted"},
        })

    def no_sleep(delay):
        pytest.fail(f"SDK attempted quota backoff: {delay}")

    monkeypatch.setattr(sdk_base.time, "sleep", no_sleep)
    with http.Client(transport=http.MockTransport(respond)) as client:
        llm._client._client = client
        with pytest.raises(anthropic.RateLimitError):
            if stream:
                list(llm.stream("hello"))
            else:
                llm.invoke("hello")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_short_retry_remains_cancelable(monkeypatch):
    monkeypatch.setattr(_OAuthChatAnthropic, "_refresh_oauth_token", lambda self: None)
    llm = _OAuthChatAnthropic(model="claude-test", oauth_token="test", max_retries=2)
    http = sdk_base.httpx2
    started = asyncio.Event()
    calls = []

    def respond(request):
        calls.append(request)
        started.set()
        return http.Response(429, headers={"retry-after": "10"}, json={
            "error": {"type": "rate_limit_error", "message": "Too many requests"},
        })

    async with http.AsyncClient(transport=http.MockTransport(respond)) as client:
        llm._async_client._client = client
        turn = asyncio.create_task(llm.ainvoke("hello"))
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.sleep(0)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, 5)
    assert len(calls) == 1


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after": "130287"}, 130287),
    ({"retry-after-ms": "1500"}, 2),
    ({"retry-after-ms": "invalid", "retry-after": "130287"}, 130287),
    ({"retry-after-ms": "nan", "retry-after": "130287"}, 130287),
    ({"retry-after": "invalid"}, None),
    ({"retry-after": "nan"}, None),
    ({"retry-after": "inf"}, None),
    ({}, None),
])
def test_quota_metadata_reaches_chat_http_error(headers, expected):
    from graph.upstream_errors import rate_limit_details
    from operator_api.chat_routes import _api_chat_error, _v1_error_response
    from server.chat import turn_error

    http = sdk_base.httpx2
    response = http.Response(429, headers=headers, request=http.Request("POST", "https://provider.test/messages"))
    body = {"error": {"message": "Weekly quota exhausted"}}
    exc = anthropic.RateLimitError("Weekly quota exhausted", response=response, body=body)
    detail = rate_limit_details(exc)
    assert detail["quota_exhausted"] is True
    assert detail.get("retry_after") == expected
    assert "Weekly quota exhausted" in detail["message"]
    assert "model selector" in detail["message"]
    classified = turn_error(exc)
    error = _api_chat_error(classified, "same-session")
    assert error.status_code == 429
    assert error.detail["quota_exhausted"] is True
    assert error.detail.get("retry_after") == expected
    assert error.headers == ({"Retry-After": str(expected)} if expected else None)
    import json

    v1 = _v1_error_response(classified)
    payload = json.loads(v1.body)["error"]
    assert payload["quota_exhausted"] is True
    assert payload.get("retry_after") == expected
    assert payload.get("retry_at") == error.detail.get("retry_at")


def test_stream_stall_is_not_quota_exhaustion():
    from graph.llm import StreamStallTimeout
    from graph.upstream_errors import rate_limit_details

    assert rate_limit_details(StreamStallTimeout("stalled")) == {}


def test_http_date_retry_preserves_reset_time():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    from graph.upstream_errors import rate_limit_details

    http = sdk_base.httpx2
    reset = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=36)
    response = http.Response(429, headers={"retry-after": format_datetime(reset)}, request=http.Request("POST", "https://provider.test/messages"))
    exc = anthropic.RateLimitError("weekly limit", response=response, body={"message": "weekly limit"})
    details = rate_limit_details(exc)
    assert 129599 <= details["retry_after"] <= 129600
    assert abs((datetime.fromisoformat(details["retry_at"]) - reset).total_seconds()) < 1


def test_oauth_identity_refusal_is_not_claimed_to_be_quota():
    from graph.upstream_errors import rate_limit_details

    http = sdk_base.httpx2
    response = http.Response(429, request=http.Request("POST", "https://provider.test/messages"))
    exc = anthropic.RateLimitError("Error", response=response, body={"error": {"message": "Error"}})
    assert "quota_exhausted" not in rate_limit_details(exc)
    assert "retry_after" not in rate_limit_details(exc)
