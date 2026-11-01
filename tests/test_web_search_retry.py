"""web_search retry / backoff behaviour (#4578).

``ddgs`` raises ``DDGSException('No results found.')`` BOTH for a genuinely empty
result and for a throttled/failing backend, so a single failed call cannot tell
"the web has nothing" from "we never actually searched" — a daily sweep once
logged "nothing found" for three days without ever having searched.

These tests pin the fix without touching the network: a fake ``ddgs.DDGS`` is
monkeypatched in, ``asyncio.sleep`` is replaced so the ~2s backoff is instant,
and we assert that

* a first failure + a successful retry returns the numbered results (and the
  retry used the alternate ``backend=``),
* two failures NEVER read as an empty result (rate-limited / failed message),
* two ambiguous 'No results found.' misses are reported as unverified, and
* a search that actually completes empty returns the plain "No results for …".

The real synchronous search still runs in a worker thread (``asyncio.to_thread``)
and the backoff is an awaited ``asyncio.sleep`` — both asserted below.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from ddgs.exceptions import DDGSException, RatelimitException

import tools.lg_tools as lg
from tools.lg_tools import web_search


def _record(calls, behaviors, query, max_results, backend):
    idx = len(calls)
    calls.append(
        {
            "query": query,
            "max_results": max_results,
            "backend": backend,
            "thread": threading.get_ident(),
        }
    )
    behavior = behaviors[idx]
    if isinstance(behavior, BaseException):
        raise behavior
    return list(behavior)


def _make_ddgs(behaviors, calls, *, accept_backend=True):
    """Build a fake ``ddgs.DDGS`` class driven by a per-call ``behaviors`` list.

    Each entry is either an exception to raise or an iterable of result dicts to
    return. ``accept_backend`` controls whether ``text`` advertises a ``backend``
    kwarg (via ``**kwargs``), exercising the signature probe.
    """
    if accept_backend:

        def text(self, query, max_results=5, **kwargs):  # noqa: ANN001
            return _record(calls, behaviors, query, max_results, kwargs.get("backend"))

    else:

        def text(self, query, max_results=5):  # noqa: ANN001
            return _record(calls, behaviors, query, max_results, None)

    return type(
        "FakeDDGS",
        (),
        {
            "__init__": lambda self: None,
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
            "text": text,
        },
    )


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace asyncio.sleep with an instant recorder; returns the slept-durations list."""
    slept: list[float] = []

    async def fake_sleep(secs, *a, **kw):
        slept.append(secs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


async def test_first_fails_then_retry_succeeds(monkeypatch, no_sleep):
    calls: list[dict] = []
    behaviors = [
        DDGSException("No results found."),  # first attempt: ambiguous failure
        [{"title": "LangGraph", "href": "https://example.com/lg", "body": "a graph runtime"}],
    ]
    monkeypatch.setattr("ddgs.DDGS", _make_ddgs(behaviors, calls))

    out = await web_search.ainvoke({"query": "langgraph", "max_results": 3})

    # Returns the numbered results, not an error / empty message.
    assert "1. LangGraph — https://example.com/lg" in out
    assert "a graph runtime" in out
    assert "1 result(s) for 'langgraph'" in out

    # Exactly two attempts; the retry routed through the alternate backend.
    assert len(calls) == 2
    assert calls[0]["backend"] is None
    assert calls[1]["backend"] == lg._DDGS_RETRY_BACKEND

    # Backoff happened once, as an awaited async sleep (loop not blocked).
    assert no_sleep == [lg._DDGS_RETRY_BACKOFF_S]

    # The blocking ddgs call ran off the event loop, in a worker thread.
    assert calls[0]["thread"] != threading.get_ident()


async def test_both_attempts_ratelimited(monkeypatch, no_sleep):
    calls: list[dict] = []
    behaviors = [
        RatelimitException("backend 202 Ratelimit"),
        RatelimitException("backend 202 Ratelimit"),
    ]
    monkeypatch.setattr("ddgs.DDGS", _make_ddgs(behaviors, calls))

    out = await web_search.ainvoke({"query": "python", "max_results": 5})

    assert len(calls) == 2
    assert out.startswith("Error:")
    assert "rate-limited" in out
    assert "tried twice" in out
    assert "NOT an empty result" in out
    # Must never be the bare "No results for …" empty-result wording.
    assert "No results for" not in out
    assert no_sleep == [lg._DDGS_RETRY_BACKOFF_S]


async def test_both_attempts_ambiguous_no_results(monkeypatch, no_sleep):
    calls: list[dict] = []
    behaviors = [
        DDGSException("No results found."),
        DDGSException("No results found."),
    ]
    monkeypatch.setattr("ddgs.DDGS", _make_ddgs(behaviors, calls))

    out = await web_search.ainvoke({"query": "obscure query", "max_results": 2})

    assert len(calls) == 2
    # Both attempts RAISED, so the search never completed: this MUST read as a failure, not
    # an empty result. The "Error:" prefix is what AuditMiddleware keys on — a both-fail reply
    # without it would be logged as a successful empty search (the #4578 regression).
    assert out.startswith("Error:")
    # Worded as unverified — ddgs reports throttling the same way it reports an empty result.
    assert "could not be verified" in out
    assert "NOT a confirmed empty result" in out
    # NEVER the bare empty-result form, confident or otherwise.
    assert "No results for" not in out
    assert not out.startswith("No results")
    assert no_sleep == [lg._DDGS_RETRY_BACKOFF_S]


async def test_both_attempts_generic_failure(monkeypatch, no_sleep):
    """Two failures that are neither ratelimit nor 'no results' still report as a failure."""
    calls: list[dict] = []
    behaviors = [
        DDGSException("backend returned 503"),
        TimeoutError("read timed out"),
    ]
    monkeypatch.setattr("ddgs.DDGS", _make_ddgs(behaviors, calls))

    out = await web_search.ainvoke({"query": "flaky backend", "max_results": 5})

    assert len(calls) == 2
    assert out.startswith("Error:")
    assert "rate-limited or failed" in out
    assert "tried twice" in out
    assert "NOT an empty result" in out
    # Surfaces the underlying reason so operators can see what actually broke.
    assert "read timed out" in out
    # Must never collapse into the empty-result wording.
    assert "No results for" not in out
    assert not out.startswith("No results")
    assert no_sleep == [lg._DDGS_RETRY_BACKOFF_S]


async def test_genuine_empty_list(monkeypatch, no_sleep):
    calls: list[dict] = []
    behaviors = [[]]  # first attempt COMPLETES and returns zero rows — a true miss
    monkeypatch.setattr("ddgs.DDGS", _make_ddgs(behaviors, calls))

    out = await web_search.ainvoke({"query": "nothinghere", "max_results": 4})

    assert out == "No results for 'nothinghere'."
    # Completed on the first call → no retry, no backoff.
    assert len(calls) == 1
    assert no_sleep == []
    # Still offloaded to a worker thread.
    assert calls[0]["thread"] != threading.get_ident()


async def test_retry_without_backend_support(monkeypatch, no_sleep):
    """If ddgs's ``text`` can't take a backend kwarg, the retry falls back to a plain call."""
    calls: list[dict] = []
    behaviors = [
        DDGSException("No results found."),
        [{"title": "Hit", "href": "https://example.com/h", "body": ""}],
    ]
    monkeypatch.setattr("ddgs.DDGS", _make_ddgs(behaviors, calls, accept_backend=False))

    out = await web_search.ainvoke({"query": "fallback", "max_results": 1})

    assert "1. Hit — https://example.com/h" in out
    assert len(calls) == 2
    # No backend was passed on either attempt (signature doesn't support it).
    assert calls[0]["backend"] is None
    assert calls[1]["backend"] is None
