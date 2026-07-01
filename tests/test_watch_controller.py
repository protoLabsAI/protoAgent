"""WatchController — create / trust-gate / evaluate / tick, and the many-concurrent-watches
property that motivated the primitive (ADR 0067)."""

import pytest

from graph.config import LangGraphConfig
from graph.watches.controller import WatchController
from graph.watches.store import WatchStore


def _ctrl(tmp_path, **overrides):
    cfg = LangGraphConfig(**overrides)
    return WatchController(cfg, WatchStore(tmp_path))


# --- create + trust gate ---------------------------------------------------


def test_create_plugin_verifier_untrusted_ok(tmp_path):
    c = _ctrl(tmp_path)
    ok, msg, w = c.create(condition="reach 1M", verifier={"type": "plugin", "check": "p:v"})
    assert ok and w is not None and "Watch created" in msg
    assert w.verifier["type"] == "plugin"


def test_create_untrusted_rejects_dangerous_verifiers(tmp_path):
    c = _ctrl(tmp_path)
    for spec in ({"type": "command", "command": "x"}, {"type": "test"}, {"type": "data", "path": "/x", "expr": "1"}):
        ok, msg, w = c.create(condition="c", verifier=spec)
        assert not ok and w is None and "operator-only" in msg


def test_create_trusted_accepts_dangerous_verifier(tmp_path):
    # The operator channel (trusted=True, gated to operator-tier by the ADR 0066 /api ceiling)
    # accepts any verifier type.
    c = _ctrl(tmp_path)
    ok, _msg, w = c.create(condition="tests pass", verifier={"type": "command", "command": "true"}, trusted=True)
    assert ok and w.verifier["type"] == "command"


def test_create_rejects_unknown_verifier(tmp_path):
    c = _ctrl(tmp_path)
    ok, msg, w = c.create(condition="c", verifier={"type": "bogus"}, trusted=True)
    assert not ok and "unknown verifier type" in msg


def test_create_requires_condition(tmp_path):
    c = _ctrl(tmp_path)
    ok, msg, _w = c.create(condition="", verifier={"type": "plugin", "check": "p:v"})
    assert not ok and "condition is required" in msg


# --- MANY concurrent watches (the whole point of the primitive) ------------


def test_many_concurrent_watches(tmp_path):
    c = _ctrl(tmp_path)
    for cond in ("watch deploy", "watch CI", "watch treasury", "watch backlog"):
        ok, _m, _w = c.create(condition=cond, verifier={"type": "plugin", "check": "p:v"})
        assert ok
    watches = c.list_watches()
    assert len(watches) == 4  # a goal could only hold ONE per session — a watch holds many
    assert {w.condition for w in watches} == {"watch deploy", "watch CI", "watch treasury", "watch backlog"}
    assert len({w.id for w in watches}) == 4  # distinct ids


def test_same_condition_idempotent_explicit_id_opts_in(tmp_path):
    c = _ctrl(tmp_path)
    c.create(condition="watch deploy", verifier={"type": "plugin", "check": "p:v"})
    c.create(condition="watch deploy", verifier={"type": "plugin", "check": "p:v"})  # same condition → same id
    assert len(c.list_watches()) == 1
    c.create(condition="watch deploy", verifier={"type": "plugin", "check": "p:v"}, watch_id="deploy-2")
    assert len(c.list_watches()) == 2  # explicit id → a second, distinct watch


# --- evaluate: met / not-met / expired / stall / tick ----------------------


@pytest.mark.asyncio
async def test_evaluate_met_finishes(tmp_path):
    c = _ctrl(tmp_path)
    _ok, _m, w = c.create(condition="done", verifier={"type": "command", "command": "exit 0"}, trusted=True)
    assert await c.evaluate(w.id) == "met"
    assert c.store.get(w.id).status == "met"


@pytest.mark.asyncio
async def test_evaluate_not_met_stays_active(tmp_path):
    c = _ctrl(tmp_path)
    _ok, _m, w = c.create(condition="pending", verifier={"type": "command", "command": "exit 1"}, trusted=True)
    assert await c.evaluate(w.id) is None
    assert c.store.get(w.id).active


@pytest.mark.asyncio
async def test_evaluate_deadline_expires(tmp_path):
    c = _ctrl(tmp_path)
    _ok, _m, w = c.create(
        condition="deploy", verifier={"type": "command", "command": "exit 1"}, deadline=1.0, trusted=True
    )  # deadline epoch 1.0 = long past
    assert await c.evaluate(w.id) == "expired"


