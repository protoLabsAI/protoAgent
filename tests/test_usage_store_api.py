"""Usage cache + usage.snapshot op + GET /api/usage (ADR 0122 D1/D4/D6, #4431 part 2).

Fake fetchers, no network: the store polls only when a reader calls, honours the min-interval
and the 429 back-off, merges windows newest-first, persists/reloads last-known-good marked
stale, publishes on change (and no-ops when the bus is absent), and the route always answers
200 with ``unknown`` entries — never a 500 — for a disconnected or raising provider.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.providers.usage import Account, Window
from observability import usage_store
from observability.usage_store import UsageStore
from operator_api.usage_routes import register_usage_routes

RESET = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """A controllable monotonic clock for the poll gate (wall-clock staleness stays real)."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def ok_account(provider_id: str, used: float = 10.0) -> Account:
    acct = Account(provider_id, provider_id, account_key="acct-1")
    acct.status = "ok"
    acct.windows = [Window(id=f"{provider_id}:0", kind="session", window_seconds=18000,
                           used_pct=used, resets_at=RESET, status="ok")]
    return acct


def rate_limited_account(provider_id: str) -> Account:
    acct = Account(provider_id, provider_id, account_key="acct-1")
    acct.status, acct.error, acct.retry_after = "unknown", "rate limited", 300
    return acct


def ok_fetchers(used: float = 10.0) -> dict:
    """A fetcher per native provider that returns that provider's own ``ok`` account."""
    return {pid: (lambda p=pid: ok_account(p, used)) for pid in usage_store.PROVIDER_IDS}


@pytest.fixture(autouse=True)
def _reset_default_store():
    yield
    usage_store.set_default_store(None)


# ── r1: min-interval + 429 back-off ─────────────────────────────────────────────────


def test_two_reads_within_60s_trigger_one_fetch(tmp_path):
    clock = FakeClock()
    calls = {"n": 0}

    def fetcher():
        calls["n"] += 1
        return ok_account("anthropic-oauth")

    store = UsageStore({"anthropic-oauth": fetcher}, path=tmp_path / "u.json", clock=clock)
    store.get("anthropic-oauth")
    store.get("anthropic-oauth")  # still inside the 60 s window → served from cache
    assert calls["n"] == 1

    clock.advance(61)
    store.get("anthropic-oauth")  # min-interval elapsed → one more fetch
    assert calls["n"] == 2

    store.get("anthropic-oauth", force=True)  # force bypasses the ordinary min-interval
    assert calls["n"] == 3


def test_force_still_respects_429_backoff(tmp_path):
    clock = FakeClock()
    calls = {"n": 0}

    def fetcher():
        calls["n"] += 1
        return rate_limited_account("openai-codex")

    store = UsageStore({"openai-codex": fetcher}, path=tmp_path / "u.json", clock=clock)
    store.get("openai-codex")  # 429 → back-off armed
    assert calls["n"] == 1

    clock.advance(61)  # past the 60 s min-interval but inside the 300 s back-off
    store.get("openai-codex", force=True)
    assert calls["n"] == 1  # force does NOT override the 429 back-off

    clock.advance(240)  # now 301 s since the poll → back-off cleared
    store.get("openai-codex", force=True)
    assert calls["n"] == 2


# ── r2: nothing fetched unless a reader calls ───────────────────────────────────────


def test_constructing_store_and_booting_routes_fetches_nothing(tmp_path):
    calls = {"n": 0}

    def counting(provider_id):
        def _fetch():
            calls["n"] += 1
            return ok_account(provider_id)

        return _fetch

    store = UsageStore(
        {pid: counting(pid) for pid in usage_store.PROVIDER_IDS}, path=tmp_path / "u.json"
    )
    usage_store.set_default_store(store)

    app = FastAPI()
    register_usage_routes(app)
    TestClient(app)  # boot the client, but issue no request

    assert calls["n"] == 0


# ── r3: route is 200 + unknown, never 500 ───────────────────────────────────────────


def test_route_200_unknown_on_raising_fetcher(tmp_path):
    def boom():
        raise RuntimeError("secret-transport-detail")

    store = UsageStore(
        {pid: boom for pid in usage_store.PROVIDER_IDS}, path=tmp_path / "u.json"
    )
    usage_store.set_default_store(store)

    app = FastAPI()
    register_usage_routes(app)
    resp = TestClient(app).get("/api/usage")

    assert resp.status_code == 200
    body = resp.json()
    assert {a["provider_id"] for a in body["accounts"]} == set(usage_store.PROVIDER_IDS)
    assert all(a["status"] == "unknown" for a in body["accounts"])
    assert "secret-transport-detail" not in resp.text  # error notes never leak


def test_route_200_unknown_for_disconnected_provider(tmp_path):
    store = UsageStore({}, path=tmp_path / "u.json")  # no fetchers wired = nothing connected
    usage_store.set_default_store(store)

    app = FastAPI()
    register_usage_routes(app)
    resp = TestClient(app).get("/api/usage")

    assert resp.status_code == 200
    body = resp.json()
    assert {a["provider_id"] for a in body["accounts"]} == set(usage_store.PROVIDER_IDS)
    assert all(a["status"] == "unknown" for a in body["accounts"])


