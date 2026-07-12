"""Surface hot-reload reconcile (ADR 0018).

A config reload used to only fire each running surface's `reload(cfg)` callback, so a
newly-ENABLED plugin's surface never started and a DISABLED plugin's surface kept
running (a leak) until a full restart — asymmetric with routers, which hot-mount.
`_reload_plugin_surfaces` now reconciles: stop removed, hot-start newly-enabled, reload
survivors. These tests cover the pure diff, the pre-startup guard, and the on-loop
start/stop behavior.
"""

from __future__ import annotations

import asyncio

import pytest

import server.agent_init as ai
from runtime.state import STATE


def _handle(plugin_id, name, *, stop=None, reload=None, handle=None):
    return {"plugin_id": plugin_id, "name": name, "stop": stop, "reload": reload, "handle": handle}


def _spec(plugin_id, name, *, start=None, stop=None, reload=None):
    return {"plugin_id": plugin_id, "name": name, "start": start, "stop": stop, "reload": reload}


def test_plan_reconcile_buckets_stop_start_and_reload():
    running = [_handle("discord", "discord-gateway"), _handle("google", "google-gateway")]
    # google survives, discord is gone, telegram is new.
    wanted = [_spec("google", "google-gateway"), _spec("telegram", "telegram-gateway")]

    to_stop, to_start, to_reload = ai._plan_surface_reconcile(running, wanted)

    assert [h["plugin_id"] for h in to_stop] == ["discord"]      # no longer wanted → stop
    assert [s["plugin_id"] for s in to_start] == ["telegram"]    # newly wanted → start
    assert [h["plugin_id"] for h in to_reload] == ["google"]     # in both → reload cb


def test_plan_reconcile_keys_on_plugin_id_and_name():
    # Two plugins can share a surface NAME — the key must include plugin_id so one
    # isn't mistaken for the other (which would wrongly stop/keep the wrong surface).
    running = [_handle("a", "gateway")]
    wanted = [_spec("b", "gateway")]

    to_stop, to_start, to_reload = ai._plan_surface_reconcile(running, wanted)

    assert [h["plugin_id"] for h in to_stop] == ["a"]
    assert [s["plugin_id"] for s in to_start] == ["b"]
    assert to_reload == []


def test_reload_is_a_noop_before_startup_started_surfaces(monkeypatch):
    # Guard: before the startup hook's surface loop runs, a reload must NOT hot-start —
    # the pending startup would then start them a second time.
    scheduled: list = []
    monkeypatch.setattr(ai, "_run_on_server_loop", lambda make, what: scheduled.append(what))
    monkeypatch.setattr(STATE, "plugin_surfaces_started", False, raising=False)
    monkeypatch.setattr(STATE, "plugin_surfaces", [_spec("telegram", "telegram-gateway")], raising=False)
    monkeypatch.setattr(STATE, "plugin_surface_handles", [], raising=False)

    ai._reload_plugin_surfaces(object())

    assert scheduled == []  # nothing scheduled on the loop


@pytest.mark.asyncio
async def test_reload_hot_starts_new_and_stops_removed_on_the_loop(monkeypatch):
    # End-to-end on a real running loop: a reload starts a newly-enabled surface and
    # stops a removed one, leaving a survivor in place.
    started: list = []
    stopped: list = []

    async def _tg_start():
        started.append("telegram")
        return "tg-task"

    async def _discord_stop():
        stopped.append("discord")

    reloaded: list = []

    async def _google_reload(cfg):
        reloaded.append("google")

    # Running: discord (to be removed) + google (survivor, has a reload cb).
    handles = [
        _handle("discord", "discord-gateway", stop=_discord_stop, handle="dc-task"),
        _handle("google", "google-gateway", reload=_google_reload, handle="gg-task"),
    ]
    # Wanted after reload: google survives, telegram is new, discord is gone.
    wanted = [
        _spec("google", "google-gateway", reload=_google_reload),
        _spec("telegram", "telegram-gateway", start=_tg_start),
    ]
    monkeypatch.setattr(STATE, "plugin_surfaces_started", True, raising=False)
    monkeypatch.setattr(STATE, "plugin_surface_handles", handles, raising=False)
    monkeypatch.setattr(STATE, "plugin_surfaces", wanted, raising=False)

    ai._reload_plugin_surfaces(object())
    # _run_on_server_loop scheduled the reconcile coroutine on THIS loop; let it drain.
    for _ in range(5):
        await asyncio.sleep(0)

    assert started == ["telegram"]          # newly-enabled surface hot-started
    assert stopped == ["discord"]           # removed surface stopped
    assert reloaded == ["google"]           # survivor got its reload callback

    keys = {(h["plugin_id"], h["name"]) for h in STATE.plugin_surface_handles}
    assert ("discord", "discord-gateway") not in keys   # dropped
    assert ("google", "google-gateway") in keys         # kept (still running)
    assert ("telegram", "telegram-gateway") in keys     # added
