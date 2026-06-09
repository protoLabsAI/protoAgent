"""protoAgent — FastAPI server wrapping a LangGraph agent with A2A.

This is the main entry point. It:

1. Initializes LangGraph (``graph/agent.py``) + the LiteLLM gateway
   connection via ``graph/llm.py``.
2. Mounts the full A2A 1.0 surface (``a2a-sdk`` ``DefaultRequestHandler`` +
   ``a2a_executor.ProtoAgentExecutor``, conventions via ``protolabs_a2a``)
   — JSON-RPC on ``POST /a2a``, SSE streaming, push notifications,
   ``tasks/*`` CRUD, agent card at ``/.well-known/agent-card.json``.
3. Mounts an OpenAI-compatible chat-completions endpoint so the agent
   can be registered as a model in the LiteLLM gateway / OpenWebUI.
4. Optionally mounts a Gradio chat UI for direct operator access.
5. Exposes a Prometheus ``/metrics`` endpoint when the ``metrics``
   module is active.

### Forking checklist

- Change the agent identity in ``_build_agent_card_proto`` /
  ``protolabs_a2a.build_agent_card`` (name, description, skills, extensions).
- Drop ``SOUL.md`` in the workspace to override the default agent prompt.
- Add your real tools to ``tools/lg_tools.py`` and wire them into
  ``graph/subagents/config.py`` if you want specialized delegation.
- Set the ``<AGENT>_API_KEY`` env var name below to match your agent's
  auth naming convention.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time

import httpx
from pathlib import Path
from typing import TYPE_CHECKING, Any

from events import ACTIVITY_CONTEXT, EventBus
from paths import scope_leaf
from runtime.state import STATE, get_state
from graph.output_format import (
    DROPPED_SCRATCH_KICKER,
    extract_confidence,
    extract_output,
    is_dropped_scratch_turn,
    stream_visible_output,
)

if TYPE_CHECKING:
    from scheduler.interface import SchedulerBackend

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Root-level log config. Python's default is WARNING, which silently filters
# every `logger.info(...)` call — including "webhook delivered" lines from
# the A2A push sender, making the A2A/webhook path invisible in docker logs.
# LOG_LEVEL env var lets operators tune without a code change.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("protoagent.server")


# ---------------------------------------------------------------------------
# Agent setup
# ---------------------------------------------------------------------------

                         # mounted ONCE at init; not hot-reloaded.
                         # — started in the startup hook, stopped on shutdown.
                       # Read by the autostart installer so the LaunchAgent reboots
                       # on the same port the operator launched with, not the default.
                       # Constructed at init, started on FastAPI startup, stopped
                       # on shutdown. Lifecycle is hooked in _main() so the
                       # polling coroutine doesn't leak on server reload.
                       # lifecycle as STATE.scheduler; keeps the prompt cache warm.
                         # control messages and runs the goal-completion loop.
                         # A config reload's heavy graph compile is offloaded to a
                         # worker thread so it no longer freezes the loop; the
                         # scheduler/Discord restart that follows still has to run
                         # ON the loop, so the worker thread schedules it here via
                         # run_coroutine_threadsafe instead of get_running_loop()
                         # (which would silently no-op in the thread — the trap).

_event_bus = EventBus()  # Server→client SSE push channel (ADR 0003). Process-
                         # lifetime singleton; producers publish, /api/events
                         # streams to connected consoles.


def _bundle_root() -> Path:
    """Root that read-only bundled assets (``static``, ``config``, ``plugins``,
    bundled ``workflows``, ``pyproject.toml``) resolve against.

    Source checkout: the repo root. This file is ``server/__init__.py``, so the
    repo is its parent's parent. Frozen sidecar (PyInstaller onefile): the
    ``_MEIPASS`` extraction dir where ``--add-data`` lands assets at the top
    level. Before ADR 0023 promoted ``server.py`` into this package these
    lookups were ``Path(__file__).parent``; the package adds one directory
    level, so they route through here to stay anchored at the repo / bundle
    root."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parents[1]


