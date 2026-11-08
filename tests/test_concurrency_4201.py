"""Deterministic concurrency tests for the embed circuit breaker and the audit
session-stats (#4201).

Real OS threads reach both pieces of shared state at the same time:

* ``HybridKnowledgeStore`` breaker (``_embed_failures`` / ``_breaker_open_until``):
  searches run through ``asyncio.to_thread`` (graph/sdk.py:604-605,
  tools/memory_tools.py:725, operator_api/knowledge_routes.py:501,
  tools/lg_tools.py:380), ``warm_probe`` starts a daemon thread
  (hybrid_store.py:232, from server/agent_init.py:767), and
  ``reset_embed_breaker`` is called from operator_api/config_routes.py:91.
* ``AuditLogger._session_stats`` (the ``observability.audit.audit_logger``
  singleton): ``log()`` runs both on the event loop (AuditMiddleware async path,
  graph/middleware/audit.py:324/347) AND on worker threads — plugin install
  audits go through ``asyncio.to_thread(_install_deps_and_refresh, ...)``
  (operator_api/plugin_routes.py:393 → :248 → graph/plugins/installer.py:2053
  → :452). These use the shared session_id "plugins". Async middleware calls
  the same singleton directly on the event loop (graph/middleware/audit.py:324).

Each hook waits for an explicit event: either the partner completes (unlocked
code), or it attempts the already-held production lock (fixed code). Timeouts
only fail a broken handshake; they never choose the interleaving's outcome.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import pytest

from knowledge.hybrid_store import HybridKnowledgeStore
from observability.audit import AuditLogger

pytestmark = pytest.mark.platform_sensitive

_WAIT_TIMEOUT = 10


class _ObservedLock:
    """Delegate to the real lock and signal a proven contending acquisition."""

    def __init__(self, lock, pin):
        self.lock = lock
        self.pin = pin

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            self.pin.release()
            assert self.lock.acquire(timeout=_WAIT_TIMEOUT), "state lock deadlocked"
        return self

    def __exit__(self, *exc):
        self.lock.release()


class _Pin:
    """Pause one chosen thread until its partner completes or contends."""

    def __init__(self, owner):
        self._reached = threading.Event()
        self._go = threading.Event()
        self._ident: int | None = None
        self._fired = False
        name = "_breaker_lock" if isinstance(owner, HybridKnowledgeStore) else "_stats_lock"
        # Also runs against the pre-fix classes, which have no lock at all.
        lock = getattr(owner, name, None)
        if lock is not None:
            setattr(owner, name, _ObservedLock(lock, self))

    def arm(self, ident: int) -> None:
        self._ident = ident

    def wait_reached(self) -> None:
        assert self._reached.wait(_WAIT_TIMEOUT), "the pinned thread never reached its hook"

    def release(self) -> None:
        self._go.set()

    def hook(self) -> None:
        if self._fired or threading.get_ident() != self._ident:
            return
        self._fired = True
        self._reached.set()
        assert self._go.wait(_WAIT_TIMEOUT), "partner neither completed nor contended"


def _run_pair(pin, pinned, partner):
    """Propagate every worker exception and join both workers, even on failure."""
    errors = []

    def run(fn, release=False):
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if release:
                pin.release()

    first = threading.Thread(target=run, args=(pinned,), daemon=True)
    second = threading.Thread(target=run, args=(partner, True), daemon=True)
    first.start()
    try:
        pin.wait_reached()
        second.start()
        first.join(_WAIT_TIMEOUT)
        second.join(_WAIT_TIMEOUT)
    finally:
        pin.release()
        first.join(_WAIT_TIMEOUT)
        if second.ident is not None:
            second.join(_WAIT_TIMEOUT)
    assert not first.is_alive() and not second.is_alive(), "worker did not terminate"
    assert not errors, f"worker exceptions: {errors!r}"


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
    pin = _Pin(store)
    store._fail_set_pin = pin  # pin the first thread mid read-modify-write of += 1

    def pinned():
        pin.arm(threading.get_ident())
        store._record_embed_failure()

    def partner():
        store._record_embed_failure()
        pin.release()

    _run_pair(pin, pinned, partner)
    store._fail_set_pin = None
    assert store._embed_failures == 2, "a concurrent failure increment was lost"
    assert store._breaker_open(), "breaker did not open at the threshold"


def test_success_overlapping_failure_no_torn_breaker(tmp_path):
    """A success that resets the counter must not leave the breaker open — the
    failure opening the breaker and the success clearing it must not interleave
    into counter=0 + breaker-open."""
    store = _InstrumentedStore(str(tmp_path / "kb.db"), embed_fn=_bow_embed, breaker_threshold=1)
    pin = _Pin(store)
    store._open_set_pin = pin  # pin the failure thread as it opens the breaker

    def pinned():  # the failure: increments to threshold, then opens the breaker (pinned there)
        pin.arm(threading.get_ident())
        store._record_embed_failure()

    def partner():  # a success resets both fields meanwhile
        store._record_embed_success()
        pin.release()

    _run_pair(pin, pinned, partner)
    store._open_set_pin = None
    _assert_breaker_consistent(store)


def test_reset_overlapping_failure_no_torn_breaker(tmp_path):
    """``reset_embed_breaker`` (the operator "test connection" path) overlapping a
    failure must not leave a torn breaker either."""
    store = _InstrumentedStore(str(tmp_path / "kb.db"), embed_fn=_bow_embed, breaker_threshold=1)
    pin = _Pin(store)
    store._open_set_pin = pin

    def pinned():
        pin.arm(threading.get_ident())
        store._record_embed_failure()

    def partner():
        store.reset_embed_breaker()
        pin.release()

    _run_pair(pin, pinned, partner)
    store._open_set_pin = None
    _assert_breaker_consistent(store)


def test_probe_failure_overlapping_success_no_torn_breaker(tmp_path):
    """A ``warm_probe`` failure (sets failures=threshold then opens the breaker)
    overlapping an in-turn success must not leave the breaker open at count 0."""

    def boom(_text):
        raise RuntimeError("embed route down")

    store = _InstrumentedStore(str(tmp_path / "kb.db"), embed_fn=boom, breaker_threshold=2)
    pin = _Pin(store)
    store._open_set_pin = pin

    def pinned():  # the probe fails: failures=threshold, then opens the breaker (pinned)
        pin.arm(threading.get_ident())
        store._probe_once()

    def partner():  # a successful in-turn embed resets both fields
        store._record_embed_success()
        pin.release()

    _run_pair(pin, pinned, partner)
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
    pin = _Pin(a)
    hd.get_pin = pin  # pin the first logger after get()->None, before the insert

    def pinned():
        pin.arm(threading.get_ident())
        _log(a, "s", "ta")

    def partner():
        _log(a, "s", "tb")
        pin.release()

    _run_pair(pin, pinned, partner)
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
    pin = _Pin(a)
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

    _run_pair(pin, pinned, partner)
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
    pin = _Pin(a)
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

    _run_pair(pin, reader, writer)
    hs.iter_pin = None
    assert errors == [], f"get_session_stats raised during a concurrent log: {errors!r}"
    assert snap["tool_calls"] == snap["successes"] + snap["failures"], "snapshot not self-consistent"
    assert snap == {
        "tool_calls": 4, "successes": 4, "failures": 0,
        "total_ms": 4, "avg_ms": 1, "tools_used": ["t0", "t1", "t2", "t3"],
    }
    assert a.get_session_stats("s")["tool_calls"] == 5


@pytest.mark.parametrize("operation", ["_embed", "_embed_batch", "_probe_once"])
def test_embed_callbacks_run_outside_breaker_lock(tmp_path, operation):
    store = None

    def embed(_text):
        assert store._breaker_lock.acquire(blocking=False), "callback holds breaker lock"
        store._breaker_lock.release()
        return [1.0]

    store = HybridKnowledgeStore(
        str(tmp_path / "kb.db"), embed_fn=embed,
        embed_batch_fn=lambda texts: [embed(text) for text in texts],
    )
    if operation == "_probe_once":
        assert store._probe_once() is True
    elif operation == "_embed_batch":
        assert store._embed_batch(["a"]) == [[1.0]]
    else:
        assert store._embed("a") == [1.0]


def test_audit_file_write_runs_outside_stats_lock(tmp_path, monkeypatch):
    from pathlib import Path

    logger = _audit(tmp_path)
    original_open = Path.open
    writes = []

    def checked_open(path, mode="r", *args, **kwargs):
        if path == logger.path and mode == "a":
            assert logger._stats_lock.acquire(blocking=False), "file write holds stats lock"
            logger._stats_lock.release()
            writes.append(path)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    _log(logger, "s", "tool")
    assert writes == [logger.path]
    assert logger.get_session_stats("s")["tool_calls"] == 1
