"""A provider that goes SILENT mid-stream must be retried, not lose the turn (#2305).

Reported signature: `No streaming chunk received for 120.0s (model=protolabs/cloud,
chunks_received=1)` on ~17% of turns — "no partial result, no retry, and any work the
turn was asked to do simply does not happen".
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from graph.llm import RETRYABLE_STREAM_ERRORS, _chunk_has_content, _stream_with_reconnect

try:
    from langchain_openai import StreamChunkTimeoutError
except ImportError:  # pragma: no cover
    StreamChunkTimeoutError = None

_no_sleep = lambda _d: asyncio.sleep(0)  # noqa: E731


class _Msg:
    def __init__(self, content="", reasoning=None):
        self.content = content
        self.additional_kwargs = {"reasoning_content": reasoning} if reasoning else {}


class _Gen:
    """A ChatGenerationChunk-shaped item: content hangs off `.message`."""

    def __init__(self, content="", reasoning=None):
        self.message = _Msg(content, reasoning)


def _stream(chunks, exc, calls):
    def make():
        calls.append(1)

        async def gen():
            for c in chunks:
                yield c
            if exc:
                raise exc

        return gen()

    return make


async def _drain(make, **kw):
    out = []
    async for item in _stream_with_reconnect(make, sleep=_no_sleep, **kw):
        out.append(item)
    return out


# ── the reported failure ──────────────────────────────────────────────────────
@pytest.mark.skipif(StreamChunkTimeoutError is None, reason="older langchain_openai")
def test_the_stall_error_is_retryable_at_all():
    """It subclasses TimeoutError/OSError — NOT httpx/httpcore — so it used to fall
    through RETRYABLE_STREAM_ERRORS entirely and no stall was ever retried."""
    assert issubclass(StreamChunkTimeoutError, RETRYABLE_STREAM_ERRORS)


@pytest.mark.skipif(StreamChunkTimeoutError is None, reason="older langchain_openai")
@pytest.mark.asyncio
async def test_a_stall_one_chunk_in_reconnects_instead_of_killing_the_turn():
    """The exact #2305 signature: chunks_received=1 (the role delta, no content) then
    silence. Pre-fix this ran once and raised."""
    calls: list = []
    exc = StreamChunkTimeoutError(120.0, model_name="protolabs/cloud", chunks_received=1)

    with pytest.raises(StreamChunkTimeoutError):
        await _drain(_stream([_Gen("")], exc, calls), max_retries=2)

    assert len(calls) == 3  # initial + 2 reconnects, not 1


@pytest.mark.skipif(StreamChunkTimeoutError is None, reason="older langchain_openai")
@pytest.mark.asyncio
async def test_a_stall_that_recovers_yields_the_retry_content():
    calls: list = []
    exc = StreamChunkTimeoutError(120.0, chunks_received=1)

    def make():
        calls.append(1)
        first = len(calls) == 1

        async def gen():
            yield _Gen("")
            if first:
                raise exc
            yield _Gen("the answer")

        return gen()

    out = await _drain(make, max_retries=2)

    assert [getattr(o.message, "content", "") for o in out] == ["", "", "the answer"]


# ── the guard that must NOT regress ───────────────────────────────────────────
@pytest.mark.skipif(StreamChunkTimeoutError is None, reason="older langchain_openai")
@pytest.mark.asyncio
async def test_a_stall_AFTER_content_still_raises_and_never_duplicates():
    """The whole reason the old rule existed: replaying a stream that already emitted
    content would double it in the user's answer."""
    calls: list = []
    exc = StreamChunkTimeoutError(120.0, chunks_received=5)

    with pytest.raises(StreamChunkTimeoutError):
        await _drain(_stream([_Gen("partial answer")], exc, calls), max_retries=2)

    assert len(calls) == 1  # no replay


@pytest.mark.asyncio
async def test_a_transport_drop_after_content_still_raises():
    calls: list = []
    with pytest.raises(httpx.ReadError):
        await _drain(_stream([_Gen("hi")], httpx.ReadError("boom"), calls), max_retries=2)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_1728_rate_limit_case_still_reconnects():
    """A provider closing the stream at the top emits nothing — unchanged behaviour."""
    calls: list = []
    with pytest.raises(httpx.ReadError):
        await _drain(_stream([], httpx.ReadError("closed"), calls), max_retries=2)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_the_happy_path_is_a_pass_through():
    calls: list = []
    out = await _drain(_stream([_Gen("a"), _Gen("b")], None, calls), max_retries=2)
    assert [o.message.content for o in out] == ["a", "b"] and len(calls) == 1


# ── where the content line is drawn ───────────────────────────────────────────
def test_the_role_delta_counts_as_no_content():
    assert _chunk_has_content(_Gen("")) is False


def test_real_text_counts_as_content():
    assert _chunk_has_content(_Gen("hello")) is True


def test_reasoning_counts_as_content_because_it_is_rendered():
    assert _chunk_has_content(_Gen("", reasoning="thinking…")) is True


def test_an_unfamiliar_chunk_shape_is_treated_as_content():
    """Conservative on purpose: an unreadable shape costs a retry we could have had,
    rather than risking a duplicated answer."""
    assert _chunk_has_content(object()) is True
