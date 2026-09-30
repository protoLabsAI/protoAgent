"""Steering queues don't outlive their sessions (#3933).

A queue normally empties itself — drained at the turn's next model step, ✕-dequeued, or
``forget``-ed when the chat is deleted. A session whose turn never reaches another model
step kept its entry forever; now every ``enqueue`` evicts queues idle past
``_QUEUE_TTL_S`` and, at ``_QUEUES_MAX`` sessions, the least recently written one —
logged, since it drops operator input.
"""

from __future__ import annotations

import logging

import pytest

from graph import steering


def _touched() -> dict:
    return getattr(steering, "_QUEUE_TOUCHED", {})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    steering._QUEUES.clear()
    steering._DRAINED.clear()
    _touched().clear()
    clock = [1000.0]
    monkeypatch.setattr(steering, "_now", lambda: clock[0], raising=False)
    yield clock
    steering._QUEUES.clear()
    steering._DRAINED.clear()
    _touched().clear()


def test_a_queue_idle_past_the_ttl_is_evicted_on_the_next_enqueue(_clean, monkeypatch, caplog):
    clock = _clean
    monkeypatch.setattr(steering, "_QUEUE_TTL_S", 60.0, raising=False)
    steering.enqueue("abandoned", "held behind a park nobody answers", msg_id="a1")
    clock[0] += 30
    steering.enqueue("recent", "still fresh", msg_id="r1")
    clock[0] += 31  # "abandoned" is now 61s idle, "recent" 31s

    with caplog.at_level(logging.WARNING, logger="graph.steering"):
        steering.enqueue("new", "hello", msg_id="n1")

    assert steering.pending("abandoned") == 0 and "abandoned" not in steering._QUEUES
    assert "abandoned" not in _touched()
    assert steering.pending("recent") == 1 and steering.pending("new") == 1
    assert any("dropped 1 undelivered" in r.getMessage() and "abandoned" in r.getMessage() for r in caplog.records)


def test_writing_to_a_queue_keeps_it_alive(_clean, monkeypatch):
    clock = _clean
    monkeypatch.setattr(steering, "_QUEUE_TTL_S", 60.0, raising=False)
    steering.enqueue("s", "one", msg_id="1")
    clock[0] += 50
    steering.enqueue("s", "two", msg_id="2")  # refreshes the idle clock
    clock[0] += 50
    steering.enqueue("other", "x", msg_id="3")

    assert [i["id"] for i in steering.pending_items("s")] == ["1", "2"]


def test_the_registry_is_capped_evicting_the_least_recently_written(_clean, monkeypatch, caplog):
    clock = _clean
    monkeypatch.setattr(steering, "_QUEUES_MAX", 3, raising=False)
    for sid in ("a", "b", "c"):
        steering.enqueue(sid, "m", msg_id=sid)
        clock[0] += 1
    steering.enqueue("a", "again", msg_id="a2")  # "a" is now the most recent; "b" the oldest

    with caplog.at_level(logging.WARNING, logger="graph.steering"):
        steering.enqueue("d", "m", msg_id="d")

    assert set(steering._QUEUES) == {"a", "c", "d"}
    assert len(steering._QUEUES) == 3
    assert any("at cap" in r.getMessage() and "session b" in r.getMessage() for r in caplog.records)

    steering.enqueue("d", "more", msg_id="d2")  # an EXISTING session at the cap evicts nothing
    assert set(steering._QUEUES) == {"a", "c", "d"}


def test_drain_dequeue_and_forget_release_the_idle_clock(_clean):
    steering.enqueue("drained", "x", msg_id="1")
    steering.drain("drained")
    steering.enqueue("dequeued", "x", msg_id="2")
    steering.dequeue("dequeued", "2")
    steering.enqueue("forgotten", "x", msg_id="3")
    steering.forget("forgotten")

    assert _touched() == {} and steering._QUEUES == {}


def test_deleting_a_chat_drops_its_steering_queue(monkeypatch):
    """The session-delete path (DELETE /api/chat/sessions/{id}) forgets the session's
    queue — a message queued for a chat that no longer exists is never delivered."""
    from tests.test_chat_routes import _client

    c = _client(monkeypatch)
    steering.enqueue("gone", "queued, then the chat is deleted", msg_id="g1")
    steering.enqueue("kept", "another chat", msg_id="k1")

    assert c.delete("/api/chat/sessions/gone").json()["deleted"] is True

    assert steering.pending("gone") == 0 and "gone" not in _touched()
    assert steering.pending("kept") == 1
