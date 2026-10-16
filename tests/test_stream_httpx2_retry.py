"""A mid-stream reset from the provider SDKs' ``httpx2`` stack takes the retryable path.

anthropic>=1.8 and openai>=3 talk HTTP through ``httpx2``/``httpcore2``, a separate class
hierarchy from ``httpx``/``httpcore``: ``httpx2.ReadError`` is NOT an
``httpx.TransportError``. Live (2026-10-16, jobCoach, ``anthropic-oauth:claude-opus-5-5``)
a connection reset surfaced as ``ConnectionResetError`` → ``httpcore2.ReadError`` →
``httpx2.ReadError`` out of ``anthropic._streaming`` — it matched nothing in
``RETRYABLE_STREAM_ERRORS``, so the #1728 reconnect never ran and the turn failed as an
"unhandled exception" instead of a classified provider stream drop.
"""

from __future__ import annotations

import asyncio

import pytest

from graph.llm import (
    RETRYABLE_STREAM_ERRORS,
    StreamStallTimeout,
    _guard_stream_timeout,
    _stream_with_reconnect,
)

httpx2 = pytest.importorskip("httpx2")
httpcore2 = pytest.importorskip("httpcore2")

_no_sleep = lambda _d: asyncio.sleep(0)  # noqa: E731 — skip reconnect backoff in tests


class _Msg:
    def __init__(self, content=""):
        self.content = content
        self.additional_kwargs: dict = {}


class _Gen:
    def __init__(self, content=""):
        self.message = _Msg(content)


def _live_reset() -> BaseException:
    """The exception the live trace ended in, with its real cause chain."""
    core = httpcore2.ReadError("")
    core.__cause__ = ConnectionResetError(54, "Connection reset by peer")
    top = httpx2.ReadError("")
    top.__cause__ = core
    return top


@pytest.mark.parametrize(
    "exc_type",
    [
        "httpx2.ReadError",
        "httpx2.ReadTimeout",
        "httpx2.RemoteProtocolError",
        "httpx2.ConnectError",
        "httpcore2.ReadError",
        "httpcore2.WriteError",
        "httpcore2.ReadTimeout",
    ],
)
def test_sdk_transport_errors_are_retryable(exc_type):
    mod, name = exc_type.split(".")
    cls = getattr({"httpx2": httpx2, "httpcore2": httpcore2}[mod], name)
    assert issubclass(cls, RETRYABLE_STREAM_ERRORS)


async def test_a_reset_before_any_content_reconnects_and_the_call_completes():
    calls: list = []

    def make():
        calls.append(1)
        attempt = len(calls)

        async def gen():
            yield _Gen("")  # the content-less first chunk (message_start) — nothing seen yet
            if attempt == 1:
                raise _live_reset()
            yield _Gen("report sent")

        return gen()

    out = [c async for c in _stream_with_reconnect(make, max_retries=2, sleep=_no_sleep)]
    assert len(calls) == 2  # reconnected once instead of failing the turn
    assert [c.message.content for c in out] == ["", "", "report sent"]


async def test_a_reset_after_content_still_raises_unchanged():
    """Replaying after visible output would duplicate it — the rule is unchanged."""
    calls: list = []

    def make():
        calls.append(1)

        async def gen():
            yield _Gen("partial answer")
            raise _live_reset()

        return gen()

    with pytest.raises(httpx2.ReadError):
        _ = [c async for c in _stream_with_reconnect(make, max_retries=2, sleep=_no_sleep)]
    assert len(calls) == 1


async def test_the_stall_guard_passes_an_sdk_read_timeout_through_unchanged():
    """The guard must hand a real transport error to the reconnect loop as-is, never
    relabel it a StreamStallTimeout."""

    async def gen():
        raise httpx2.ReadTimeout("read timed out")
        yield  # pragma: no cover — makes this an async generator

    with pytest.raises(httpx2.ReadTimeout):
        _ = [c async for c in _guard_stream_timeout(gen(), timeout=5, label="m")]
    assert not issubclass(httpx2.ReadTimeout, StreamStallTimeout)


async def test_anthropic_oauth_astream_reconnects_on_an_sdk_reset(monkeypatch):
    """End to end on the live provider path: ``_OAuthChatAnthropic._astream`` →
    ``_guarded_reconnecting_stream`` → ChatAnthropic's stream raising ``httpx2.ReadError``."""
    import graph.providers.anthropic_oauth as ao

    if ao._OAuthChatAnthropic is None:  # pragma: no cover — langchain-anthropic absent
        pytest.skip("langchain-anthropic is not importable")

    from langchain_anthropic import ChatAnthropic

    calls: list = []

    async def fake_astream(self, *a, **k):
        calls.append(1)
        yield _Gen("")
        if len(calls) == 1:
            raise _live_reset()
        yield _Gen("ok")

    monkeypatch.setattr(ChatAnthropic, "_astream", fake_astream, raising=True)
    monkeypatch.setattr(ao, "current_oauth_token", lambda **_k: "tok-abc")

    client = ao._OAuthChatAnthropic(
        model="claude-opus-5-5",
        api_key="oauth-via-auth-token",
        oauth_token="tok-abc",
        default_request_timeout=120,
        max_retries=2,
    )

    out = await asyncio.wait_for(_drain(client), 5.0)
    assert calls == [1, 1]
    assert out[-1].message.content == "ok"


async def _drain(client):
    return [c async for c in client._astream([])]


def test_chat_classifies_the_live_reset_as_a_provider_stream_drop():
    """So a reset that DOES end the turn is the clean "provider closed the stream" failure,
    not an "unhandled exception" traceback."""
    from server.chat import _is_provider_stream_drop

    assert _is_provider_stream_drop(_live_reset())


def test_upstream_unreachable_recognizes_the_sdk_transport_error():
    """``/v1`` maps an unreachable upstream to 502, not an internal 500 (#3946)."""
    from graph.upstream_errors import upstream_unreachable

    assert upstream_unreachable(_live_reset())
