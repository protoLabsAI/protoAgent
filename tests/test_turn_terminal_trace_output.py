"""A turn's trace output is what the caller received — its terminal frame — on every
path, not only a native turn that ends on a tool-call-free reply.

Before this, an ``@delegate`` exchange, a slash command, a turn parked on ``ask_human``
and a failed turn all left a Langfuse trace with input and no output. The wrapper that
writes it sits OUTSIDE the impl generator that opens ``trace_session``: this drives the
real wrapper against a real ``trace_session`` to prove the session span is still current
there (a generator runs in its consumer's context).
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

from observability import tracing

# `server.chat` the attribute is the re-exported `chat` function; take the module.
chat = importlib.import_module("server.chat")


@pytest.fixture
def session_span(monkeypatch):
    fake = MagicMock()
    span = MagicMock()
    span.trace_id = "c" * 32
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=None)
    fake.start_as_current_observation.return_value = cm
    monkeypatch.setattr(tracing, "_langfuse", fake)
    monkeypatch.setattr(tracing, "_enabled", True)
    return span


def _impl_yielding(*frames, incognito: bool = False):
    async def impl(message, session_id, **_kw):
        async with tracing.trace_session(session_id, name="a2a-stream", input=message, incognito=incognito):
            for frame in frames:
                yield frame

    return impl


async def _drain(monkeypatch, impl) -> list:
    monkeypatch.setattr(chat, "_chat_langgraph_stream_impl", impl)
    return [ev async for ev in chat._chat_langgraph_stream("hi", "sess-1")]


def _outputs(span) -> list:
    return [c.kwargs["output"] for c in span.update.call_args_list if "output" in c.kwargs]


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        (("done", "hermes says hi"), "hermes says hi"),
        (("input_required", {"question": "What did postage cost?"}), "[input required] What did postage cost?"),
        (("input_required", {"kind": "approval", "title": "Run rm -rf build?"}), "[input required] Run rm -rf build?"),
        (("error", "provider closed the stream"), "[error] provider closed the stream"),
    ],
)
async def test_terminal_frame_becomes_the_trace_output(monkeypatch, session_span, frame, expected):
    events = await _drain(monkeypatch, _impl_yielding(("tool_start", {"id": "t"}), frame))

    assert events[-1] == frame  # passed through untouched
    assert _outputs(session_span) == [expected]


async def test_terminal_output_is_redacted(monkeypatch, session_span):
    await _drain(monkeypatch, _impl_yielding(("done", "key is sk-" + "A" * 40)))

    assert "A" * 40 not in _outputs(session_span)[0]


async def test_a_secret_straddling_the_cap_is_still_redacted(monkeypatch, session_span):
    secret = "sk-" + "A" * 40
    # Only "sk-" + 10 chars survive the cap: too short for the key pattern on its own.
    text = "x" * (tracing.MAX_IO_CHARS - 14) + " " + secret
    await _drain(monkeypatch, _impl_yielding(("done", text)))

    (out,) = _outputs(session_span)
    assert len(out) <= tracing.MAX_IO_CHARS
    assert "A" * 10 not in out  # not even the half that survived the cut


async def test_incognito_turn_records_no_output(monkeypatch, session_span):
    await _drain(monkeypatch, _impl_yielding(("done", "secret answer"), incognito=True))

    assert _outputs(session_span) == []


async def test_non_terminal_frames_write_nothing(monkeypatch, session_span):
    await _drain(monkeypatch, _impl_yielding(("tool_start", {"id": "t"}), ("usage", {"tokens": 3})))

    assert _outputs(session_span) == []