def _resolve_operator_project_root() -> str:
    """The operator console's default project root (+ its always-allowed dir).

    In a source checkout this is the repo root (``__file__``'s dir). But in a
    PyInstaller-frozen sidecar (the desktop app) ``__file__`` lives inside the
    ephemeral ``_MEIxxxx`` onefile extraction dir — which doesn't persist and
    isn't a real workspace, so the console's project-scoped APIs (notes/beads)
    fail with "project_path does not exist". Resolve a stable, writable dir
    instead: an explicit ``PROTOAGENT_PROJECT_DIR`` wins; else (when frozen) the
    per-user app dir the desktop already provides via ``PROTOAGENT_CONFIG_DIR``,
    else the home dir."""
    env = os.environ.get("PROTOAGENT_PROJECT_DIR")
    if env:
        return str(Path(env).expanduser().resolve())
    if getattr(sys, "frozen", False):
        cfg = os.environ.get("PROTOAGENT_CONFIG_DIR")
        base = Path(cfg) if cfg else Path.home()
        return str(base.expanduser().resolve())
    return str(_bundle_root())


def _install_parent_death_watchdog() -> None:
    """Exit if the launcher process (``PROTOAGENT_PARENT_PID``) goes away.

    Set by the desktop's Tauri shell (apps/desktop/src-tauri/src/lib.rs) when it
    spawns this server as a sidecar. A PyInstaller onefile runs as a bootloader
    + re-exec'd child, so the shell killing the tracked bootloader on exit can
    leave this process orphaned and holding its port. Polling the launcher PID
    and exiting when it dies reaps the whole tree regardless of how the shell
    went away (clean quit, crash, or SIGKILL). No-op when the env isn't set
    (normal standalone / container runs)."""
    ppid_s = os.environ.get("PROTOAGENT_PARENT_PID")
    if not ppid_s:
        return
    try:
        ppid = int(ppid_s)
    except ValueError:
        return

    import threading

    def _watch() -> None:
        while True:
            time.sleep(2)
            try:
                os.kill(ppid, 0)  # signal 0 = liveness probe; raises if gone
            except OSError:
                log.info("[watchdog] launcher pid %d gone — exiting sidecar", ppid)
                os._exit(0)
            except Exception:  # noqa: BLE001 — never let the watchdog crash the server
                return

    threading.Thread(target=_watch, daemon=True, name="parent-death-watchdog").start()




# Chat backend (ADR 0023 phase 2) — the turn loop, tool/interrupt shaping, and
# slash-command parsing/execution live in server/chat.py. Re-exported here so
# server.<symbol> keeps resolving for the OpenAI-compat + A2A wiring in _main and
# for the test suite. chat.py imports nothing from this module, so no cycle.
from server.chat import (  # noqa: E402,F401 — re-export of the extracted chat backend
    _TOOL_PREVIEW_CHARS,
    _chat_langgraph,
    _chat_langgraph_stream,
    _coerce_tool_output,
    _coerce_tool_value,
    _interrupt_payload,
    _parse_slash_command,
    _parse_subagent_command,
    _parse_workflow_command,
    _parse_workflow_inputs,
    _run_parsed_subagent,
    _run_parsed_workflow,
    _run_turn_stream,
    _setup_required_message,
    chat,
)


# ---------------------------------------------------------------------------
# Agent card — EDIT THIS when forking
# ---------------------------------------------------------------------------

AGENT_NAME_ENV = os.environ.get("AGENT_NAME", "protoagent")


def agent_name() -> str:
    """Resolve the active agent name.

    Preference order: wizard-set ``identity.name`` in YAML (when loaded
    and non-placeholder) → ``AGENT_NAME`` env var → ``"protoagent"``.
    The agent card, OpenAI-compat model id, and chat header all call
    this so a wizard rename propagates without a restart. The
    Prometheus metric prefix and ``<AGENT>_API_KEY`` env name are
    set at boot and still require a restart (see docs).
    """
    if STATE.graph_config and STATE.graph_config.identity_name and STATE.graph_config.identity_name != "protoagent":
        return STATE.graph_config.identity_name
    return AGENT_NAME_ENV


