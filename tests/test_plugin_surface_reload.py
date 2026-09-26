"""Surface hot-reload reconcile (ADR 0018).

A config reload used to only fire each running surface's `reload(cfg)` callback, so a
newly-ENABLED plugin's surface never started and a DISABLED plugin's surface kept
running (a leak) until a full restart — asymmetric with routers, which hot-mount.
`_reload_plugin_surfaces` now reconciles: stop removed, hot-start newly-enabled, reload
survivors — and restart a survivor orphaned from the re-run ``register()`` (#3593). These
tests cover the pure diff, the pre-startup guard, and the on-loop
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

    to_stop, to_start, to_reload, to_restart = ai._plan_surface_reconcile(running, wanted)

    assert to_restart == []
    assert [h["plugin_id"] for h in to_stop] == ["discord"]      # no longer wanted → stop
    assert [s["plugin_id"] for s in to_start] == ["telegram"]    # newly wanted → start
    assert [h["plugin_id"] for h in to_reload] == ["google"]     # in both → reload cb


def test_plan_reconcile_keys_on_plugin_id_and_name():
    # Two plugins can share a surface NAME — the key must include plugin_id so one
    # isn't mistaken for the other (which would wrongly stop/keep the wrong surface).
    running = [_handle("a", "gateway")]
    wanted = [_spec("b", "gateway")]

    to_stop, to_start, to_reload, to_restart = ai._plan_surface_reconcile(running, wanted)

    assert to_restart == []
    assert [h["plugin_id"] for h in to_stop] == ["a"]
    assert [s["plugin_id"] for s in to_start] == ["b"]
    assert to_reload == []


def test_plan_restarts_a_survivor_whose_register_minted_a_new_stop():
    # #3593: no reload hook + a different stop ⇒ the running surface belongs to the
    # previous registration's objects. It must be restarted from the new spec.
    old_stop, new_stop = (lambda: None), (lambda: None)
    running = [_handle("pr-reviewer", "sweep", stop=old_stop)]
    wanted = [_spec("pr-reviewer", "sweep", stop=new_stop)]

    to_stop, to_start, to_reload, to_restart = ai._plan_surface_reconcile(running, wanted)

    assert (to_stop, to_start, to_reload) == ([], [], [])
    assert to_restart == [(running[0], wanted[0])]


def test_plan_keeps_a_survivor_that_is_still_the_same_surface():
    # The same stop (module-level function) or an EQUAL bound method of the same
    # long-lived object (terminal's MANAGER.close_all) is the same surface — restarting
    # it would e.g. kill every open terminal on a config save.
    class Manager:
        def close_all(self):
            pass

    mgr = Manager()

    def module_stop():
        pass

    running = [_handle("t", "sessions", stop=mgr.close_all), _handle("d", "health", stop=module_stop)]
    wanted = [_spec("t", "sessions", stop=mgr.close_all), _spec("d", "health", stop=module_stop)]
    assert running[0]["stop"] is not wanted[0]["stop"]  # fresh bound-method objects…

    _, _, to_reload, to_restart = ai._plan_surface_reconcile(running, wanted)

    assert to_restart == []  # …but equal, so not restarted
    assert [h["plugin_id"] for h in to_reload] == ["t", "d"]


def test_plan_leaves_a_surface_with_a_reload_hook_on_the_reload_path():
    # A reload hook is the plugin opting into live reconfiguration (Discord, the board
    # loop) — its new-closure stop must not turn every config save into a restart.
    running = [_handle("discord", "gw", stop=lambda: None, reload=lambda cfg: None)]
    wanted = [_spec("discord", "gw", stop=lambda: None, reload=lambda cfg: None)]

    _, _, to_reload, to_restart = ai._plan_surface_reconcile(running, wanted)

    assert to_restart == [] and to_reload == running


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


# ── #3593 end to end: the real loader, register() run twice ────────────────────

_SWEEP_PLUGIN = """
import asyncio

EVENTS = []          # module-level: survives re-register (the module isn't re-exec'd)


class Dispatcher:
    count = 0

    def __init__(self):
        Dispatcher.count += 1
        self.gen = Dispatcher.count


def register(registry):
    dispatcher = Dispatcher()          # one per register(), like pr-reviewer
    stop_event = asyncio.Event()

    async def _loop():
        EVENTS.append(("running", dispatcher.gen))
        await stop_event.wait()
        for _ in range(3):             # finish the in-flight tick after stop()
            await asyncio.sleep(0)
        EVENTS.append(("stopped", dispatcher.gen))

    def _start():
        return asyncio.ensure_future(_loop())

    def _stop():
        stop_event.set()   # returns at once; the loop winds down on its own

    registry.register_surface(_start, _stop, name="sweep")
"""


@pytest.mark.asyncio
async def test_a_config_reload_leaves_exactly_one_sweep_on_the_new_registration(tmp_path, monkeypatch):
    import sys

    from graph.config import LangGraphConfig
    from graph.plugins import loader as plugin_loader

    d = tmp_path / "sweeper"
    d.mkdir()
    (d / "protoagent.plugin.yaml").write_text("id: sweeper\nname: sweeper\nversion: 0.1.0\nenabled: true\n")
    (d / "__init__.py").write_text(_SWEEP_PLUGIN)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [tmp_path])

    async def _drain():
        for _ in range(10):
            await asyncio.sleep(0)

    # Boot: load + start, as the startup hook does.
    boot = plugin_loader.load_plugins(LangGraphConfig())
    [spec] = [s for s in boot.surfaces if s["plugin_id"] == "sweeper"]
    handles = [{"plugin_id": "sweeper", "name": "sweep", "stop": spec["stop"], "reload": None, "handle": spec["start"]()}]
    monkeypatch.setattr(STATE, "plugin_surfaces_started", True, raising=False)
    monkeypatch.setattr(STATE, "plugin_surface_handles", handles, raising=False)
    await _drain()

    # Config save: load_plugins re-runs register() → a second Dispatcher.
    reloaded = plugin_loader.load_plugins(LangGraphConfig())
    monkeypatch.setattr(STATE, "plugin_surfaces", reloaded.surfaces, raising=False)
    ai._reload_plugin_surfaces(object())
    await _drain()

    events = sys.modules[plugin_loader._plugin_module_name("sweeper")].EVENTS
    # The gen-1 sweep stopped BEFORE gen 2 started — never two loops at once.
    assert events == [("running", 1), ("stopped", 1), ("running", 2)]
    assert [h["stop"] for h in STATE.plugin_surface_handles] == [reloaded.surfaces[0]["stop"]]
    STATE.plugin_surface_handles[0]["stop"]()  # tidy: let the gen-2 loop finish
    await _drain()
    plugin_loader.purge_plugin_modules("sweeper")
