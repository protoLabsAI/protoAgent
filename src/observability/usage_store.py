"""On-demand subscription-usage cache (ADR 0122 D1/D6).

One in-memory cache per process, keyed by ``provider_id``, over the tolerant
fetchers in :mod:`graph.providers.usage` (the U2a half). The contract is
deliberately small (ADR 0122 D1):

- **On-demand only.** Nothing is ever fetched in the background. A provider's
  fetcher runs only when a reader calls :meth:`UsageStore.get` (the ``usage.snapshot``
  op, the ``GET /api/usage`` route, or a future CLI/MCP projection) — constructing
  the store or booting the routes polls nothing.
- **Rate-limited polling.** A provider is re-polled at most once per
  :data:`_MIN_INTERVAL_S`; after a provider answers ``429`` (``retry_after`` set),
  it is left alone for :data:`_BACKOFF_S`, and ``force=True`` still honours that
  back-off (only the ordinary min-interval is bypassed).
- **Newest wins.** :meth:`UsageStore.merge_windows` folds in windows observed
  elsewhere (U3 captures them from live response headers); per window id the newer
  ``fetched_at`` wins.
- **Degrade, never raise.** A disconnected/unknown provider or a fetcher that
  raised reads as an ``unknown`` account, never an exception (ADR 0122 D4).
- **Last-known across restarts.** The last *good* snapshot per provider is
  persisted as JSON under the instance data dir, so a restart can show last-known
  data marked ``stale`` until a live poll refreshes it.
- **Live updates.** Every change publishes ``provider.usage`` over the plugin-host
  event bus (``HOST.publish``) when a server is running — the same best-effort seam
  ``graph/agent.py`` uses; ``None`` outside a server is a safe no-op.

The store is thread-safe; the fetchers block on the network, so readers call it off
the event loop.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# A fetcher is any ``() -> Account`` that never raises on its own (the U2a fetchers
# catch everything and return an ``unknown`` account); the store wraps it defensively
# all the same, so a fake/third-party fetcher that *does* raise still degrades.
Fetcher = Callable[[], Any]

# The native-OAuth providers ADR 0122 covers, in display order.
PROVIDER_IDS: tuple[str, ...] = ("anthropic-oauth", "openai-codex")

# Poll a provider at most once per minute; after a 429 leave it alone for five.
_MIN_INTERVAL_S = 60.0
_BACKOFF_S = 300.0

# How long a freshly fetched account stays "fresh" — mirrors the Account default in
# graph.providers.usage so a merged/last-known account ages the same way.
_STALE_AFTER_S = 60

_SNAPSHOT_FILE = "usage-snapshot.json"


@dataclass
class _Entry:
    """One provider's cached state. ``last_poll`` is a monotonic timestamp (``None``
    = never polled in this process, so the next read polls); ``stale`` marks data that
    came off disk and has not yet been confirmed by a live poll; ``window_fetched``
    tracks the ``fetched_at`` per window (keyed by :func:`_window_key`) for the
    newest-wins merge."""

    account: Any
    last_poll: float | None
    rate_limited: bool
    stale: bool
    window_fetched: dict[str, datetime] = field(default_factory=dict)
    fingerprint: str = ""


def _window_key(window: Any) -> str:
    """What a window measures, not the id its source gave it. The endpoint names
    Claude's session window ``session:0`` and the response headers name the same
    window ``5h:0``; keyed by id, a turn after a poll listed it twice."""
    scope = ",".join(f"{k}={v}" for k, v in sorted((window.scope or {}).items()))
    return f"{window.kind}|{window.window_seconds}|{scope}"


def _parse_dt(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class UsageStore:
    """In-memory, on-demand usage cache. Construct with the provider fetchers (real in
    production, fakes in tests) and an optional persistence ``path``; construction loads
    the last persisted snapshot but fetches nothing."""

    def __init__(
        self,
        fetchers: dict[str, Fetcher],
        *,
        path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._fetchers = dict(fetchers)
        self._path = Path(path) if path is not None else None
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        # Last *good* account dict per provider, exactly as persisted — an unknown poll
        # never overwrites it, so the on-disk snapshot keeps the last real data.
        self._last_good: dict[str, dict] = {}
        self._load()

    # ── public reads ────────────────────────────────────────────────────────────

    def get(self, provider_id: str, *, force: bool = False) -> dict:
        """The provider's account as a dict (``Account.to_dict()`` plus a ``stale``
        flag), polling the fetcher first iff a poll is due (and permitted by the 429
        back-off). Never raises — a failed/absent fetcher reads as ``unknown``."""
        with self._lock:
            entry, payload = self._refresh_locked(provider_id, force)
            data = self._present(provider_id, entry)
        if payload is not None:
            self._publish(payload)
        return data

    def cached(self, provider_id: str) -> dict | None:
        """The currently-cached account for ``provider_id`` **without polling** (``None``
        if nothing is cached). This is the last-known / stale read a restart shows before
        a live poll refreshes it."""
        with self._lock:
            entry = self._entries.get(provider_id)
            return self._present(provider_id, entry) if entry is not None else None

    # ── merge (U3 header capture) ────────────────────────────────────────────────

    def merge_windows(
        self, account: Any, windows: list, source: str, fetched_at: datetime
    ) -> None:
        """Fold ``windows`` observed for ``account`` into the cache; per window id the
        newer ``fetched_at`` wins (older observations are dropped). Publishes
        ``provider.usage`` if anything actually changed. Does not count as a poll."""
        payload = None
        with self._lock:
            payload = self._merge_locked(account, list(windows), source, fetched_at)
        if payload is not None:
            self._publish(payload)

    def _merge_locked(self, account, windows, source, fetched_at) -> dict | None:
        pid = account.provider_id
        prior = self._entries.get(pid)
        window_fetched = dict(prior.window_fetched) if prior else {}
        by_key = {_window_key(w): w for w in (prior.account.windows if prior else [])}
        changed = False
        for w in windows:
            key = _window_key(w)
            seen = window_fetched.get(key)
            if seen is None or fetched_at > seen:
                by_key[key] = w
                window_fetched[key] = fetched_at
                changed = True
        if not changed:
            return None
        base = prior.account if prior else account
        newest = max([fetched_at, *window_fetched.values()])
        merged = replace(
            base,
            provider_id=pid,
            provider_type=account.provider_type,
            account_key=account.account_key or getattr(base, "account_key", None),
            plan=account.plan if account.plan is not None else getattr(base, "plan", None),
            source=source,
            windows=list(by_key.values()),
            status="ok",
            error=None,
            retry_after=None,
            fetched_at=newest,
            stale_after=newest + timedelta(seconds=_STALE_AFTER_S),
        )
        entry = _Entry(
            account=merged,
            last_poll=prior.last_poll if prior else None,
            rate_limited=prior.rate_limited if prior else False,
            stale=False,
            window_fetched=window_fetched,
            fingerprint=self._fingerprint(merged.to_dict()),
        )
        self._entries[pid] = entry
        if self._record_good(merged):
            self._persist_locked()
        return self._present(pid, entry)

    # ── polling ───────────────────────────────────────────────────────────────────

    def _refresh_locked(self, provider_id: str, force: bool) -> tuple[_Entry | None, dict | None]:
        prior = self._entries.get(provider_id)
        if not self._should_poll(prior, force):
            return prior, None
        account = self._fetch_one(provider_id)
        now = self._clock()
        rate_limited = account.retry_after is not None
        if self._has_good_data(account):
            # A fresh, real poll: take the new account as-is.
            entry = _Entry(
                account=account,
                last_poll=now,
                rate_limited=rate_limited,
                stale=False,
                window_fetched={_window_key(w): account.fetched_at for w in account.windows},
                fingerprint=self._fingerprint(account.to_dict()),
            )
        elif prior is not None and self._has_good_data(prior.account):
            # The poll failed (429, network error, missing credentials) but we still hold
            # last-known-good windows — from an earlier poll or loaded off disk at restart.
            # Keep showing them (marked stale) rather than erasing them with an empty
            # ``unknown`` account, but record the poll time and arm the 429 back-off so a
            # single failure can't re-hammer the endpoint. This is what every reader —
            # ``get()``, the op, the route — sees, not just ``cached()``.
            entry = _Entry(
                account=prior.account,
                last_poll=now,
                rate_limited=rate_limited,
                stale=True,
                window_fetched=dict(prior.window_fetched),
                fingerprint=prior.fingerprint,
            )
        else:
            # Failed poll with no last-known-good data to fall back on → show unknown.
            entry = _Entry(
                account=account,
                last_poll=now,
                rate_limited=rate_limited,
                stale=False,
                window_fetched={_window_key(w): account.fetched_at for w in account.windows},
                fingerprint=self._fingerprint(account.to_dict()),
            )
        self._entries[provider_id] = entry
        # A fresh→stale or stale→fresh transition is a UI-visible change even when the
        # underlying windows match.
        changed = (
            prior is None
            or prior.fingerprint != entry.fingerprint
            or prior.stale != entry.stale
        )
        if self._record_good(account):
            self._persist_locked()
        return entry, (self._present(provider_id, entry) if changed else None)

    @staticmethod
    def _has_good_data(account) -> bool:
        """Whether ``account`` carries real, usable windows (as opposed to an ``unknown``/
        failed poll). Only such an account is worth persisting or falling back to."""
        return account.status == "ok" and bool(account.windows)

    def _should_poll(self, entry: _Entry | None, force: bool) -> bool:
        if entry is None or entry.last_poll is None:
            return True
        elapsed = self._clock() - entry.last_poll
        if entry.rate_limited:
            # The 429 back-off binds even under force (ADR 0122 D1).
            return elapsed >= _BACKOFF_S
        if force:
            return True
        return elapsed >= _MIN_INTERVAL_S

    def _fetch_one(self, provider_id: str):
        fetcher = self._fetchers.get(provider_id)
        if fetcher is None:
            return self._unknown_account(provider_id, "not connected")
        try:
            account = fetcher()
        except Exception:  # noqa: BLE001 — a fetcher must never take the surface down
            log.warning("%s usage fetch failed", provider_id, exc_info=False)
            return self._unknown_account(provider_id, "usage unavailable")
        return account if account is not None else self._unknown_account(provider_id, "usage unavailable")

    def _unknown_account(self, provider_id: str, note: str):
        from graph.providers.usage import Account

        account = Account(provider_id, provider_id)
        account.status, account.error = "unknown", note
        return account

    # ── presentation / staleness ───────────────────────────────────────────────────

    def _present(self, provider_id: str, entry: _Entry | None) -> dict:
        if entry is None:
            return self._unknown_account(provider_id, "not connected").to_dict() | {"stale": False}
        account = entry.account
        stale = entry.stale or self._wall_clock() >= account.stale_after
        data = account.to_dict()
        data["stale"] = stale
        if stale:
            # Past its freshness window an account reads as unknown; the last-known
            # windows are kept so the console can grey out the previous numbers.
            data["status"] = "unknown"
        return data

    @staticmethod
    def _fingerprint(data: dict) -> str:
        volatile = {"fetched_at", "stale_after", "stale"}
        return json.dumps({k: v for k, v in data.items() if k not in volatile}, sort_keys=True, default=str)

    # ── persistence ─────────────────────────────────────────────────────────────────

    def _record_good(self, account) -> bool:
        """Remember an account as last-known-good iff it carries real data; returns
        whether the persisted set changed. Unknown accounts never displace good data."""
        if not self._has_good_data(account):
            return False
        data = account.to_dict()
        if self._last_good.get(account.provider_id) == data:
            return False
        self._last_good[account.provider_id] = data
        return True

    def _persist_locked(self) -> None:
        if self._path is None:
            return
        from infra.paths import atomic_write

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._path, json.dumps({"accounts": list(self._last_good.values())}), mode=0o600)
        except OSError:
            log.warning("failed to persist usage snapshot", exc_info=False)

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            doc = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        accounts = doc.get("accounts") if isinstance(doc, dict) else None
        if not isinstance(accounts, list):
            return
        for data in accounts:
            if not isinstance(data, dict):
                continue
            try:
                account = self._account_from_dict(data)
            except Exception:  # noqa: BLE001 — a corrupt row is skipped, not fatal
                continue
            pid = account.provider_id
            self._last_good[pid] = account.to_dict()
            self._entries[pid] = _Entry(
                account=account,
                last_poll=None,  # never polled this process → next read polls
                rate_limited=False,
                stale=True,  # last-known off disk: show it, marked stale
                window_fetched={_window_key(w): account.fetched_at for w in account.windows},
                fingerprint=self._fingerprint(account.to_dict()),
            )

    @staticmethod
    def _account_from_dict(data: dict):
        from graph.providers.usage import Account, Window

        windows = []
        for w in data.get("windows") or []:
            windows.append(
                Window(
                    id=w["id"],
                    kind=w["kind"],
                    window_seconds=w.get("window_seconds"),
                    used_pct=w["used_pct"],
                    resets_at=_parse_dt(w["resets_at"]),
                    status=w["status"],
                    scope=w.get("scope"),
                    binding=w.get("binding", False),
                )
            )
        return Account(
            provider_id=data["provider_id"],
            provider_type=data.get("provider_type", data["provider_id"]),
            account_key=data.get("account_key"),
            plan=data.get("plan"),
            source=data.get("source", "endpoint"),
            fetched_at=_parse_dt(data["fetched_at"]),
            stale_after=_parse_dt(data["stale_after"]),
            windows=windows,
            credits=data.get("credits"),
            status=data.get("status", "unknown"),
            error=data.get("error"),
            retry_after=data.get("retry_after"),
        )

    # ── event bus ─────────────────────────────────────────────────────────────────

    def _publish(self, payload: dict) -> None:
        """Best-effort ``provider.usage`` emit over the plugin-host bus — the same seam
        and failure contract as ``graph/agent.py``; a missing/failing bus never raises."""
        try:
            from graph.plugins.host import HOST

            if HOST.publish is not None:
                HOST.publish("provider.usage", payload)
        except Exception:  # noqa: BLE001 — observability must never break a read
            log.exception("failed to publish provider.usage event")


# ── process-wide default store (what the op / route read) ───────────────────────────

_DEFAULT_STORE: UsageStore | None = None
_DEFAULT_LOCK = threading.Lock()


def default_fetchers() -> dict[str, Fetcher]:
    """The real native-OAuth fetchers (U2a). Imported lazily so the store module stays
    cheap to import and so nothing resolves credentials at import time."""
    from graph.providers.usage.anthropic import fetch_anthropic_usage
    from graph.providers.usage.codex import fetch_codex_usage

    return {"anthropic-oauth": fetch_anthropic_usage, "openai-codex": fetch_codex_usage}


def _default_path() -> Path:
    from infra.paths import instance_paths

    return instance_paths().store(_SNAPSHOT_FILE)


def default_store() -> UsageStore:
    """The lazily-built process store the op/route read. Building it loads the persisted
    snapshot but polls nothing (on-demand contract)."""
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = UsageStore(default_fetchers(), path=_default_path())
        return _DEFAULT_STORE


def set_default_store(store: UsageStore | None) -> None:
    """Install (or clear, with ``None``) the process store. Tests inject a fake-fetcher
    store here; the next :func:`default_store` call rebuilds the real one when cleared."""
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        _DEFAULT_STORE = store
