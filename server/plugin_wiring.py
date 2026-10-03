"""Plugin wiring: HTTP router mounting, route wrapping, plugin registries, the plugin
host, and plugin-surface hot-reload reconcile.

Extracted from ``server/agent_init.py`` (#3821, epic #3804). This is the part of agent
composition that binds a built plugin bundle (ADR 0018) to the live server:

- ``_mount_plugin_routers`` remounts plugin routers on the running FastAPI app, after
  wrapping each handler in the structured error envelope (``_install_error_envelope``) and
  leaving unschemable routes out of ``/openapi.json`` (``_exclude_unschemable_routes``) —
  both BEFORE ``include_router`` (FastAPI >= 0.141 builds served routes lazily).
- ``_apply_plugin_registries`` pushes a bundle's verifiers / hooks / work providers /
  component kinds into their live module registries.
- ``_populate_plugin_host`` / ``_plugin_agent_invoke`` fill the ADR 0018 plugin host.
- ``_reload_plugin_surfaces`` (+ ``_plan_surface_reconcile``) reconciles running
  surfaces against a reloaded plugin set.

``server.agent_init`` (and ``server/__init__``) re-export these names so existing
callers keep resolving; agent_init's boot/reload path calls the entry points by bare
name, so a patch on ``agent_init._mount_plugin_routers`` / ``_reload_plugin_surfaces`` /
``_apply_plugin_registries`` still intercepts those callers.

**Patch collaborators here, not on ``agent_init``.** What these functions call by bare
name — ``_install_error_envelope``, ``_plan_surface_reconcile``, the ``_SURFACE_*`` grace
periods, ``chat`` — resolves in THIS module's globals. The two exceptions are agent_init's
own seams, called through the module at call time: ``_run_on_server_loop`` (shared with
the scheduler) and ``_apply_settings_changes`` (the settings-apply path).
"""

import asyncio
import logging
import weakref

from runtime.state import STATE
from server import _event_bus
from server.chat import chat

log = logging.getLogger("protoagent.server")


def _agent_init():
    """``server.agent_init``, resolved at call time — it imports this module at load, so a
    top-level import here would be a cycle, and a call-time lookup is what lets a test's
    patch on an agent_init seam intercept the callers below."""
    from server import agent_init

    return agent_init