def test_route_account_filter_and_refresh(tmp_path):
    calls = {"n": 0}

    def fetcher():
        calls["n"] += 1
        return ok_account("anthropic-oauth")

    store = UsageStore({"anthropic-oauth": fetcher}, path=tmp_path / "u.json")
    usage_store.set_default_store(store)

    app = FastAPI()
    register_usage_routes(app)
    client = TestClient(app)

    body = client.get("/api/usage?account=anthropic-oauth").json()
    assert [a["provider_id"] for a in body["accounts"]] == ["anthropic-oauth"]
    assert body["accounts"][0]["status"] == "ok"
    assert isinstance(body["fetched_at"], str)
    assert calls["n"] == 1

    client.get("/api/usage?account=anthropic-oauth&refresh=true")  # force a re-poll
    assert calls["n"] == 2


# ── r4: merge newest-wins + persisted reload is stale ───────────────────────────────


def test_merge_windows_keeps_newer_fetched_at_per_window(tmp_path):
    store = UsageStore({}, path=tmp_path / "u.json")
    acct = Account("anthropic-oauth", "anthropic-oauth", account_key="acct-1")

    def win(used, status="ok"):
        return Window(id="5h:0", kind="session", window_seconds=18000,
                      used_pct=used, resets_at=RESET, status=status)

    t0 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    t1 = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 22, 11, 0, tzinfo=UTC)

    store.merge_windows(acct, [win(10)], "headers", t1)
    assert store.cached("anthropic-oauth")["windows"][0]["used_pct"] == 10

    store.merge_windows(acct, [win(90, "warning")], "headers", t2)  # newer → wins
    assert store.cached("anthropic-oauth")["windows"][0]["used_pct"] == 90

    store.merge_windows(acct, [win(5)], "headers", t0)  # older → ignored
    assert store.cached("anthropic-oauth")["windows"][0]["used_pct"] == 90


def test_persisted_snapshot_reloads_as_stale(tmp_path):
    path = tmp_path / "u.json"
    calls = {"n": 0}

    def fetcher():
        calls["n"] += 1
        return ok_account("anthropic-oauth", used=42.0)

    store = UsageStore({"anthropic-oauth": fetcher}, path=path)
    store.get("anthropic-oauth")  # polls once, persists the good snapshot
    assert path.exists()
    assert calls["n"] == 1

    # "Restart": a fresh store over the same file loads last-known data but fetches nothing.
    reload_calls = {"n": 0}

    def fetcher2():
        reload_calls["n"] += 1
        return ok_account("anthropic-oauth")

    store2 = UsageStore({"anthropic-oauth": fetcher2}, path=path)
    cached = store2.cached("anthropic-oauth")
    assert cached is not None
    assert cached["stale"] is True
    assert cached["status"] == "unknown"  # past its freshness window → reads unknown
    assert cached["windows"][0]["used_pct"] == 42.0  # last-known data still shown
    assert reload_calls["n"] == 0


# ── failed poll must not erase last-known windows (the get() path readers use) ───────


def test_failed_poll_keeps_last_known_windows_via_get(tmp_path):
    """A 429 after good data degrades to ``unknown``/stale but must keep the last-known
    windows reachable through ``get()`` (not just ``cached()``), and must not re-poll for
    the whole back-off — one 429 cannot erase the windows a process already held."""
    clock = FakeClock()
    calls = {"n": 0}
    state = {"mode": "ok"}

    def fetcher():
        calls["n"] += 1
        if state["mode"] == "ok":
            return ok_account("anthropic-oauth", used=42.0)
        return rate_limited_account("anthropic-oauth")

    store = UsageStore({"anthropic-oauth": fetcher}, path=tmp_path / "u.json", clock=clock)

    first = store.get("anthropic-oauth")
    assert first["status"] == "ok"
    assert first["windows"][0]["used_pct"] == 42.0
    assert calls["n"] == 1

    state["mode"] = "429"
    clock.advance(61)  # min-interval elapsed → the next read polls and hits the 429
    degraded = store.get("anthropic-oauth")
    assert calls["n"] == 2
    assert degraded["status"] == "unknown"  # degraded...
    assert degraded["stale"] is True
    assert degraded["windows"][0]["used_pct"] == 42.0  # ...but the windows survive the 429

    clock.advance(61)  # past the min-interval but still inside the 300 s back-off
    held = store.get("anthropic-oauth", force=True)  # back-off binds even under force
    assert calls["n"] == 2  # no extra poll
    assert held["status"] == "unknown"
    assert held["windows"][0]["used_pct"] == 42.0  # last-known windows still shown


