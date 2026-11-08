"""A short-lived, single-flight cache for provider model lists.

Opening Settings used to probe every connection live: ``GET /api/settings/schema``
asked the main provider for its models and then asked EVERY lane again for the slot
pickers — four external ``/models`` calls (1.2-1.9 s) per page load, and more under
load. When one of those probes failed transiently (a Codex ``/models`` call timing out
while eight Settings requests raced it) the lane came back with no models, and the
slot options silently lost all ten ``openai-codex:*`` entries for that one response.

This module holds the last answer per connection so that:

* a fresh answer (younger than :data:`TTL_S`) is served without a probe;
* concurrent callers for the same connection share ONE probe (single flight) instead
  of each making their own;
* a failed probe never erases a list that worked: the last good list is served,
  marked ``stale``, with the probe's error kept beside it, and the next retry waits
  :data:`RETRY_S` rather than hammering a provider that just failed;
* a failure with nothing to fall back on is remembered only briefly
  (:data:`NEGATIVE_TTL_S`), so a sign-in or a fixed endpoint shows up quickly.

The values are model ids only. A credential is never stored here: gateway keys are
reduced to a hash before they become part of a cache key.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field

#: How long a successful list is served without asking the provider again.
TTL_S = 300.0
#: After a failed refresh with a last good list in hand, wait this long before retrying.
RETRY_S = 30.0
#: How long a failure with no last good list is remembered (so concurrent and rapid
#: repeat callers don't each re-probe a provider that is down).
NEGATIVE_TTL_S = 15.0

# Indirection so tests can drive the clock.
_clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class ModelList:
    models: list[str]
    error: str = ""
    #: True when ``models`` is the last good list served because the latest probe failed.
    stale: bool = False


@dataclass
class _Entry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    good: list[str] | None = None  # last successful list (None = never succeeded)
    error: str = ""  # error from the latest probe ("" when it succeeded)
    next_probe_at: float = 0.0  # monotonic time after which a probe is due


_entries: dict[Hashable, _Entry] = {}
_entries_lock = threading.Lock()


def _entry(key: Hashable) -> _Entry:
    with _entries_lock:
        entry = _entries.get(key)
        if entry is None:
            entry = _entries[key] = _Entry()
        return entry


def _answer(entry: _Entry) -> ModelList:
    if entry.error and entry.good is not None:
        return ModelList(list(entry.good), entry.error, stale=True)
    if entry.error:
        return ModelList([], entry.error)
    return ModelList(list(entry.good or []))


def get(key: Hashable, fetch: Callable[[], tuple[list[str], str]], *, refresh: bool = False) -> ModelList:
    """The model list for ``key``, probing with ``fetch`` only when one is due.

    ``fetch`` returns ``(models, error)``; a non-empty ``error`` is a failed probe.
    ``refresh=True`` is an operator's explicit "get models": it always probes (still
    single-flighted) and returns that probe's own honest answer — a failure is reported,
    not papered over with the cached list — while a success refreshes the cache.
    """
    entry = _entry(key)
    with entry.lock:  # single flight: one probe per key; the rest wait and reuse it
        if not refresh and _clock() < entry.next_probe_at:
            return _answer(entry)
        try:
            models, error = fetch()
        except Exception as exc:  # noqa: BLE001 — a probe must never take the caller down
            models, error = [], str(exc) or type(exc).__name__
        models = [str(m) for m in (models or [])]
        now = _clock()
        if error:
            entry.error = error
            entry.next_probe_at = now + (RETRY_S if entry.good is not None else NEGATIVE_TTL_S)
            if refresh:
                return ModelList(models, error)
        else:
            entry.good, entry.error = models, ""
            entry.next_probe_at = now + TTL_S
        return _answer(entry)


def gateway_key(base: str, api_key: str, allow_env_key: bool) -> tuple:
    """Cache key for an OpenAI-compatible endpoint. The key is hashed, never stored."""
    digest = hashlib.sha256((api_key or "").encode()).hexdigest()[:16]
    return ("gateway", (base or "").strip().rstrip("/"), digest, bool(allow_env_key))


def native_key(provider: str) -> tuple:
    return ("native", (provider or "").strip().lower())


def invalidate(provider: str | None = None) -> None:
    """Forget cached lists — all of them, or one native provider's (on sign-in/out).

    The entry is dropped rather than reset: a probe already in flight finishes into the
    orphaned entry, so an answer that may belong to the previous account never reaches
    the next caller, who starts a fresh entry and probes."""
    with _entries_lock:
        if provider is None:
            _entries.clear()
        else:
            _entries.pop(native_key(provider), None)


def clear() -> None:
    """Drop every entry (tests)."""
    invalidate()