def _mount_plugin_routers(routers: list[dict]) -> None:
    """Mount plugin routers (ADR 0018) onto the live app, keyed ``(plugin_id,
    prefix)``. Called at boot AND on every config reload, so enabling a
    route-bearing plugin (e.g. ``delegates``) takes effect without a restart —
    the #797 fleet blocker ("hot-reload rebuilds the graph but routes bind at
    startup"). FastAPI accepts ``include_router`` after startup; new routes are
    appended (no existing /api catch-all shadows them).

    Plugin prefix enforcement (#870): plugin routes SHOULD live under
    ``/plugins/<id>/...``. Routes at non-conforming prefixes are still mounted
    (many plugins legitimately use ``/api/...`` prefixes for their data routes)
    but a WARNING is logged. The default-deny auth middleware guards all
    non-public paths regardless of prefix, so the security gap is closed.

    REMOUNT, not skip (ADR 0096 live QA): every reload re-execs ``register()``, so
    each call carries FRESH router objects for the CURRENT code. A key that is
    already mounted gets its previous Route objects removed from
    ``app.router.routes`` (a plain list Starlette iterates per request — "FastAPI
    can't unmount" is only true of the public API) and the new ones served.
    Previously the first mount won forever: iterating on a scaffolded VIEW served
    the stale page until a restart (the #942 class — the agent correctly told the
    operator "needs a restart" mid-demo). A key ABSENT from this call's roster
    (plugin disabled/uninstalled) has its routes removed outright, retiring the
    router half of restart_recommended. During a swap the OLD route matches first
    (it precedes the new in the list) — a transient serves stale, never a 404.
    Best-effort per router so one bad plugin can't break boot or a reload."""
    app = STATE.fastapi_app
    if app is None:
        return
    from graph.plugins.registry import _prefix_conforms

    seen_this_call: set[tuple[str, str]] = set()
    for r in routers:
        plugin_id = r.get("plugin_id", "")
        prefix = r.get("prefix") or ""
        key = (plugin_id, prefix)
        if key in seen_this_call:
            # A plugin registered a SECOND router at the SAME prefix — the first
            # already won the slot, so this one is silently dropped and its routes
            # never serve (projectBoard's /board 404'd for exactly this reason).
            # Mount distinct prefixes for distinct route groups (e.g. a public
            # /plugins/<id> view router + a gated /api/plugins/<id> data router).
            log.warning(
                "[plugins] %s registered a second router at prefix %s — dropped "
                "(its routes won't be served; mount each router at a distinct prefix)",
                plugin_id,
                prefix or "/",
            )
            continue
        seen_this_call.add(key)
        # Warn for non-conforming prefixes (#870 plugin prefix enforcement).
        # The two canonical prefixes are /plugins/<id>/... (public view) and
        # /api/plugins/<id>/... (bearer-gated data router) — the documented
        # two-router pattern (#1732). Routes under any other prefix are still
        # mounted (mounted as-is; the default-deny middleware guards non-public
        # paths regardless) but the warning surfaces genuine mis-configurations.
        if plugin_id and prefix and not _prefix_conforms(prefix, plugin_id):
            log.warning(
                "[plugins] %s: router prefix %r does not start with /plugins/%s or "
                "/api/plugins/%s — plugin routes SHOULD live under /plugins/<id>/ "
                "(public) or /api/plugins/<id>/ (gated data; mounted as-is)",
                plugin_id,
                prefix,
                plugin_id,
                plugin_id,
            )
        try:
            _install_error_envelope(r["router"], plugin_id)
            # Probe BEFORE including: FastAPI >= 0.141 builds an included router's served
            # routes on the first request that reaches them and snapshots `include_in_schema`
            # then. Reloads run in a worker thread while the app keeps serving, so a request
            # landing between include and probe would cache a broken route as documented.
            _exclude_unschemable_routes(r["router"], plugin_id, prefix)
            before = len(app.router.routes)
            app.include_router(r["router"], prefix=prefix)
            fresh = list(app.router.routes[before:])
            stale = STATE.plugin_router_routes.pop(key, [])
            for route in stale:
                try:
                    app.router.routes.remove(route)
                except ValueError:  # already gone — never fatal
                    pass
            STATE.plugin_router_routes[key] = fresh
            STATE.plugin_router_keys.add(key)
            if stale:
                log.debug(
                    "[plugins] remounted router from %s at %s (%d stale route(s) replaced)",
                    plugin_id,
                    prefix or "/",
                    len(stale),
                )
            else:
                log.info("[plugins] mounted router from %s at %s", plugin_id, prefix or "/")
        except Exception:  # noqa: BLE001
            log.exception("[plugins] failed to mount router from %s", plugin_id)

    # Roster-absent keys = the plugin was disabled or uninstalled this reload —
    # its routes leave NOW instead of lingering until a restart.
    for key in [k for k in STATE.plugin_router_routes if k not in seen_this_call]:
        for route in STATE.plugin_router_routes.pop(key, []):
            try:
                app.router.routes.remove(route)
            except ValueError:
                pass
        STATE.plugin_router_keys.discard(key)
        log.info("[plugins] unmounted router from %s at %s (disabled/removed)", key[0], key[1] or "/")
    # FastAPI caches the schema against a routes version that `include_router` bumps but the
    # list surgery above doesn't, so a remount or an unmount could leave /openapi.json
    # documenting routes that just left (the version sums can even come out equal). Mark the
    # change where FastAPI tracks one — a schema build racing this reload then rebuilds rather
    # than keeping what it saw — and drop the cache for FastAPI versions that don't.
    mark_changed = getattr(app.router, "_mark_routes_changed", None)
    if callable(mark_changed):
        mark_changed()
    app.openapi_schema = None


def _plugin_api_routes(router, prefix: str = ""):
    """Yield ``(path, APIRoute)`` for every HTTP route a plugin router carries, nested
    includes included, with the path as it will serve under ``prefix``.

    FastAPI >= 0.141 includes a router LAZILY: the parent's route list holds a wrapper whose
    ``original_router`` carries the real APIRoutes (and further wrappers for deeper includes),
    and the served routes are built from those on first use. Older FastAPI copies nested
    APIRoutes into the parent directly. Either way these are the objects the served routes are
    built from, so a change made here before the router is mounted is the one that serves."""
    from fastapi.routing import APIRoute

    for entry in getattr(router, "routes", ()):
        inner = getattr(entry, "original_router", None)
        if inner is not None:
            nested = getattr(getattr(entry, "include_context", None), "prefix", "")
            yield from _plugin_api_routes(inner, prefix + nested)
        elif isinstance(entry, APIRoute):
            yield prefix + entry.path, entry


