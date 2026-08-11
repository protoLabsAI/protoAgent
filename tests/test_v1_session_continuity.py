"""/v1 session continuity + defined disconnect semantics (#2119)."""

from __future__ import annotations

import asyncio

import pytest

from operator_api.chat_routes import _run_v1_turn, _v1_session_id


class _Req:
    """Minimal stand-in for the Starlette Request the route receives."""

    def __init__(self, headers=None):
        self.headers = headers or {}


# ── which key a turn resumes ──────────────────────────────────────────────────
def test_no_key_mints_a_fresh_unique_session():
    """Unchanged behaviour for an existing stateless caller — but unique, where the old
    `int(time.time())` silently POOLED two callers landing in the same second."""
    a = _v1_session_id({}, _Req())
    b = _v1_session_id({}, _Req())

    assert a != b
    assert a.startswith("openai-compat-") and b.startswith("openai-compat-")


def test_body_session_id_pins_the_session():
    first = _v1_session_id({"session_id": "plan-run-7"}, _Req())
    second = _v1_session_id({"session_id": "plan-run-7"}, _Req())

    assert first == second == "openai-compat-plan-run-7"


def test_x_session_id_header_pins_the_session():
    sid = _v1_session_id({}, _Req({"X-Session-Id": "wf-42"}))
    assert sid == "openai-compat-wf-42"


def test_openai_user_field_pins_the_session():
    """The field the issue proposed honouring — an OpenAI client already has it."""
    assert _v1_session_id({"user": "josh"}, _Req()) == "openai-compat-josh"


def test_precedence_is_body_then_header_then_user():
    req = {"session_id": "body", "user": "user"}
    assert _v1_session_id(req, _Req({"X-Session-Id": "hdr"})) == "openai-compat-body"
    assert _v1_session_id({"user": "user"}, _Req({"X-Session-Id": "hdr"})) == "openai-compat-hdr"
    assert _v1_session_id({"user": "user"}, _Req()) == "openai-compat-user"


def test_blank_values_fall_through_rather_than_pinning_an_empty_session():
    sid = _v1_session_id({"session_id": "   ", "user": ""}, _Req({"X-Session-Id": " "}))
    assert sid != "openai-compat-" and len(sid) > len("openai-compat-")


# ── the key reaches filesystem paths, so it must be sanitized ─────────────────
def test_path_traversal_in_a_caller_supplied_key_is_neutralized():
    """A session id reaches memory paths; `../` from a /v1 caller must not escape."""
    sid = _v1_session_id({"session_id": "../../etc/passwd"}, _Req())

    assert ".." not in sid and "/" not in sid


def test_separators_and_exotic_characters_are_stripped():
    sid = _v1_session_id({"user": "a/b\\c:d*e\x00f"}, _Req())

    assert all(c.isalnum() or c in "-_." for c in sid)


def test_a_very_long_key_is_capped():
    sid = _v1_session_id({"session_id": "x" * 5000}, _Req())
    assert len(sid) <= len("openai-compat-") + 96


def test_distinct_keys_stay_distinct_after_sanitizing():
    a = _v1_session_id({"session_id": "team-a"}, _Req())
    b = _v1_session_id({"session_id": "team-b"}, _Req())
    assert a != b


# ── disconnect semantics ──────────────────────────────────────────────────────
# These tests script a three-way race — the turn starts, the caller disconnects, the
# turn then finishes — so every step waits on the *event that step is about to observe*
# rather than on a wall-clock guess. Fixed sleeps made this file flaky on the Windows
# runner (#2551): the drain logged ~1ms after a 50ms budget expired, on a diff that
# touched only CSS. Timing-flaky tests here block unrelated PRs once the Windows job
# becomes a required check (#2455).
async def _until(predicate, *, timeout: float = 2.0) -> None:
    """Wait for an observable effect, bounded by the effect — not by the clock."""

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


@pytest.mark.asyncio
async def test_turn_runs_to_completion_when_the_caller_goes_away(monkeypatch):
    """The reported failure: a client-side timeout left a 15-minute turn's work
    unreachable. The turn must survive the caller and land in its session."""
    started, keep_going, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def _slow_chat(prompt, session_id, **kw):
        started.set()
        await keep_going.wait()  # still in flight while the caller disconnects
        finished.set()
        return [{"role": "assistant", "content": "done"}]

    monkeypatch.setattr("operator_api.chat_routes.chat", _slow_chat)

    waiter = asyncio.ensure_future(_run_v1_turn("go", "openai-compat-s1"))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    waiter.cancel()  # the client disconnects mid-turn
    with pytest.raises(asyncio.CancelledError):
        await waiter

    keep_going.set()  # the turn only now runs to its end — with nobody awaiting it
    await asyncio.wait_for(finished.wait(), timeout=2.0)  # turn completed anyway


@pytest.mark.asyncio
async def test_a_failure_after_the_caller_left_is_retrieved_not_dangling(monkeypatch, caplog):
    """Without draining, an orphaned turn's exception surfaces at GC time as a bare
    'never retrieved' with no session attached."""
    started, keep_going = asyncio.Event(), asyncio.Event()

    async def _boom(prompt, session_id, **kw):
        started.set()
        await keep_going.wait()
        raise RuntimeError("gateway died")

    monkeypatch.setattr("operator_api.chat_routes.chat", _boom)

    waiter = asyncio.ensure_future(_run_v1_turn("go", "openai-compat-s2"))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    keep_going.set()  # the turn fails with the caller already gone
    await _until(lambda: "openai-compat-s2" in caplog.text and "gateway died" in caplog.text)


@pytest.mark.asyncio
async def test_normal_completion_still_returns_the_result(monkeypatch):
    async def _ok(prompt, session_id, **kw):
        return [{"role": "assistant", "content": "hi"}]

    monkeypatch.setattr("operator_api.chat_routes.chat", _ok)

    assert await _run_v1_turn("go", "s") == [{"role": "assistant", "content": "hi"}]


@pytest.mark.asyncio
async def test_an_error_propagates_normally_to_a_connected_caller(monkeypatch):
    async def _boom(prompt, session_id, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr("operator_api.chat_routes.chat", _boom)

    with pytest.raises(RuntimeError):
        await _run_v1_turn("go", "s")