@pytest.mark.asyncio
async def test_stall_fires_on_stalled_without_ending(tmp_path):
    from graph.watches import hooks as watch_hooks

    fired: list[str] = []
    watch_hooks.set_watch_hooks([{"plugin_id": "t", "on_stalled": lambda w: fired.append(w.id)}])
    try:
        c = _ctrl(tmp_path)
        _ok, _m, w = c.create(
            condition="stuck", verifier={"type": "command", "command": "exit 1"}, stall_after=2, trusted=True
        )
        await c.evaluate(w.id)  # baseline (streak 0)
        await c.evaluate(w.id)  # streak 1
        assert fired == []
        await c.evaluate(w.id)  # streak 2 → fire once
        assert fired == [w.id]
        assert c.store.get(w.id).active  # signal, NOT terminal
        await c.evaluate(w.id)  # streak 3 → no re-fire
        assert fired == [w.id]
    finally:
        watch_hooks.set_watch_hooks([])


@pytest.mark.asyncio
async def test_tick_all_counts_terminal(tmp_path):
    c = _ctrl(tmp_path)
    c.create(condition="a", verifier={"type": "command", "command": "exit 0"}, trusted=True)  # met
    c.create(condition="b", verifier={"type": "command", "command": "exit 1"}, trusted=True)  # active
    assert await c.tick_all() == 1


# --- the run_in_session reaction on met (the supervision payoff) ------------


@pytest.mark.asyncio
async def test_met_enqueues_followup_turn_in_session(tmp_path, monkeypatch):
    from runtime.state import STATE

    added: list[dict] = []

    class _Sched:
        def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None):
            added.append({"prompt": prompt, "context_id": context_id, "job_id": job_id})

            class _J:
                id = job_id or "j"

            return _J()

        def cancel_job(self, job_id):
            return True

    monkeypatch.setattr(STATE, "scheduler", _Sched())
    c = _ctrl(tmp_path)
    _ok, _m, w = c.create(
        condition="deploy done",
        verifier={"type": "command", "command": "exit 0"},
        run_prompt="Run the smoke test.",
        run_session="sess-7",
        trusted=True,
    )
    assert await c.evaluate(w.id) == "met"
    assert added and added[0]["context_id"] == "sess-7"  # follow-up turn fired into the target session
    assert added[0]["prompt"] == "Run the smoke test."


def test_clear(tmp_path):
    c = _ctrl(tmp_path)
    _ok, _m, w = c.create(condition="c", verifier={"type": "plugin", "check": "p:v"})
    assert c.clear(w.id) is True
    assert c.clear(w.id) is False


def test_store_resolves_base_without_args(tmp_path, monkeypatch):
    # Exercises the REAL _resolve_base (the infra.paths API) — the path a base_dir-injecting
    # test skips, and that live-smoke caught (a stale scope_leaf import). WATCH_PATH keeps it
    # off the shared instance dir.
    monkeypatch.setenv("WATCH_PATH", str(tmp_path / "w"))
    s = WatchStore()
    assert s._base == (tmp_path / "w")


def test_safe_name_no_collision_across_sanitized_ids():
    # CodeRabbit #1505: distinct raw ids that sanitize to the same string used to collide on
    # one file. Now they get distinct filenames; an already-safe id stays human-readable.
    from graph.watches.store import _safe_name

    assert _safe_name("abc/def") != _safe_name("abc_def")
    assert _safe_name("a/b") != _safe_name("a\\b")
    assert _safe_name("deploy-1a2b3c") == "deploy-1a2b3c"


@pytest.mark.asyncio
async def test_concurrent_evaluate_finishes_once(tmp_path, monkeypatch):
    # CodeRabbit #1505: the per-watch lock serializes tick_all vs evaluate_now on the SAME
    # watch, so a met watch finishes (and fires its run_in_session reaction) exactly once.
    import asyncio

    from runtime.state import STATE

    calls: list[str] = []

    class _Sched:
        def add_job(self, prompt, schedule, *, job_id=None, timezone=None, context_id=None):
            calls.append(job_id)

            class _J:
                id = job_id or "j"

            return _J()

        def cancel_job(self, job_id):
            return True

    monkeypatch.setattr(STATE, "scheduler", _Sched())
    c = _ctrl(tmp_path)
    _ok, _m, w = c.create(
        condition="done",
        verifier={"type": "command", "command": "exit 0"},
        run_prompt="go",
        run_session="s",
        trusted=True,
    )
    await asyncio.gather(c.evaluate(w.id), c.evaluate(w.id))
    assert calls.count(f"watch-{w.id}") == 1  # not 2 — the lock prevented a double-finish