def _exclude_unschemable_routes(router, plugin_id: str, prefix: str = "") -> None:
    """Leave a plugin route out of ``/openapi.json`` when its schema can't be built.

    One route whose schema fails takes the WHOLE schema down: ``/openapi.json`` and
    ``/docs`` answered 500 on every agent running orgChart, the portfolio plugin or the
    learning-wiki plugin. Each wrote a page route as ``-> HTMLResponse`` with the import
    inside the router-builder function under ``from __future__ import annotations``, so
    FastAPI resolved the string against the module's globals, couldn't, and inferred a
    response model from an unresolved forward reference that pydantic then refused.

    Probing each route on its own before it mounts keeps a plugin's mistake local: that
    route still SERVES, it is only left out of the documented schema, and the warning names
    the plugin, the route and the fix. It covers the routes the plugin hands over; schema
    arguments given to a nested ``include_router`` call itself are not probed. Best-effort —
    a probe that can't run changes nothing."""
    try:
        from fastapi.openapi.utils import get_openapi
    except Exception:  # noqa: BLE001 — no schema tooling, nothing to protect
        return

    for path, route in _plugin_api_routes(router, prefix):
        if not route.include_in_schema:
            continue
        try:
            get_openapi(title="plugin-route-probe", version="0", routes=[route])
        except Exception as exc:  # noqa: BLE001 — any schema failure is the plugin's, not ours
            route.include_in_schema = False
            log.warning(
                "[plugins] %s: %s %s is left out of /openapi.json — its schema can't be built "
                "(%s). The route still serves. Usual cause: a return annotation naming a class "
                "imported inside a function under `from __future__ import annotations`; declare "
                "`response_class=` on the decorator instead of annotating the return type.",
                plugin_id,
                ",".join(sorted(route.methods or ())),
                path,
                type(exc).__name__,
            )


def _install_error_envelope(router, plugin_id: str) -> None:
    """Wrap a plugin router's handlers so an unhandled exception answers with a
    structured JSON error instead of Starlette's bare ``Internal Server Error`` (#2259).

    A plugin endpoint that raised on bad input produced a plain-text 500 with no body a
    caller could parse — so "my request was malformed" and "the panel blew up mid-run"
    were indistinguishable, which matters a great deal when the endpoint is long-running
    and retrying an actual crash is expensive.

    Applied at the **dispatch layer**, so it covers every plugin at once rather than
    asking each to remember. Deliberately does NOT reinterpret status: mapping, say,
    ``ValueError`` to 400 would guess that an internal bug is a client error and recreate
    the same confusion in the other direction. A plugin that wants 400 raises
    ``HTTPException(400, …)`` itself — those pass through untouched, as do the
    ``HTTPException``s FastAPI raises for request validation.

    FastAPI builds each served route from ``route.endpoint`` when the router is included
    (or, from 0.141, on first use), so wrapping the endpoint before inclusion is enough —
    for routes in nested sub-routers too, which 0.141 no longer flattens into the plugin's
    route list (they answered a bare 500). Websocket routes are left alone (an HTTP error
    body is meaningless once a socket is upgraded), and the wrapper is idempotent so the
    hot-reload path can re-mount the same router object without stacking wrappers.
    """
    for _path, route in _plugin_api_routes(router):
        route.endpoint = _wrap_plugin_endpoint(route.endpoint, plugin_id)


