"""Regression coverage for the answer-streaming flush threshold (_FLUSH_CHARS).

Proves the many-frame path a lower threshold produces doesn't strand the
terminal frames on the #1713 teardown-cancel grace window, that the full
answer always lands intact regardless of how many artifact-update frames it
took to get there, and that the wire actually flushes at ~_FLUSH_CHARS
granularity (frame count ≈ answer_len / _FLUSH_CHARS) — so a silent
regression back to coarse batching fails here, not in the console.
"""

import json
import logging

import httpx
import pytest

from tests.test_a2a_handler import A2A_HEADERS, _build_app

# ≥4KB, and chunked small enough, to force many flushes at any realistic
# _FLUSH_CHARS value — the scenario the teardown-race concern was originally
# about (#2672 sized the real-world case at 11KB ≈ 48 frames at the old 240).
_ANSWER = "The quick brown fox jumps over the lazy dog. " * 100
assert len(_ANSWER) >= 4096
_FLUSH_CHARS = 60  # mirrors the executor-local constant (a2a_impl/executor.py)


async def _fast_dense_stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
    step = 7  # small increments, no await between them — worst case for drain time
    for i in range(0, len(_ANSWER), step):
        yield ("text", _ANSWER[i : i + step])
    yield ("done", _ANSWER)


async def _run_one(app, rpc_id):
    frame_count = 0
    text = ""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=15) as c:
        async with c.stream(
            "POST",
            "/a2a",
            headers=A2A_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "SendStreamingMessage",
                "params": {"message": {"messageId": rpc_id, "role": "ROLE_USER", "parts": [{"text": "hi"}]}},
            },
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                au = json.loads(line[5:].strip()).get("result", {}).get("artifactUpdate")
                if not au:
                    continue
                frame_count += 1
                for part in au.get("artifact", {}).get("parts", []):
                    if part.get("text"):
                        text = f"{text}{part['text']}" if au.get("append") else part["text"]
    return frame_count, text


@pytest.mark.asyncio
async def test_many_small_flushes_never_strand_the_terminal_frames(caplog):
    """A dense ≥4KB stream still lands the complete answer every time, at the
    granularity _FLUSH_CHARS promises, with no teardown-grace-window warning."""
    caplog.set_level(logging.WARNING, logger="a2a_impl.registry")
    # The wire carries ~one append frame per _FLUSH_CHARS of answer (each flush
    # rounds up to a whole 7-char delta, the tail flushes on `done`, plus the
    # one terminal REPLACE) — so total frames ≈ len/60 within a ±20% band. A
    # regression back to coarse batching (or a per-token flood) breaks the band.
    expected = len(_ANSWER) / _FLUSH_CHARS
    for i in range(8):
        app = _build_app(_fast_dense_stream)
        frame_count, text = await _run_one(app, f"r{i}")
        assert text == _ANSWER, f"iteration {i}: answer truncated/corrupted ({len(text)} vs {len(_ANSWER)} chars)"
        assert 0.8 * expected <= frame_count <= 1.2 * expected, (
            f"iteration {i}: {frame_count} artifact frames for a {len(_ANSWER)}-char answer — "
            f"expected ≈{expected:.0f} (±20%) at _FLUSH_CHARS={_FLUSH_CHARS}"
        )

    stuck_warnings = [r.message for r in caplog.records if "still pending" in r.message]
    assert not stuck_warnings, f"teardown grace window was hit: {stuck_warnings}"