# A2A surface (ADR 0023 phase 2) — card building, skill declarations, per-turn
# telemetry, and the executor terminal hook live in ``server/a2a.py``. They're
# re-exported here so ``server.<symbol>`` keeps resolving for ``_main``'s a2a-sdk
# wiring below and for the test suite. ``a2a.py`` imports ``agent_name`` /
# ``_event_bus`` / ``_bundle_root`` from this module — all defined above, so this
# import is not a cycle.
from server.a2a import (  # noqa: E402,F401 — re-export of the extracted A2A surface
    _SKILL_SPECS,
    _a2a_card_url,
    _a2a_terminal,
    _agent_skills,
    _bearer_configured,
    _build_agent_card_proto,
    assert_routable_card_url,
    _package_version,
    _record_a2a_telemetry,
    structured_skill_schema,
)
# Agent init / builders / reload / settings (ADR 0023 phase 2) live in
# server/agent_init.py. Re-exported here so server.<symbol> keeps resolving for
# _main's wiring below and the test suite. agent_init.py imports agent_name /
# AGENT_NAME_ENV / _event_bus / _bundle_root from this module — all defined above
# this line — so the import is not a cycle.
from server.agent_init import (  # noqa: E402,F401 — re-export of the extracted agent-init backend
    _apply_settings_changes,
    _build_activity_log,
    _build_checkpointer,
    _build_inbox_store,
    _build_knowledge_store,
    _build_mcp,
    _build_plugins,
    _build_scheduler,
    _build_settings_callbacks,
    _build_skills_index,
    _build_telemetry_store,
    _build_workflow_registry,
    _checkpoint_prune_loop,
    _init_langgraph_agent,
    _monitor_goals_loop,
    _plugin_agent_invoke,
    _populate_plugin_host,
    _register_plugin_subagents,
    _reload_langgraph_agent,
    _reload_plugin_surfaces,
    _resolve_checkpoint_db,
    _resolve_skills_db,
    _retire_thread,
    _run_on_server_loop,
    _seed_instance_env,
    _start_scheduler_async,
    _stop_scheduler_async,
    _sync_autostart_with_config,
)




# ---------------------------------------------------------------------------
# Main — FastAPI + Gradio + A2A + OpenAI-compat + Prometheus
# ---------------------------------------------------------------------------

