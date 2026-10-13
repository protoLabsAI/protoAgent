"""Streaming model calls must honor ``model.request_timeout`` (#3699).

A lead-agent request on ``anthropic-oauth:claude-opus-5-5`` (262K input tokens) hung
>17 min under ``request_timeout: 120`` / ``max_retries: 2`` — nothing logged, no retry,
until the operator cancelled. The client's timeout only bounds a single socket read, so
an SSE stream that stays OPEN while producing no assistant chunk was never caught.

The fix bounds each stream on two deadlines derived from ``request_timeout`` — the
time-to-first-token and the inter-chunk idle — raising a retryable ``StreamStallTimeout``
so the existing reconnect path applies ``max_retries`` and then fails with a clear error.
A stream that keeps producing chunks (a long output) resets the deadline every chunk and
still completes.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from graph.llm import (
    RETRYABLE_STREAM_ERRORS,
    StreamStallTimeout,
    _guard_stream_timeout,
    _guarded_reconnecting_stream,
    _stream_timeout_s,
)

_no_sleep = lambda _d: asyncio.sleep(0)  # noqa: E731 — skip reconnect backoff in tests


class _Msg:
    def __init__(self, content=""):
        self.content = content
        self.additional_kwargs: dict = {}


class _Gen:
    """A ChatGenerationChunk-shaped item: content hangs off ``.message`` (matches
    ``_chunk_has_content``). An empty string is the role-delta / no-content chunk."""

    def __init__(self, content=""):
        self.message = _Msg(content)


# ── fake streams ──────────────────────────────────────────────────────────────
def _never_first_chunk():
    """Opens the stream and then goes silent forever (the reported failure)."""

    async def gen():
        await asyncio.sleep(3600)
        yield _Gen("unreachable")

    return gen()


def _one_chunk_then_idle():
    """Yields the content-less role delta (chunks_received=1) and then stalls."""

    async def gen():
        yield _Gen("")
        await asyncio.sleep(3600)
        yield _Gen("unreachable")

    return gen()


def _keeps_yielding(n, gap):
    """Streams ``n`` content chunks ``gap`` apart — each gap under the deadline, but the
    TOTAL well over it."""

    async def gen():
        for i in range(n):
            await asyncio.sleep(gap)
            yield _Gen(f"tok{i}")

    return gen()


async def _drain_guard(stream, **kw):
    return [c async for c in _guard_stream_timeout(stream, **kw)]


async def _drain_reconnect(make, **kw):
    return [c async for c in _guarded_reconnecting_stream(make, sleep=_no_sleep, **kw)]


# ── the guard classifies as retryable ──────────────────────────────────────────
def test_stall_timeout_is_a_retryable_stream_error():
    assert issubclass(StreamStallTimeout, RETRYABLE_STREAM_ERRORS)
    assert issubclass(StreamStallTimeout, TimeoutError)


def test_stream_timeout_s_only_accepts_a_positive_number():
    assert _stream_timeout_s(120) == 120.0
    assert _stream_timeout_s(0.05) == 0.05
    assert _stream_timeout_s(0) is None
    assert _stream_timeout_s(-1) is None
    assert _stream_timeout_s(None) is None
    assert _stream_timeout_s((3.0, 60.0)) is None  # an httpx (connect, read) tuple


# ── r2: never a first chunk → timeout, retried, then a clear failure ────────────
async def test_r2_no_first_chunk_raises_a_stall_timeout_promptly():
    with pytest.raises(StreamStallTimeout) as ei:
        # wait_for is a watchdog on the TEST: if the guard failed to fire, the fake would
        # sleep 3600s and this would surface as a plain TimeoutError instead.
        await asyncio.wait_for(
            _drain_guard(_never_first_chunk(), timeout=0.05, label="test-model"), 2.0
        )
    assert "test-model" in str(ei.value)
    assert "first token" in str(ei.value)


async def test_r2_no_first_chunk_is_retried_per_max_retries_then_fails_clearly():
    calls: list = []

    def make():
        calls.append(1)
        return _never_first_chunk()

    with pytest.raises(StreamStallTimeout) as ei:
        await _drain_reconnect(
            make, timeout=0.05, max_retries=2, label="anthropic-oauth model 'claude-opus-5-5'"
        )

    assert len(calls) == 3  # initial + 2 retries, not 1
    assert "claude-opus-5-5" in str(ei.value)
    assert "request_timeout" in str(ei.value)


# ── r3: one chunk then idle → aborted and retried the same way ──────────────────
async def test_r3_idle_after_first_chunk_is_aborted_and_retried():
    calls: list = []

    def make():
        calls.append(1)
        return _one_chunk_then_idle()

    with pytest.raises(StreamStallTimeout) as ei:
        await _drain_reconnect(make, timeout=0.05, max_retries=2, label="m")

    # The lone chunk is the content-less role delta, so replaying it is safe and the
    # reconnect loop retries — the exact #2305/#3699 chunks_received=1 signature.
    assert len(calls) == 3
    assert "between chunks" in str(ei.value)


# ── r4: a stream producing longer than request_timeout in total still completes ─
async def test_r4_long_output_past_request_timeout_completes_normally():
    out = await _drain_guard(_keeps_yielding(12, 0.02), timeout=0.1, label="m")
    # 12 * 0.02 = 0.24s total, well over the 0.1s deadline — but each gap is under it, so
    # the per-chunk deadline never trips.
    assert [c.message.content for c in out] == [f"tok{i}" for i in range(12)]


async def test_r4_long_output_completes_through_the_reconnecting_stream():
    calls: list = []

    def make():
        calls.append(1)
        return _keeps_yielding(12, 0.02)

    out = await _drain_reconnect(make, timeout=0.1, max_retries=2, label="m")
    assert [c.message.content for c in out] == [f"tok{i}" for i in range(12)]
    assert len(calls) == 1  # completed on the first attempt, no spurious reconnect


# ── the guard is transparent to real transport errors (no regression) ───────────
async def test_a_real_transport_error_passes_through_unchanged():
    import httpx

    async def gen():
        raise httpx.ReadError("boom")
        yield  # pragma: no cover — makes this an async generator

    with pytest.raises(httpx.ReadError):
        await _drain_guard(gen(), timeout=0.05, label="m")


# ── r2/r3 on the real openai-compatible client: it fails the CALL ───────────────
async def test_openai_compat_astream_enforces_request_timeout(monkeypatch):
    from langchain_openai import ChatOpenAI

    import graph.llm as llm_mod

    calls: list = []

    async def fake_astream(self, *a, **k):
        calls.append(1)
        await asyncio.sleep(3600)
        yield _Gen("unreachable")

    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream, raising=True)

    client = llm_mod._ReasoningChatOpenAI(
        model="protolabs/reasoning", api_key="k", timeout=0.05, max_retries=1
    )

    async def run():
        return [c async for c in client._astream([])]

    with pytest.raises(StreamStallTimeout) as ei:
        await asyncio.wait_for(run(), 5.0)

    assert calls == [1, 1]  # initial + 1 retry (max_retries=1)
    assert "protolabs/reasoning" in str(ei.value)


# ── r1: the anthropic-oauth client is built with the configured request_timeout ─
def _fake_creds(token="tok-abc"):
    return types.SimpleNamespace(access_token=token)


def _oauth_config(**over):
    base = dict(
        model_name="claude-opus-5-5",
        max_tokens=1024,
        request_timeout=120.0,
        llm_max_retries=2,
        reasoning_effort="",
        thinking="",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_r1_anthropic_oauth_client_is_built_with_request_timeout(monkeypatch):
    import graph.providers.anthropic_oauth as ao

    if ao._OAuthChatAnthropic is None:  # pragma: no cover — langchain-anthropic absent
        pytest.skip("langchain-anthropic is not importable")

    monkeypatch.setattr(ao, "resolve_anthropic_oauth", lambda: _fake_creds())

    llm = ao.build_anthropic_oauth_llm(_oauth_config())

    assert llm.default_request_timeout == 120.0
    assert llm.max_retries == 2
    assert llm.model == "claude-opus-5-5"


async def test_anthropic_oauth_astream_enforces_request_timeout(monkeypatch):
    import graph.providers.anthropic_oauth as ao

    if ao._OAuthChatAnthropic is None:  # pragma: no cover — langchain-anthropic absent
        pytest.skip("langchain-anthropic is not importable")

    from langchain_anthropic import ChatAnthropic

    calls: list = []

    async def fake_astream(self, *a, **k):
        calls.append(1)
        await asyncio.sleep(3600)
        yield _Gen("unreachable")

    monkeypatch.setattr(ChatAnthropic, "_astream", fake_astream, raising=True)
    # Keep the token refresh a no-op (no keychain shell-out): current == cached.
    monkeypatch.setattr(ao, "current_oauth_token", lambda **_k: "tok-abc")

    client = ao._OAuthChatAnthropic(
        model="claude-opus-5-5",
        api_key="oauth-via-auth-token",
        oauth_token="tok-abc",
        default_request_timeout=0.05,
        max_retries=1,
    )

    async def run():
        return [c async for c in client._astream([])]

    with pytest.raises(StreamStallTimeout) as ei:
        await asyncio.wait_for(run(), 5.0)

    assert calls == [1, 1]  # initial + 1 retry
    assert "anthropic-oauth" in str(ei.value)
    assert "claude-opus-5-5" in str(ei.value)
