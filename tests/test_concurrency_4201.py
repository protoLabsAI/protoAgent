"""Deterministic concurrency tests for the embed circuit breaker and the audit
session-stats (#4201).

Real OS threads reach both pieces of shared state at the same time:

* ``HybridKnowledgeStore`` breaker (``_embed_failures`` / ``_breaker_open_until``):
  searches run through ``asyncio.to_thread`` (graph/sdk.py:604-605,
  tools/memory_tools.py:725, operator_api/knowledge_routes.py:501,
  tools/lg_tools.py:380), ``warm_probe`` starts a daemon thread
  (hybrid_store.py:218, from server/agent_init.py:767), and
  ``reset_embed_breaker`` is called from operator_api/config_routes.py:91.
* ``AuditLogger._session_stats`` (the ``observability.audit.audit_logger``
  singleton): ``log()`` runs both on the event loop (AuditMiddleware async path,
  graph/middleware/audit.py:324/347) AND on worker threads — plugin install
  audits go through ``asyncio.to_thread(_run_install, ...)``
  (operator_api/python_routes.py:91, node_routes.py:107, plugin_routes.py:393 →
  graph/plugins/installer.py:452) and the sync ToolNode path runs parallel tool
  calls via ``get_executor_for_config(...).map(_run_one, ...)``
  (langgraph tool_node), each reaching the sync AuditMiddleware
  (graph/middleware/audit.py:237/260) with the SAME session_id.

Each interleaving is FORCED with event hooks, never raced-and-hoped: a chosen
thread is pinned between a read and the dependent write while a partner runs to
completion. The pin is a bounded ``Event.wait`` so that once the fix's lock is in
place — and the partner is parked on that lock instead of finishing — the pinned
thread proceeds and the operation completes correctly rather than hanging. The
RESULT is therefore identical on every run: wrong (lost update / torn state /
KeyError / RuntimeError) without the lock, correct with it. No sleeps, no stress
loops, no ``setswitchinterval``.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from knowledge.hybrid_store import HybridKnowledgeStore
from observability.audit import AuditLogger

# How long a pinned thread waits for its partner. The partner finishes in
# microseconds WITHOUT the lock (so this is never actually hit then); WITH the
# lock the partner is parked on it and never signals, so the pinned thread falls
# through after this bound and the operation completes correctly. Latency only —
# the outcome is deterministic either way.
_PIN_TIMEOUT = 0.75


class _Pin:
    """A one-shot, single-thread pause used to force an interleaving.

    Only the armed thread blocks (a symmetric pause would deadlock both sides),
    and only once. The partner thread runs meanwhile and calls :meth:`release`.
    """

    def __init__(self, timeout: float = _PIN_TIMEOUT):
        self._reached = threading.Event()
        self._go = threading.Event()
        self._ident: int | None = None
        self._fired = False
        self._timeout = timeout

    def arm(self, ident: int) -> None:
        self._ident = ident

    def wait_reached(self, timeout: float = 5.0) -> None:
        assert self._reached.wait(timeout), "the pinned thread never reached its hook"

    def release(self) -> None:
        self._go.set()

    def hook(self) -> None:
        if self._fired or threading.get_ident() != self._ident:
            return
        self._fired = True
        self._reached.set()
        self._go.wait(self._timeout)


# ── HybridKnowledgeStore breaker ──────────────────────────────────────────────


class _InstrumentedStore(HybridKnowledgeStore):
    """Pins a chosen thread between the read and the dependent write of the
    breaker's state. It does NOT override the methods under test
    (``_record_embed_failure`` / ``_record_embed_success`` /
    ``reset_embed_breaker`` / ``_probe_once``) — only the backing attributes —
    so the real (locked, once fixed) methods still run around the hook."""

    def __init__(self, *args, **kwargs):
        self._raw_failures = 0
        self._raw_open_until = 0.0
        self._fail_set_pin: _Pin | None = None  # fires inside the _embed_failures setter
        self._open_set_pin: _Pin | None = None  # fires inside the _breaker_open_until setter
        super().__init__(*args, **kwargs)

    @property
    def _embed_failures(self):
        return self._raw_failures

    @_embed_failures.setter
    def _embed_failures(self, value):
        pin = self._fail_set_pin
        if pin is not None:
            pin.hook()
        self._raw_failures = value

    @property
    def _breaker_open_until(self):
        return self._raw_open_until

    @_breaker_open_until.setter
    def _breaker_open_until(self, value):
        pin = self._open_set_pin
        if pin is not None and value:  # only the breaker-OPEN write, not the reset to 0.0
            pin.hook()
        self._raw_open_until = value


def _bow_embed(text: str) -> list[float]:
    return [1.0 if w in text.lower() else 0.0 for w in ("a", "b", "c")]


def _assert_breaker_consistent(store: _InstrumentedStore) -> None:
    """No serial order of the recorded calls can leave the breaker OPEN below the
    threshold, or CLOSED at/above it (#4201)."""
    open_ = store._breaker_open()
    tripped = store._embed_failures >= store._breaker_threshold
    assert open_ == tripped, (
        f"torn breaker: open={open_} but failures={store._embed_failures} "
        f"(threshold {store._breaker_threshold})"
    )


def test_concurrent_failures_count_exactly_and_trip_breaker(tmp_path):
    """Two threads each record one failure. The count must be exactly 2 and the
    breaker must open at the threshold — a lost increment leaves it at 1, closed."""
    store = _InstrumentedStore(str(tmp_path / "kb.db"), embed_fn=_bow_embed, breaker_threshold=2)
    pin = _Pin()
    store._fail_set_pin = pin  # pin the first thread mid read-modify-write of += 1

    def pinned():
        pin.arm(threading.get_ident())
        store._record_embed_failure()

    def partner():
        store._record_embed_failure()
        pin.release()

    a = threading.Thread(target=pinned)
    a.start()
    pin.wait_reached()
    b = threading.Thread(target=partner)
    b.start()
    a.join(10)
    b.join(10)
    store._fail_set_pin = None
    assert not a.is_alive() and not b.is_alive()
    assert store._embed_failures == 2, "a concurrent failure increment was lost"
    assert store._breaker_open(), "breaker did not open at the threshold"


def test_success_overlapping_failure_no_torn_breaker(tmp_path):
    """A success that resets the counter must not leave the breaker open — the
    failure opening the breaker and the success clearing it must not interleave
    into counter=0 + breaker-open."""
    store = _InstrumentedStore(str(tmp_path / "kb.db"), embed_fn=_bow_embed, breaker_threshold=1)
    pin = _Pin()
    store._open_set_pin = pin  # pin the failure thread as it opens the breaker

    def pinned():  # the failure: increments to threshold, then opens the breaker (pinned there)
        pin.arm(threading.get_ident())
        store._record_embed_failure()

    def partner():  # a success resets both fields meanwhile
        store._record_embed_success()
        pin.release()

    a = threading.Thread(target=pinned)
    a.start()
    pin.wait_reached()
    b = threading.Thread(target=partner)
    b.start()
    a.join(10)
    b.join(10)
    store._open_set_pin = None
    _assert_breaker_consistent(store)


def test_reset_overlapping_failure_no_torn_breaker(tmp_path):
    """``reset_embed_breaker`` (the operator "test connection" path) overlapping a
    failure must not leave a torn breaker either."""
    store = _InstrumentedStore(str(tmp_path / "kb.db"), embed_fn=_bow_embed, breaker_threshold=1)
    pin = _Pin()
    store._open_set_pin = pin

    def pinned():
        pin.arm(threading.get_ident())
        store._record_embed_failure()

    def partner():
        store.reset_embed_breaker()
        pin.release()

    a = threading.Thread(target=pinned)
    a.start()
    pin.wait_reached()
    b = threading.Thread(target=partner)
    b.start()
    a.join(10)
    b.join(10)
    store._open_set_pin = None
    _assert_breaker_consistent(store)


def test_probe_failure_overlapping_success_no_torn_breaker(tmp_path):
    """A ``warm_probe`` failure (sets failures=threshold then opens the breaker)
    overlapping an in-turn success must not leave the breaker open at count 0."""

    def boom(_text):
        raise RuntimeError("embed route down")

    store = _InstrumentedStore(str(tmp_path / "kb.db"), embed_fn=boom, breaker_threshold=2)
    pin = _Pin()
    store._open_set_pin = pin

    def pinned():  # the probe fails: failures=threshold, then opens the breaker (pinned)
        pin.arm(threading.get_ident())
        store._probe_once()

    def partner():  # a successful in-turn embed resets both fields
        store._record_embed_success()
        pin.release()

    a = threading.Thread(target=pinned)
    a.start()
    pin.wait_reached()
    b = threading.Thread(target=partner)
    b.start()
    a.join(10)
    b.join(10)
    store._open_set_pin = None
    _assert_breaker_consistent(store)


# ── AuditLogger session stats ─────────────────────────────────────────────────


class _HookDict(OrderedDict):
    """An ``_session_stats`` whose ``get`` can pin a chosen thread between the
    lookup and the dependent insert / ``move_to_end``."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.get_pin: _Pin | None = None

    def get(self, key, default=None):
        result = super().get(key, default)
        pin = self.get_pin
        if pin is not None:
            pin.hook()
        return result


class _HookSet(set):
    """A ``tools_used`` set that pins the reader after its first yielded element,
    so a concurrent ``add`` from ``log()`` lands mid-iteration."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.iter_pin: _Pin | None = None

    def __iter__(self):
        it = super().__iter__()
        pin = self.iter_pin
        first = True
        while True:
            try:
                x = next(it)
            except StopIteration:
                return
            yield x
            if first and pin is not None:
                pin.hook()  # the writer adds here; the next next() sees the change
            first = False


def _audit(tmp_path) -> AuditLogger:
    return AuditLogger(path=tmp_path / "audit.jsonl")


def _log(a: AuditLogger, session_id: str, tool: str, *, success: bool = True) -> None:
    a.log(session_id=session_id, tool=tool, args={}, result_summary="", duration_ms=1, success=success)


def test_two_first_logs_same_session_both_count(tmp_path):
    """Two first ``log()`` calls for the same new session — both must count. A
    lost insert leaves the session at one call instead of two."""
    a = _audit(tmp_path)
    hd = _HookDict()
    a._session_stats = hd
    pin = _Pin()
    hd.get_pin = pin  # pin the first logger after get()->None, before the insert

    def pinned():
        pin.arm(threading.get_ident())
        _log(a, "s", "ta")

    def partner():
        _log(a, "s", "tb")
        pin.release()

    t1 = threading.Thread(target=pinned)
    t1.start()
    pin.wait_reached()
    t2 = threading.Thread(target=partner)
    t2.start()
    t1.join(10)
    t2.join(10)
    hd.get_pin = None
    assert a.get_session_stats("s")["tool_calls"] == 2, "a concurrent first log() was lost"


def test_eviction_overlapping_log_no_keyerror(tmp_path, monkeypatch):
    """Evicting the oldest session while a ``log()`` for THAT session is in flight
    must not KeyError, and the dict must stay bounded."""
    from observability import audit

    monkeypatch.setattr(audit, "_MAX_SESSIONS", 2)
    a = _audit(tmp_path)
    hd = _HookDict()
    a._session_stats = hd
    _log(a, "old", "t")  # oldest, next to be evicted
    _log(a, "mid", "t")  # now at capacity (2)
    pin = _Pin()
    hd.get_pin = pin  # pin the "old" logger after get("old"), before move_to_end
    errors: list[Exception] = []

    def pinned():  # re-logs the oldest session
        pin.arm(threading.get_ident())
        try:
            _log(a, "old", "t2")
        except Exception as exc:  # noqa: BLE001 — the whole point is: this must not raise
            errors.append(exc)

    def partner():  # logs a NEW session -> pushes over the cap -> evicts the oldest
        try:
            _log(a, "new", "t")
        finally:
            pin.release()

    t1 = threading.Thread(target=pinned)
    t1.start()
    pin.wait_reached()
    t2 = threading.Thread(target=partner)
    t2.start()
    t1.join(10)
    t2.join(10)
    hd.get_pin = None
    assert errors == [], f"a concurrent eviction raised: {errors!r}"
    assert len(a._session_stats) <= 2, "session stats grew past the cap"


def test_get_session_stats_overlapping_log_consistent_snapshot(tmp_path):
    """``get_session_stats`` iterating ``tools_used`` while ``log()`` adds to it must
    not raise 'set changed size during iteration', and the snapshot it returns must
    be self-consistent (tool_calls == successes + failures)."""
    a = _audit(tmp_path)
    for tool in ("t0", "t1", "t2", "t3"):
        _log(a, "s", tool)
    hs = _HookSet(a._session_stats["s"]["tools_used"])
    a._session_stats["s"]["tools_used"] = hs
    pin = _Pin()
    hs.iter_pin = pin  # pin the reader mid-iteration
    snap: dict = {}
    errors: list[Exception] = []

    def reader():
        pin.arm(threading.get_ident())
        try:
            snap.update(a.get_session_stats("s"))
        except Exception as exc:  # noqa: BLE001 — must not raise under a concurrent add
            errors.append(exc)

    def writer():
        _log(a, "s", "t4", success=False)  # a NEW tool -> grows tools_used mid-iteration
        pin.release()

    t1 = threading.Thread(target=reader)
    t1.start()
    pin.wait_reached()
    t2 = threading.Thread(target=writer)
    t2.start()
    t1.join(10)
    t2.join(10)
    hs.iter_pin = None
    assert errors == [], f"get_session_stats raised during a concurrent log: {errors!r}"
    assert snap["tool_calls"] == snap["successes"] + snap["failures"], "snapshot not self-consistent"