def _wrap_plugin_endpoint(fn, plugin_id: str):
    import functools
    import inspect

    from fastapi import HTTPException

    if getattr(fn, "__protoagent_error_envelope__", False):
        return fn

    def _envelope(exc: Exception) -> HTTPException:
        # The traceback goes to the log (the operator's copy); the response carries the
        # exception type + message so the CALLER can tell a validation reject from a
        # crash without shell access to the host.
        log.exception("[plugins] %s: unhandled error in plugin route", plugin_id)
        return HTTPException(
            500,
            detail={
                "error": f"{type(exc).__name__}: {exc}".strip()[:500] or "unhandled plugin error",
                "plugin": plugin_id,
                "type": type(exc).__name__,
            },
        )

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _wrapped(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except HTTPException:
                raise
            except Exception as exc:
                raise _envelope(exc) from exc

    else:

        @functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except Exception as exc:
                raise _envelope(exc) from exc

    _wrapped.__protoagent_error_envelope__ = True
    return _wrapped


def _apply_plugin_registries(plugins) -> None:
    """Push a freshly (re)built plugin bundle's goal verifiers + goal/watch hooks into their
    LIVE module registries (``graph.goals.verifiers`` / ``graph.goals.hooks`` /
    ``graph.watches.hooks``). Called at full init AND in the hot-reload commit — without the
    reload call, a plugin update/enable that adds or changes a verifier or hook leaves the live
    registry holding the PRE-reload mapping, so a newly-shipped watch/goal verifier resolves as
    "unknown plugin verifier" (armed but blind) until a full server restart (#1752).

    Not itself serialized: each set_* is a single GIL-atomic global rebind, and the reload
    caller already holds the config-write lock (init runs once at boot, uncontended)."""
    from graph.goals import hooks as _goal_hooks
    from graph.goals import verifiers as _goal_verifiers
    from graph.lifecycle import hooks as _lifecycle_hooks
    from graph import work_providers as _work_providers
    from graph.watches import hooks as _watch_hooks

    # getattr: a duck-typed bundle (tests) or one built before this field existed still
    # threads its verifiers; they simply render without a description.
    _goal_verifiers.set_plugin_verifiers(
        plugins.goal_verifiers, getattr(plugins, "goal_verifier_meta", None)
    )  # ADR 0028
    # getattr for the same reason as above: a bundle built before this field existed still
    # loads, it just contributes no work providers.
    _work_providers.set_plugin_work_providers(
        getattr(plugins, "work_providers", None), getattr(plugins, "work_provider_meta", None)
    )  # ADR 0079
    _goal_hooks.set_goal_hooks(plugins.goal_hooks)
    _watch_hooks.set_watch_hooks(plugins.watch_hooks)  # ADR 0067
    _lifecycle_hooks.set_lifecycle_hooks(plugins.lifecycle_hooks)  # ADR 0074
    # Plugin component-v1 kinds (#3617) — re-applied on reload so a disabled plugin's kind
    # stops extracting and a newly-enabled one starts, without a restart.
    from graph import components as _components

    _components.set_plugin_components(getattr(plugins, "components", None))
    # Plugin services (ADR 0116) — rebound wholesale so a disabled plugin's service stops
    # resolving through sdk.service the moment the reload commits.
    from graph import plugin_services as _plugin_services

    _plugin_services.set_plugin_services(
        getattr(plugins, "services", None), getattr(plugins, "service_meta", None)
    )


async def _plugin_agent_invoke(prompt: str, session_id: str, *, tool_fence: list[str] | None = None) -> str:
    """Agent invoke exposed to plugin surfaces via the plugin host (ADR 0018) — a
    chat turn joined to its assistant text (mirrors the Discord surface invoker).
    ``tool_fence`` (#2972) restricts the turn to that tool allowlist — for a surface
    relaying a message from an untrusted party (another operator's agent)."""
    result = await chat(prompt, session_id, tool_fence=tool_fence, origin="plugin")
    return "\n\n".join(m["content"] for m in result if m.get("role") == "assistant" and m.get("content"))


def _populate_plugin_host() -> None:
    """Wire the plugin host (ADR 0018) — agent invoke + event bus — so a plugin
    surface/route can reach them. Called once in _main, before startup."""
    try:
        from graph.plugins.host import HOST

        HOST.invoke = _plugin_agent_invoke
        HOST.publish = _event_bus.publish
        HOST.subscribe = _event_bus.subscribe
        HOST.on = _event_bus.subscribe_handler  # ADR 0039 — in-process topic subscriptions
        HOST.config = lambda: STATE.graph_config
        # Through the module at call time: the settings-apply path's patch seam (and the
        # patches that fake it) live on agent_init (defined in settings_apply, #3848).
        HOST.apply_settings = lambda patch: _agent_init()._apply_settings_changes(config=patch)
    except Exception:  # noqa: BLE001
        log.exception("[plugins] failed to populate plugin host")


def _surface_key(spec_or_handle) -> tuple:
    """The identity of a surface for reconcile: ``(plugin_id, name)``. Keyed on both so
    two plugins may share a surface name without colliding."""
    return (spec_or_handle.get("plugin_id"), spec_or_handle.get("name"))


def _plan_surface_reconcile(handles: list, wanted: list) -> tuple[list, list, list, list]:
    """Pure diff for surface hot-reload — no I/O, so it's unit-testable.

    ``handles`` is the live ``STATE.plugin_surface_handles`` (running surfaces); ``wanted``
    is the reloaded plugin surface spec set. Returns ``(to_stop, to_start, to_reload,
    to_restart)``:

    - ``to_stop`` — running handles whose ``(plugin_id, name)`` is no longer wanted
      (its plugin was disabled/uninstalled).
    - ``to_start`` — wanted specs not currently running (a newly-enabled plugin).
    - ``to_reload`` — handles present in both (fire the ``reload(cfg)`` callback; leave
      the surface running so a live gateway connection isn't dropped).
    - ``to_restart`` — ``(handle, spec)`` pairs for survivors that are ORPHANED from the
      current registration (#3593): the old or new registration declares no ``reload`` hook, and the re-run
      ``register()`` handed back a different ``stop`` than the one that owns the running
      surface. Its closures belong to the previous registration's objects (a dispatcher,
      a queue) while the freshly registered routes/tools hold new ones, and nothing will
      ever tell it — so stop it and start it again from the new spec. A surface with a
      ``reload`` hook opted into reconfiguring itself live and keeps that path; one whose
      ``stop`` is unchanged (module-level functions, a long-lived singleton) is the same
      surface and is left running.
    """
    running = {_surface_key(h): h for h in handles}
    wanted_by = {_surface_key(s): s for s in wanted}
    to_stop = [h for k, h in running.items() if k not in wanted_by]
    to_start = [s for k, s in wanted_by.items() if k not in running]
    to_reload: list = []
    to_restart: list = []
    for k, s in wanted_by.items():
        if k not in running:
            continue
        h = running[k]
        old_stop, new_stop = h.get("stop"), s.get("stop")
        orphaned = (
            # Both generations must declare ``reload`` for the live-reconfigure path: the
            # running surface needs one to be told anything, and a new registration that
            # dropped it no longer promises to reconfigure in place.
            not (callable(h.get("reload")) and callable(s.get("reload")))
            and callable(old_stop)
            and callable(new_stop)
            and old_stop != new_stop  # ``!=`` not ``is not``: a fresh bound method of the SAME object is equal
        )
        if orphaned:
            to_restart.append((h, s))
        else:
            to_reload.append(h)
    return to_stop, to_start, to_reload, to_restart


# How long a restarted surface's old task may take to wind down after ``stop()`` before
# it is cancelled (#3593) — long enough for a sweep tick, short enough not to stall a
# reload. A task that ignores the cancel too gets ``_SURFACE_CANCEL_GRACE_S`` more, then
# the restart is abandoned and the old surface kept: two generations never run at once.
_SURFACE_RESTART_GRACE_S = 10.0
_SURFACE_CANCEL_GRACE_S = 2.0

# One reconcile at a time per loop (#3593). A restart can await a stop() and a grace
# period; a second reload's reconcile planning from the same handles meanwhile would see
# the surface half-stopped and start it twice. Keyed by loop: an asyncio.Lock binds to
# the loop it is first contended on.
_SURFACE_RECONCILE_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _surface_reconcile_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _SURFACE_RECONCILE_LOCKS.get(loop)
    if lock is None:
        lock = _SURFACE_RECONCILE_LOCKS[loop] = asyncio.Lock()
    return lock


def _reload_plugin_surfaces(new_config) -> None:
    """Reconcile running plugin surfaces against the reloaded plugin set (ADR 0018/0019).

    On a config reload: **stop** surfaces whose plugin was disabled/uninstalled,
    **hot-start** newly-enabled plugins' surfaces, **restart** survivors orphaned from the
    re-run ``register()`` (no ``reload`` hook, new ``stop`` — #3593), and fire each other
    survivor's ``reload(cfg)`` callback so a Discord/Google-style gateway reconnects on a
    token/admin change without a restart. Before this, a reload only fired reload callbacks
    — a newly-enabled surface stayed dead and a disabled one leaked (kept running) until a
    full restart.

    A no-op until the startup hook has started surfaces (``plugin_surfaces_started``): a
    reload before boot's surface loop would double-start (here AND there). The whole
    reconcile runs as ONE coroutine on the server loop under a per-loop lock, so
    back-to-back reloads reconcile one after another, each against the surface set
    current when it gets the lock. Best-effort per surface: a failure logs, never breaks
    the reload.
    """
    if not STATE.plugin_surfaces_started:
        return  # the pending startup hook will start the already-updated STATE.plugin_surfaces

    async def _ended(h, what: str) -> bool:
        """Call ``h``'s stop and confirm its task ended (grace, then cancel). A surface
        that still won't end is recorded in ``STATE.plugin_surfaces_stuck`` — its old task
        keeps running, which only a process restart clears (the one case the plugin routes
        still answer ``restart_recommended`` for)."""
        stop_ok = True
        stop_cb = h.get("stop")
        if callable(stop_cb):
            try:
                res = stop_cb()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("[plugins] surface %s %s failed", h.get("name"), what)
                stop_ok = False
        task = h.get("handle")
        if isinstance(task, asyncio.Future):
            # A stop() that only sets an event returns while the old tick still runs.
            if not task.done():
                await asyncio.wait({task}, timeout=_SURFACE_RESTART_GRACE_S)
            if not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=_SURFACE_CANCEL_GRACE_S)
            ended = task.done()
        else:
            # No task to watch: the stop callback's success is the only evidence it ended.
            ended = stop_ok
        key = _surface_key(h)
        if ended:
            STATE.plugin_surfaces_stuck.pop(key, None)
        else:
            STATE.plugin_surfaces_stuck[key] = f"did not stop ({what})"
        return ended

    async def _stop(h) -> None:
        if not await _ended(h, "stop-on-reload"):
            log.error(
                "[plugins] surface %s did not stop — its task is still running; restart the agent to end it",
                h.get("name"),
            )
        if h in STATE.plugin_surface_handles:
            STATE.plugin_surface_handles.remove(h)

    async def _start(s) -> bool:
        try:
            res = s["start"]()
            if asyncio.iscoroutine(res):
                res = await res
        except Exception:
            log.exception("[plugins] surface %s failed to hot-start", s.get("name"))
            return False
        STATE.plugin_surface_handles.append(
            {
                "plugin_id": s.get("plugin_id"),
                "name": s.get("name"),
                "start": s.get("start"),
                "stop": s.get("stop"),
                "reload": s.get("reload"),
                "handle": res,
            }
        )
        return True

    async def _stopped_for_restart(h) -> bool:
        """Stop ``h`` and confirm it ended. True → safe to start its replacement. On False
        the old handle stays in ``plugin_surface_handles`` (it may still be running)."""
        if not await _ended(h, "stop-on-restart"):
            log.error(
                "[plugins] surface %s did not stop — keeping it and NOT starting its replacement; "
                "restart the agent to pick up the plugin's new registration",
                h.get("name"),
            )
            return False
        if h in STATE.plugin_surface_handles:
            STATE.plugin_surface_handles.remove(h)
        return True

    async def _run():
        async with _surface_reconcile_lock():
            await _reconcile()

    async def _reconcile():
        wanted = list(STATE.plugin_surfaces)  # the latest set, read under the lock
        to_stop, to_start, to_reload, to_restart = _plan_surface_reconcile(STATE.plugin_surface_handles, wanted)
        for h in to_stop:
            await _stop(h)
            log.info("[plugins] stopped surface %s — its plugin was disabled/removed", h.get("name"))
        for s in to_start:
            if await _start(s):
                log.info("[plugins] hot-started surface %s — its plugin was enabled", s.get("name"))
        for h, s in to_restart:
            # Two generations of a sweep must never dispatch side by side: start the
            # replacement only once the old one is confirmed ended.
            if not await _stopped_for_restart(h):
                continue
            if await _start(s):
                log.info("[plugins] restarted surface %s — its plugin re-registered it", s.get("name"))
                continue
            # The replacement failed to start and the old one is already stopped. Bring
            # the old generation back rather than leave the surface dead: that is exactly
            # what ran before this reload (stale objects, but alive and tracked).
            if callable(h.get("start")) and await _start(h):
                log.error(
                    "[plugins] surface %s: its new registration failed to start — restored the "
                    "previous one; fix the plugin and reload, or restart the agent",
                    h.get("name"),
                )
            else:
                log.error(
                    "[plugins] surface %s is DOWN: its new registration failed to start and the "
                    "previous one could not be restored — restart the agent",
                    h.get("name"),
                )
        for h in to_reload:
            reload_cb = h.get("reload")
            if not callable(reload_cb):
                continue
            try:
                res = reload_cb(new_config)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("[plugins] surface %s reload failed", h.get("name"))

    # agent_init owns the loop marshal (the scheduler uses it too) — looked up there at
    # call time so a patch on agent_init._run_on_server_loop intercepts this caller.
    # The handle is kept on STATE so a plugin route that just reloaded can wait for the
    # reconcile before it answers ``restart_recommended``.
    STATE.plugin_surface_reconcile = _agent_init()._run_on_server_loop(lambda: _run(), "surface reconcile")