def test_restart_last_known_survives_failing_first_poll_via_get(tmp_path):
    """After a restart the first ``get()`` polls (``_load`` leaves ``last_poll=None``); if
    that poll fails, the reader must still see the disk-loaded last-known windows, not an
    empty ``unknown`` account."""
    path = tmp_path / "u.json"
    good = UsageStore(
        {"anthropic-oauth": lambda: ok_account("anthropic-oauth", used=73.0)}, path=path
    )
    good.get("anthropic-oauth")  # persist a good snapshot
    assert path.exists()

    def boom():
        raise RuntimeError("no creds")

    store2 = UsageStore({"anthropic-oauth": boom}, path=path)  # "restart", provider now fails
    out = store2.get("anthropic-oauth")  # the only method the op/route call
    assert out["stale"] is True
    assert out["status"] == "unknown"
    assert out["windows"][0]["used_pct"] == 73.0  # last-known data survives the failing poll


# ── r5: publish on change, no-op when the bus is absent ─────────────────────────────


def test_publish_fires_on_change_and_is_none_safe(tmp_path, monkeypatch):
    from graph.plugins import host

    events = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, payload: events.append((topic, payload)))

    clock = FakeClock()
    state = {"used": 10.0}

    def fetcher():
        return ok_account("anthropic-oauth", used=state["used"])

    store = UsageStore({"anthropic-oauth": fetcher}, path=tmp_path / "u.json", clock=clock)

    store.get("anthropic-oauth")  # first data → publish
    assert len(events) == 1
    assert events[0][0] == "provider.usage"
    assert events[0][1]["provider_id"] == "anthropic-oauth"

    clock.advance(1)
    store.get("anthropic-oauth", force=True)  # identical data → no new publish
    assert len(events) == 1

    state["used"] = 95.0
    clock.advance(1)
    store.get("anthropic-oauth", force=True)  # changed data → publish again
    assert len(events) == 2

    monkeypatch.setattr(host.HOST, "publish", None)  # bus absent (CLI / no server)
    state["used"] = 50.0
    clock.advance(1)
    store.get("anthropic-oauth", force=True)  # must not raise
    assert len(events) == 2


def test_merge_publishes_on_change(tmp_path, monkeypatch):
    from graph.plugins import host

    events = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, payload: events.append((topic, payload)))

    store = UsageStore({}, path=tmp_path / "u.json")
    acct = Account("openai-codex", "openai-codex", account_key="acct-1")
    win = Window(id="rate:primary", kind="session", window_seconds=18000,
                 used_pct=20.0, resets_at=RESET, status="ok")
    t = datetime(2026, 10, 22, 11, 0, tzinfo=UTC)

    store.merge_windows(acct, [win], "headers", t)
    assert [e[0] for e in events] == ["provider.usage"]

    # Re-merging an equal-or-older observation changes nothing → no new publish.
    store.merge_windows(acct, [win], "headers", t)
    assert len(events) == 1


# ── r6: registry ────────────────────────────────────────────────────────────────────


def test_usage_snapshot_registered_read_only():
    from ops import load_all

    specs = load_all()
    assert "usage.snapshot" in specs
    assert specs["usage.snapshot"].mutates is False
    assert specs["usage.snapshot"].summary


def test_op_snapshot_returns_every_provider(tmp_path):
    store = UsageStore(ok_fetchers(), path=tmp_path / "u.json")
    usage_store.set_default_store(store)

    from ops import OpContext
    from ops.usage import snapshot

    out = snapshot(OpContext())
    assert isinstance(out["fetched_at"], str)
    assert {a["provider_id"] for a in out["accounts"]} == set(usage_store.PROVIDER_IDS)
    assert all(a["status"] == "ok" for a in out["accounts"])


def test_headers_after_a_poll_update_the_same_windows_not_add_copies(tmp_path):
    # The endpoint names Claude's windows session:0 / weekly_all:1 / weekly_scoped:2 and the
    # response headers name the same session and weekly windows 5h:0 / 7d:1. Keyed by id, the
    # first turn after a poll listed session and weekly twice in Settings ▸ Providers.
    from graph.providers.usage.anthropic import parse_anthropic_usage
    from graph.providers.usage.headers import parse_anthropic_headers
    from tests.test_usage_fetchers import CLAUDE_LIVE

    polled = parse_anthropic_usage(CLAUDE_LIVE)
    store = UsageStore({"anthropic-oauth": lambda: polled}, path=tmp_path / "u.json", clock=FakeClock())
    store.get("anthropic-oauth")
    headers = {
        "anthropic-ratelimit-unified-5h-utilization": "0.25",
        "anthropic-ratelimit-unified-5h-reset": "1792756800",
        "anthropic-ratelimit-unified-7d-utilization": "0.81",
        "anthropic-ratelimit-unified-7d-reset": "1792756800",
    }
    later = datetime(2099, 1, 1, tzinfo=UTC)
    store.merge_windows(Account("anthropic-oauth", "anthropic-oauth"), parse_anthropic_headers(headers), "headers", later)

    windows = store.cached("anthropic-oauth")["windows"]
    assert len(windows) == len(polled.windows)
    unscoped = {w["kind"]: w["used_pct"] for w in windows if not w.get("scope")}
    assert unscoped == {"session": 25.0, "weekly": 81.0}  # the newer header reading won
    assert [w for w in windows if w.get("scope")]  # the scoped weekly window is kept