def _main():

    # Plugin management subcommand (ADR 0027): `python -m server plugin install
    # <git-url>` (+ list/uninstall/sync). Handled before the server argparse —
    # it fetches code to disk and exits, never starting the server.
    if len(sys.argv) > 1 and sys.argv[1] == "plugin":
        from graph.plugins.cli import run_plugin_cli

        raise SystemExit(run_plugin_cli(sys.argv[2:]))

    # Workspace management subcommand (ADR 0041): `python -m server workspace
    # new/ls/run/rm` — named, isolated agents on one host. `new`/`ls`/`rm` act on
    # disk and exit; `run` execs the normal server with the workspace's config dir +
    # instance + port wired in (so the dispatch below runs unchanged for it).
    if len(sys.argv) > 1 and sys.argv[1] == "workspace":
        from graph.workspaces.cli import run_workspace_cli

        raise SystemExit(run_workspace_cli(sys.argv[2:]))

    # Skills subcommand (ADR 0041 slice 3): `python -m server skills ls|promote <name>`
    # — inspect/curate the layered (commons ∪ private) skill library. Acts on the DBs
    # and exits.
    if len(sys.argv) > 1 and sys.argv[1] == "skills":
        from graph.skills.cli import run_skills_cli

        raise SystemExit(run_skills_cli(sys.argv[2:]))

    # Fleet subcommand (ADR 0042 slice 1): `python -m server fleet up|down|ls` —
    # run workspace agents as persistent background processes (start/stop/status).
    if len(sys.argv) > 1 and sys.argv[1] == "fleet":
        from graph.fleet.cli import run_fleet_cli

        raise SystemExit(run_fleet_cli(sys.argv[2:]))

    # Frozen-binary entrypoint for a plugin's managed MCP server (ADR 0019): the
    # bundled desktop app has no `python` on PATH, so a plugin's managed-server
    # factory re-invokes this binary with `--mcp-plugin <id>` instead of `-m
    # <module>`. We import that plugin's module and call its `mcp_main()`. Handle
    # it before argparse/server startup. (The Google plugin is the first user.)
    if "--mcp-plugin" in sys.argv:
        i = sys.argv.index("--mcp-plugin")
        plugin_id = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        from graph.plugins.loader import run_plugin_mcp_main

        run_plugin_mcp_main(plugin_id)
        return

    parser = argparse.ArgumentParser(
        prog="python -m server",
        description=f"{AGENT_NAME_ENV} — protoAgent server",
    )
    parser.add_argument("--port", type=int, default=7870)
    # Bind host. Defaults to loopback so a local/desktop run is NOT exposed on
    # all interfaces (prod-readiness: the console + operator API are otherwise
    # reachable by anything that can reach the port). Containers set
    # PROTOAGENT_HOST=0.0.0.0 (entrypoint / deploy manifests) because their
    # boundary is the network policy + published port, not the in-container bind.
    parser.add_argument("--host", type=str, default=os.environ.get("PROTOAGENT_HOST", "127.0.0.1"))
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument(
        "--ui",
        choices=["full", "console", "none"],
        default=os.environ.get("PROTOAGENT_UI", "").lower() or None,
        help="UI deployment tier (ADR 0010): 'full' = Gradio + React console + "
             "API/A2A (local default); 'console' = React console + API/A2A, no "
             "Gradio (desktop sidecar); 'none' = API + A2A + /metrics only "
             "(headless servers / the lighter stack). Env: PROTOAGENT_UI.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=os.environ.get("PROTOAGENT_HEADLESS", "").lower() in ("1", "true", "yes"),
        help="DEPRECATED alias for --ui console.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Headless setup (ADR 0010): validate the live config + mark setup "
             "complete, then exit. No wizard/UI needed.",
    )
    args = parser.parse_args()
    STATE.active_port = args.port

    # Resolve the UI tier: explicit --ui/PROTOAGENT_UI wins; else the deprecated
    # --headless/PROTOAGENT_HEADLESS maps to 'console'; else default 'full'.
    if args.ui:
        ui = args.ui
    elif args.headless:
        ui = "console"
        log.warning("--headless / PROTOAGENT_HEADLESS is deprecated — use --ui console.")
    else:
        ui = "full"

    # `--setup` one-shot: complete setup headlessly and exit.
    if args.setup:
        from graph.config import LangGraphConfig
        from graph.config_io import (
            CONFIG_YAML_PATH, ensure_live_config, mark_setup_complete, validate_for_headless,
        )
        ensure_live_config()
        cfg = LangGraphConfig.from_yaml(CONFIG_YAML_PATH)
        ok, reason = validate_for_headless(cfg)
        if not ok:
            print(f"setup: config invalid — {reason}", file=sys.stderr)
            raise SystemExit(2)
        mark_setup_complete()
        print("setup: complete — .setup-complete written; the graph will compile on next start.")
        raise SystemExit(0)

    # Headless setup applies when there is no wizard to finish it: the 'none'
    # tier, or an explicit opt-in env (ADR 0010).
    headless_setup = ui == "none" or os.environ.get("PROTOAGENT_HEADLESS_SETUP", "").lower() in ("1", "true", "yes")

    # Initialize observability
    import tracing
    import metrics
    tracing.init()
    metrics.init()

    _init_langgraph_agent(headless_setup=headless_setup)

    # Gradio chat UI — only the 'full' tier (ADR 0010). 'console'/'none' never
    # import Gradio (its biggest, PyInstaller-hostile dep). If it's not installed
    # in 'full' (lean deps), degrade to 'console' rather than crash.
    blocks = None
    if ui == "full":
        try:
            from chat_ui import create_chat_app
            blocks = create_chat_app(
                chat_fn=chat,
                title=agent_name(),
                subtitle="protoAgent",
                placeholder="Send a message...",
                pwa=True,
                settings=_build_settings_callbacks(),
            )
        except ImportError:
            log.warning(
                "gradio not installed — degrading --ui full to console. "
                "Install it (`pip install -r requirements-ui.txt`) for the Gradio UI.",
            )
            ui = "console"

    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel as PydanticBaseModel

    fastapi_app = FastAPI(title=f"{agent_name()} — protoAgent")
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=(
            r"^(tauri://localhost|http://tauri\.localhost|"
            r"https?://(localhost|127\.0\.0\.1)(:\d+)?)$"
        ),
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    # --- React operator-console API ----------------------------------------
    # Console handler bodies live in operator_api/console_handlers.py (ADR 0023
    # phase 3); imported here as `_console` and wired into register_operator_routes
    # below. The register_*_routes registrars mount the rest of the console API.
    from operator_api import console_handlers as _console
    from operator_api.chat_routes import register_chat_routes
    from operator_api.config_routes import register_config_routes
    from operator_api.knowledge_routes import register_knowledge_routes
    from operator_api.plugin_routes import register_plugin_routes
    from operator_api.mcp_routes import register_mcp_routes
    from operator_api.routes import register_operator_routes
    from operator_api.telemetry_routes import register_telemetry_routes

    # The in-process beads store is agent-global + graph-independent, but it's
    # otherwise created in _init_langgraph_agent (which only runs once setup is
    # complete). For a fresh, unconfigured agent (first launch, before the wizard)
    # ensure it exists now — otherwise the beads routes bind the CLI fallback
    # service that raises "project_path is required" (the agent-global adapter
    # ignores project_path). Reused by _init_langgraph_agent later.
    if STATE.beads_store is None:
        from beads import BeadsStore
        STATE.beads_store = BeadsStore()

    # Console handler bodies live in operator_api/console_handlers.py (ADR 0023
    # phase 3); _main just wires them to their routes.
    register_operator_routes(
        fastapi_app,
        runtime_status=_console._operator_runtime_status,
        subagent_list=_console._operator_subagent_list,
        tools_list=_console._operator_tools_list,
        subagent_run=_console._operator_subagent_run,
        subagent_batch=_console._operator_subagent_batch,
        beads_store=STATE.beads_store,
        allowed_dirs=_console._operator_allowed_dirs,
        scheduler_list=_console._operator_scheduler_list,
        scheduler_add=_console._operator_scheduler_add,
        scheduler_cancel=_console._operator_scheduler_cancel,
        goal_list=_console._operator_goals_list,
        goal_clear=_console._operator_goals_clear,
        goal_set=_console._operator_goals_set,
        chat_commands=_console._operator_chat_commands,
        workflows_list=_console._operator_workflows_list,
        workflows_run=_console._operator_workflow_run,
        workflows_save=_console._operator_workflow_save,
        workflows_delete=_console._operator_workflow_delete,
        events_subscribe=_event_bus.subscribe,
        events_publish=_event_bus.publish,
        activity_list=_console._operator_activity_list,
        inbox_add=_console._operator_inbox_add,
        inbox_authorized=_console._inbox_authorized,
        inbox_list=_console._operator_inbox_list,
        inbox_deliver=_console._operator_inbox_deliver,
    )

    # Wire the plugin host (agent invoke + event bus) before any surface starts.
    _populate_plugin_host()

    # Plugin-contributed routes (ADR 0018) — mounted after the core routes,
    # under each plugin's namespaced prefix (default /plugins/<id>). Once, here;
    # routes don't hot-reload. Best-effort so one bad router can't break boot.
    for r in STATE.plugin_routers:
        try:
            fastapi_app.include_router(r["router"], prefix=r["prefix"])
            log.info("[plugins] mounted router from %s at %s", r["plugin_id"], r["prefix"] or "/")
        except Exception:
            log.exception("[plugins] failed to mount router from %s", r.get("plugin_id"))

    # --- Scheduler lifecycle ------------------------------------------------
    # The local scheduler needs an asyncio polling task; the Workstacean
    # adapter is a no-op start/stop. Both implement the same contract so
    # we just call through. on_event is preferred over a lifespan
    # context manager here — the rest of the boot is sync (uvicorn.run
    # is the only blocking call) and FastAPI fires startup/shutdown
    # around it.
    @fastapi_app.on_event("startup")
    async def _scheduler_startup() -> None:
        # Capture the server's event loop so an offloaded reload (#497) can
        # schedule the scheduler/Discord restart back onto it from a worker
        # thread (see _run_on_server_loop).
        import asyncio

        STATE.main_loop = asyncio.get_running_loop()
        if STATE.scheduler is not None:
            try:
                await STATE.scheduler.start()
            except Exception:
                log.exception("[scheduler] startup failed")
        if STATE.cache_warmer is not None:
            try:
                await STATE.cache_warmer.start()
            except Exception:
                log.exception("[cache-warmer] startup failed")
        # Periodic maintenance — checkpoint pruning and/or telemetry retention.
        # Starts if either is enabled (the loop guards each step independently).
        _cfg = STATE.graph_config
        if _cfg is not None and (
            (STATE.checkpoint_path and _cfg.checkpoint_prune_interval_hours > 0)
            or getattr(_cfg, "telemetry_retention_days", 0) > 0
        ):
            import asyncio
            STATE.checkpoint_prune_task = asyncio.create_task(_checkpoint_prune_loop())

        # Monitor-goal cadence (ADR 0030) — out-of-band verifier ticks so a met
        # long-horizon goal finishes without a session turn.
        if STATE.graph_config is not None and getattr(STATE.graph_config, "goal_enabled", True):
            import asyncio
            STATE.monitor_goals_task = asyncio.create_task(_monitor_goals_loop())

        # (The inbound Discord gateway now starts as the discord plugin's surface,
        # below — ADR 0018/0019.)

        # Plugin-contributed surfaces (ADR 0018) — start each on the loop. `start`
        # may be sync or async and may return a handle (kept for shutdown).
        # Best-effort: a failing surface logs, never breaks boot.
        for s in STATE.plugin_surfaces:
            try:
                res = s["start"]()
                if asyncio.iscoroutine(res):
                    res = await res
                STATE.plugin_surface_handles.append(
                    {"name": s["name"], "stop": s.get("stop"), "reload": s.get("reload"), "handle": res}
                )
                log.info("[plugins] started surface: %s", s["name"])
            except Exception:
                log.exception("[plugins] surface %s failed to start", s.get("name"))

    @fastapi_app.on_event("shutdown")
    async def _scheduler_shutdown() -> None:
        # Stop plugin surfaces first (ADR 0018) — best-effort.
        for h in STATE.plugin_surface_handles:
            stop = h.get("stop")
            if not callable(stop):
                continue
            try:
                res = stop()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("[plugins] surface %s failed to stop", h.get("name"))
        if STATE.scheduler is not None:
            try:
                await STATE.scheduler.stop()
            except Exception:
                log.exception("[scheduler] shutdown failed")
        if STATE.cache_warmer is not None:
            try:
                await STATE.cache_warmer.stop()
            except Exception:
                log.exception("[cache-warmer] shutdown failed")
        try:
            from surfaces.discord import stop as _stop_discord
            await _stop_discord()
        except Exception:
            log.exception("[discord] shutdown failed")
        if STATE.checkpoint_prune_task is not None:
            STATE.checkpoint_prune_task.cancel()
        if STATE.monitor_goals_task is not None:
            STATE.monitor_goals_task.cancel()
        # Close the long-lived A2A push-notification client (created below in
        # _main) so its connection pool doesn't leak on shutdown/reload — matters
        # in the desktop-sidecar restart loop. Best-effort; NameError if boot
        # never reached its construction is swallowed.
        try:
            await _a2a_push_client.aclose()
        except Exception:
            log.exception("[a2a] push client close failed")

    # Chat / goal / health / OpenAI-compat HTTP surface. Extracted to
    # operator_api/chat_routes.py (ADR 0023 phase 3); ``ui`` is passed in
    # because /healthz echoes the active tier.
    register_chat_routes(fastapi_app, ui)

    # Knowledge store + Playbooks (ADR 0020). Extracted to
    # operator_api/knowledge_routes.py (ADR 0023 phase 3).
    register_knowledge_routes(fastapi_app)
    register_plugin_routes(fastapi_app)

    # Fleet control plane (ADR 0042) — /api/fleet (list/create/start/stop) +
    # /api/archetypes. The CLI + the desktop GUI panels both drive these.
    from operator_api.fleet_routes import register_fleet_routes
    register_fleet_routes(fastapi_app)
    register_mcp_routes(fastapi_app)

    # --- Telemetry (ADR 0006 Slice 2) --------------------------------------
    # Per-turn cost/latency + advise-only insights (ADR 0006). Extracted to
    # operator_api/telemetry_routes.py (ADR 0023 phase 3).
    register_telemetry_routes(fastapi_app)

    # Live config / SOUL editing, model probe/test, setup wizard, and
    # schema-driven settings. Extracted to operator_api/config_routes.py
    # (ADR 0023 phase 3).
    register_config_routes(fastapi_app)

    # OpenAI-compatible /v1/chat/completions + /v1/models are registered above
    # by register_chat_routes (operator_api/chat_routes.py, ADR 0023 phase 3).

    # --- A2A protocol (a2a-sdk 1.0) -----------------------------------------
    # a2a-sdk owns all protocol mechanics: JSON-RPC dispatch, SSE streaming,
    # the task lifecycle, and push delivery. Our ProtoAgentExecutor bridges
    # protoagent's LangGraph stream onto it, and protolabs_a2 builds the card +
    # emits the four custom extensions. Task + push-config state is durable
    # (SQLite via a2a_stores), and push callbacks are SSRF-guarded.
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes.agent_card_routes import create_agent_card_routes
    from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes

    import a2a_auth
    from a2a_executor import ProtoAgentExecutor, set_terminal_hook
    from a2a_stores import (
        build_a2a_stores,
        build_push_sender,
        initialize_a2a_stores,
    )

    STATE.telemetry_store = _build_telemetry_store(STATE.graph_config)

    # ADR 0003 / 0006: record telemetry + surface Activity output on terminal.
    set_terminal_hook(_a2a_terminal)

    # Request-time auth + origin enforcement (a2a-sdk advertises schemes on the
    # card but does not enforce them). Bearer = YAML auth.token / A2A_AUTH_TOKEN;
    # X-API-Key = <AGENT>_API_KEY; origin = A2A_ALLOWED_ORIGINS.
    #
    # ``auth_token`` defaults to "" when no YAML/secret token is set — collapse
    # that to ``None`` so configure() applies the documented A2A_AUTH_TOKEN env
    # fallback. (configure() treats an explicit "" as "bearer off, no fallback";
    # protoAgent has no separate apiKey-only flag, so unset ⇒ env, not off.)
    a2a_auth.install(
        fastapi_app,
        bearer_token=((STATE.graph_config.auth_token if STATE.graph_config else "") or None),
        api_key=os.environ.get(f"{AGENT_NAME_ENV.upper()}_API_KEY", ""),
        allowed_origins_raw=os.environ.get("A2A_ALLOWED_ORIGINS", ""),
    )

    a2a_card = _build_agent_card_proto()
    # Deploy-time guard (opt-in): refuse to start if the card would advertise a
    # loopback URL — a deployed agent that does so is silently unreachable to
    # remote consumers. No-op unless a2a.require_routable_url is set.
    assert_routable_card_url()

    # Durable SQLite-backed task + push-config stores (survive restart; 24h TTL
    # sweep on tasks). The push-config store rejects SSRF callback URLs at
    # set-time; the matching push sender re-validates at send-time.
    task_store, push_config_store, task_db, push_db = build_a2a_stores()
    asyncio.run(initialize_a2a_stores(task_store, push_config_store))
    log.info("[a2a] durable stores ready (tasks=%s, push=%s)", task_db, push_db)

    async def _structured_finalizer(skill_id: str, final_text: str):
        """Enforce a declared skill's output_schema on the lead's free-text
        answer + emit it as a DataPart (#476). None ⇒ text-only. Closes over the
        skill registry so the executor needn't import server (no circular dep)."""
        spec = structured_skill_schema(skill_id)
        if not spec:
            return None
        from graph.structured_skill import finalize_structured
        return await finalize_structured(
            skill_id, spec["schema"], spec["mime"], final_text, STATE.graph_config
        )

    _a2a_push_client = httpx.AsyncClient(timeout=30)
    a2a_request_handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(_chat_langgraph_stream, structured_finalizer=_structured_finalizer),
        task_store=task_store,
        agent_card=a2a_card,
        push_config_store=push_config_store,
        push_sender=build_push_sender(push_config_store, _a2a_push_client),
    )
    add_a2a_routes_to_fastapi(
        fastapi_app,
        agent_card_routes=create_agent_card_routes(a2a_card),
        jsonrpc_routes=create_jsonrpc_routes(a2a_request_handler, rpc_url="/a2a"),
    )
    log.info("[a2a] a2a-sdk routes mounted (JSON-RPC at /a2a, card at /.well-known/agent-card.json)")

    # --- Prometheus metrics -------------------------------------------------
    if metrics.is_enabled():
        try:
            from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
            from fastapi import Response as FastAPIResponse

            @fastapi_app.get("/metrics", include_in_schema=False)
            async def _prometheus_metrics():
                return FastAPIResponse(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
        except ImportError:
            pass

    # --- React operator console (tiers full/console; skipped in 'none') ------
    static_dir = _bundle_root() / "static"
    if ui != "none":
        from operator_api.web import mount_react_app

        web_dist_dir = _bundle_root() / "apps" / "web" / "dist"
        if mount_react_app(fastapi_app, web_dist_dir):
            log.info("React operator console mounted at /app")

    # --- Static + PWA assets (skipped in 'none') ---------------------------
    if ui != "none" and static_dir.exists():
        manifest_path = static_dir / "manifest.json"
        if manifest_path.exists():
            @fastapi_app.get("/manifest.json", include_in_schema=False)
            async def _serve_manifest() -> FileResponse:
                return FileResponse(str(manifest_path), media_type="application/manifest+json")

        sw_path = static_dir / "sw.js"
        if sw_path.exists():
            @fastapi_app.get("/sw.js", include_in_schema=False)
            async def _serve_sw() -> FileResponse:
                return FileResponse(
                    str(sw_path), media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/"},
                )

        # Root favicon so a bare browser request (the agent's base URL, not just
        # /app) shows the brand mark instead of a 404. Both /favicon.svg and the
        # legacy /favicon.ico path resolve to the SVG mark.
        favicon_path = static_dir / "favicon.svg"
        if favicon_path.exists():
            @fastapi_app.get("/favicon.svg", include_in_schema=False)
            @fastapi_app.get("/favicon.ico", include_in_schema=False)
            async def _serve_favicon() -> FileResponse:
                return FileResponse(str(favicon_path), media_type="image/svg+xml")

        fastapi_app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # --- Mount Gradio at root (only the 'full' tier) ------------------------
    if ui == "full" and blocks is not None:
        import gradio as gr

        app = gr.mount_gradio_app(
            fastapi_app, blocks, path="/",
            footer_links=[],
            favicon_path=str(static_dir / "favicon.svg") if (static_dir / "favicon.svg").exists() else None,
        )
        log.info("Starting %s (ui=full) on http://%s:%d", agent_name(), args.host, args.port)
    else:
        app = fastapi_app
        log.info("Starting %s (ui=%s) on http://%s:%d", agent_name(), ui, args.host, args.port)

    # Loud warning when exposed on all interfaces with no A2A auth token: the
    # operator/console API (/api/*, /api/chat, /v1/*) is reachable by untrusted
    # callers. Loopback (the default) is safe; a container behind a network
    # policy is the intended 0.0.0.0 case.
    if args.host not in ("127.0.0.1", "localhost", "::1") and not _bearer_configured():
        log.warning(
            "[security] binding %s with NO A2A auth token — the agent + operator "
            "API are open to anything that can reach this port. Set auth.token / "
            "A2A_AUTH_TOKEN, or bind 127.0.0.1 (the default), or front it with a "
            "network policy.", args.host,
        )

    # Don't outlive the launcher. When run as a desktop sidecar the Tauri shell
    # sets PROTOAGENT_PARENT_PID; a PyInstaller-frozen onefile runs as a
    # bootloader + child, so the shell killing the bootloader can leave this
    # server orphaned (holding its port). Poll the launcher and exit if it dies.
    _install_parent_death_watchdog()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    _main()
