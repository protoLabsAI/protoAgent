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
    from graph.config_io import is_setup_complete as _operator_setup_complete
    from operator_api.routes import register_operator_routes
    from operator_api.runtime import build_runtime_status as _build_operator_status
    from operator_api.subagents import (
        list_subagents as _operator_list_subagents,
        run_manual_subagent as _operator_run_manual_subagent,
        run_manual_subagent_batch as _operator_run_manual_subagent_batch,
    )

    _operator_repo_root = _resolve_operator_project_root()

    def _operator_allowed_dirs() -> list[str]:
        # The repo root is always operable (it's the default project);
        # config adds any extra project roots. Read live so a settings
        # reload takes effect without restarting the server.
        roots = [_operator_repo_root]
        if STATE.graph_config is not None:
            roots.extend(getattr(STATE.graph_config, "operator_allowed_dirs", []) or [])
        return roots

    def _operator_runtime_status():
        return _build_operator_status(
            config=STATE.graph_config,
            setup_complete=_operator_setup_complete(),
            graph_loaded=STATE.graph is not None,
            project_path=_operator_repo_root,
            allowed_dirs=_operator_allowed_dirs(),
            knowledge_store=STATE.knowledge_store,
            scheduler=STATE.scheduler,
            cache_warmer=STATE.cache_warmer,
            goal_controller=STATE.goal_controller,
            skills_index=STATE.skills_index,
            mcp={
                "enabled": bool(getattr(STATE.graph_config, "mcp_enabled", False)) if STATE.graph_config else False,
                "servers": STATE.mcp_meta,
                "tool_count": len(STATE.mcp_tools),
            },
            plugins=STATE.plugin_meta,
        )

    def _operator_subagent_list():
        return _operator_list_subagents(STATE.graph_config)

    async def _operator_subagent_run(req: dict):
        if STATE.graph is None:
            raise RuntimeError("agent graph is not loaded; finish setup first")
        return await _operator_run_manual_subagent(
            config=STATE.graph_config,
            knowledge_store=STATE.knowledge_store,
            scheduler=STATE.scheduler,
            description=req.get("description", ""),
            prompt=req.get("prompt", ""),
            subagent_type=req.get("type") or req.get("subagent_type", "researcher"),
            emit_skill=bool(req.get("emit_skill", False)),
        )

    async def _operator_subagent_batch(req: dict):
        if STATE.graph is None:
            raise RuntimeError("agent graph is not loaded; finish setup first")
        return await _operator_run_manual_subagent_batch(
            config=STATE.graph_config,
            knowledge_store=STATE.knowledge_store,
            scheduler=STATE.scheduler,
            tasks=req.get("tasks", []),
        )

    async def _operator_scheduler_list() -> dict:
        import asyncio
        if STATE.scheduler is None:
            return {"jobs": [], "backend": "disabled"}
        jobs = await asyncio.to_thread(STATE.scheduler.list_jobs)
        return {
            "jobs": [j.as_dict() for j in jobs],
            "backend": getattr(STATE.scheduler, "name", "local"),
        }

    async def _operator_scheduler_add(req: dict) -> dict:
        import asyncio
        if STATE.scheduler is None:
            raise RuntimeError("scheduler is not loaded (disabled or setup incomplete)")
        prompt = (req.get("prompt") or "").strip()
        schedule = (req.get("schedule") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        if not schedule:
            raise ValueError("schedule is required")
        job = await asyncio.to_thread(
            STATE.scheduler.add_job, prompt, schedule, job_id=req.get("job_id") or None
        )
        return job.as_dict()

    async def _operator_scheduler_cancel(job_id: str) -> dict:
        import asyncio
        if STATE.scheduler is None:
            raise RuntimeError("scheduler is not loaded (disabled or setup incomplete)")
        canceled = await asyncio.to_thread(STATE.scheduler.cancel_job, job_id)
        return {"canceled": bool(canceled)}

    async def _operator_goals_list() -> dict:
        import asyncio
        if STATE.goal_controller is None:
            return {"goals": [], "enabled": False}
        states = await asyncio.to_thread(STATE.goal_controller.store.all)
        return {"goals": [s.to_dict() for s in states], "enabled": True}

    async def _operator_goals_clear(session_id: str) -> dict:
        import asyncio
        if STATE.goal_controller is None:
            return {"cleared": False, "enabled": False}
        cleared = await asyncio.to_thread(STATE.goal_controller.store.clear, session_id)
        return {"cleared": bool(cleared)}

    def _operator_workflows_list() -> dict:
        if STATE.workflow_registry is None:
            return {"workflows": []}
        return {"workflows": STATE.workflow_registry.list()}

    async def _operator_workflow_run(name: str, inputs: dict) -> dict:
        if STATE.graph is None:
            raise RuntimeError("agent graph is not loaded; finish setup first")
        from graph.agent import run_manual_workflow
        return await run_manual_workflow(
            STATE.graph_config, STATE.workflow_registry,
            knowledge_store=STATE.knowledge_store, scheduler=STATE.scheduler,
            name=name, inputs=inputs or {},
        )

    def _operator_workflow_save(recipe: dict) -> dict:
        # Validate against the live subagent registry before writing, so a
        # UI-authored recipe can't reference an unknown subagent / bad DAG.
        if STATE.workflow_registry is None:
            raise RuntimeError("workflows are not available")
        from graph.subagents.config import SUBAGENT_REGISTRY
        from graph.workflows.engine import validate_recipe
        errors = validate_recipe(recipe, known_subagents=set(SUBAGENT_REGISTRY))
        if errors:
            raise ValueError("invalid recipe: " + "; ".join(errors))
        path = STATE.workflow_registry.save(recipe)
        return {"saved": True, "name": recipe.get("name"), "path": path}

    def _operator_workflow_delete(name: str) -> dict:
        if STATE.workflow_registry is None:
            raise RuntimeError("workflows are not available")
        return {"deleted": STATE.workflow_registry.delete(name)}

    async def _operator_activity_list() -> dict:
        """Return the Activity provenance feed (ADR 0022) — newest-first entries
        with origin/trigger/priority — plus the thread's message history from the
        checkpointer (for the continue view). The console renders the feed and
        opens the thread on demand."""
        entries = STATE.activity_log.recent(limit=100) if STATE.activity_log is not None else []
        messages: list[dict] = []
        if STATE.checkpointer is not None:
            thread_id = f"a2a:{ACTIVITY_CONTEXT}"
            try:
                tup = await STATE.checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
                raw = (tup.checkpoint or {}).get("channel_values", {}).get("messages", []) if tup else []
            except Exception:
                log.exception("[activity] failed to read thread %s", thread_id)
                raw = []
            for m in raw:
                role = getattr(m, "type", "")
                content = getattr(m, "content", "")
                if not isinstance(content, str):
                    content = str(content)
                if role == "human":
                    messages.append({"role": "user", "content": content})
                elif role == "ai":
                    visible = extract_output(content) or content
                    if visible.strip():
                        messages.append({"role": "assistant", "content": visible})
                # tool/system messages are omitted from the surface view
        return {"context_id": ACTIVITY_CONTEXT, "entries": entries, "messages": messages}

    def _inbox_authorized(token: str | None) -> bool:
        """Validate the inbound bearer token (ADR 0003). Mirrors the A2A posture:
        when no token is configured the endpoint is open (dev), else it must match."""
        active = ((STATE.graph_config.auth_token if STATE.graph_config else "") or os.environ.get("A2A_AUTH_TOKEN", "") or "").strip()
        if not active:
            return True
        return (token or "") == active

    async def _fire_activity_from_inbox(item: dict) -> bool:
        """Fire a now-priority inbox item as a turn into the Activity thread.
        Self-POSTs to /a2a (parity with the scheduler), guarded against storms."""
        import time
        from uuid import uuid4
        import httpx

        if STATE.storm_guard is not None and not STATE.storm_guard.allow(time.monotonic()):
            log.warning("[inbox] storm guard suppressed now-fire for item %s", item.get("id"))
            return False
        # A2A 1.0 (a2a-sdk ≥1.1): the version header + proto method name are
        # mandatory — the 0.3 `message/send` 404s with -32601. Mirrors the
        # scheduler's fire (scheduler/local.py).
        headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
        bearer = ((STATE.graph_config.auth_token if STATE.graph_config else "") or os.environ.get("A2A_AUTH_TOKEN", "")).strip()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        api_key = os.environ.get(f"{AGENT_NAME_ENV.upper()}_API_KEY", "").strip()
        if api_key:
            headers["X-API-Key"] = api_key
        mid = str(uuid4())
        body = {
            "jsonrpc": "2.0", "id": mid, "method": "SendMessage",
            "params": {
                # contextId is a field of Message in 1.0 (params-level => -32602).
                "message": {
                    "role": "ROLE_USER",
                    "parts": [{"text": item["text"]}],
                    "messageId": mid,
                    "contextId": ACTIVITY_CONTEXT,
                },
                "metadata": {"origin": "inbox", "inbox_id": item.get("id"), "inbox_source": item.get("source", ""), "priority": item.get("priority", "now")},
            },
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"http://127.0.0.1:{STATE.active_port}/a2a", headers=headers, json=body)
            # A JSON-RPC error rides a 200, so status alone isn't enough.
            if r.status_code >= 400:
                return False
            err = r.json().get("error") if r.headers.get("content-type", "").startswith("application/json") else None
            if err:
                log.warning("[inbox] now-fire rejected for item %s: %s", item.get("id"), err)
                return False
            return True
        except Exception:
            log.exception("[inbox] now-fire failed for item %s", item.get("id"))
            return False

    async def _operator_inbox_add(payload: dict) -> dict:
        """Ingest an inbound item (ADR 0003). now-priority fires an Activity turn;
        others queue for check_inbox. Dedup is handled by the store."""
        if STATE.inbox_store is None:
            raise RuntimeError("inbox not loaded; finish setup first")
        item = STATE.inbox_store.add(
            payload.get("text", ""),
            priority=payload.get("priority", "next") or "next",
            source=payload.get("source", "") or "",
            dedup_key=payload.get("dedup_key", "") or "",
        )
        if item is None:
            return {"ok": True, "deduped": True}
        _event_bus.publish("inbox.item", {
            "id": item["id"], "priority": item["priority"],
            "source": item.get("source") or "", "text": item["text"],
        })
        fired = await _fire_activity_from_inbox(item) if item["priority"] == "now" else False
        return {"ok": True, "item": item, "fired": fired}

    async def _operator_inbox_list(floor: str, include_delivered: bool) -> dict:
        if STATE.inbox_store is None:
            return {"items": []}
        items = STATE.inbox_store.list(
            priority_floor=floor or "later", include_delivered=include_delivered, limit=200,
        )
        return {"items": items}

    async def _operator_inbox_deliver(item_id: int) -> dict:
        if STATE.inbox_store is None:
            raise RuntimeError("inbox not loaded; finish setup first")
        return {"ok": True, "delivered": STATE.inbox_store.mark_delivered([item_id])}

    def _operator_chat_commands() -> dict:
        """Slash commands the chat understands — drives the composer autocomplete.

        Currently just `/goal` (when goal mode is loaded). Register a new
        server-handled control command here and the console picks it up.
        """
        commands = []
        if STATE.goal_controller is not None:
            commands.append({
                "name": "goal",
                "description": "Set, check, or clear a self-driving goal for this chat session.",
                "usage": "/goal <condition>   ·   /goal  (status)   ·   /goal clear",
            })
        # Each registered workflow is runnable as /<name> (ADR 0002).
        wf_names: set[str] = set()
        if STATE.workflow_registry is not None:
            for wf in STATE.workflow_registry.list():
                wf_names.add(wf["name"])
                declared = wf.get("inputs", []) or []
                req = "".join(f" <{i['name']}>" for i in declared if i.get("required"))
                opt = "".join(f" [{i['name']}]" for i in declared if not i.get("required"))
                commands.append({
                    "name": wf["name"],
                    "description": wf.get("description") or f"Run the {wf['name']} workflow.",
                    "usage": f"/{wf['name']}{req}{opt}",
                })
        # Each registered subagent is runnable as /<name> <prompt> (ADR 0020),
        # unless a workflow already claims the name (workflow wins in dispatch).
        try:
            from graph.subagents.config import SUBAGENT_REGISTRY
        except Exception:
            SUBAGENT_REGISTRY = {}
        for name, cfg in SUBAGENT_REGISTRY.items():
            if name in wf_names:
                continue
            commands.append({
                "name": name,
                "description": getattr(cfg, "description", "") or f"Run the {name} subagent.",
                "usage": f"/{name} <prompt>",
            })
        return {"commands": commands}

    # The in-process beads store is agent-global + graph-independent, but it's
    # otherwise created in _init_langgraph_agent (which only runs once setup is
    # complete). For a fresh, unconfigured agent (first launch, before the wizard)
    # ensure it exists now — otherwise the beads routes bind the CLI fallback
    # service that raises "project_path is required" (the agent-global adapter
    # ignores project_path). Reused by _init_langgraph_agent later.
    if STATE.beads_store is None:
        from beads import BeadsStore
        STATE.beads_store = BeadsStore()

    register_operator_routes(
        fastapi_app,
        runtime_status=_operator_runtime_status,
        subagent_list=_operator_subagent_list,
        subagent_run=_operator_subagent_run,
        subagent_batch=_operator_subagent_batch,
        beads_store=STATE.beads_store,
        allowed_dirs=_operator_allowed_dirs,
        scheduler_list=_operator_scheduler_list,
        scheduler_add=_operator_scheduler_add,
        scheduler_cancel=_operator_scheduler_cancel,
        goal_list=_operator_goals_list,
        goal_clear=_operator_goals_clear,
        chat_commands=_operator_chat_commands,
        workflows_list=_operator_workflows_list,
        workflows_run=_operator_workflow_run,
        workflows_save=_operator_workflow_save,
        workflows_delete=_operator_workflow_delete,
        events_subscribe=_event_bus.subscribe,
        activity_list=_operator_activity_list,
        inbox_add=_operator_inbox_add,
        inbox_authorized=_inbox_authorized,
        inbox_list=_operator_inbox_list,
        inbox_deliver=_operator_inbox_deliver,
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
        # Checkpoint pruner — periodic sweep to keep the SQLite history DB bounded.
        if (
            STATE.checkpoint_path
            and STATE.graph_config is not None
            and STATE.graph_config.checkpoint_prune_interval_hours > 0
        ):
            import asyncio
            STATE.checkpoint_prune_task = asyncio.create_task(_checkpoint_prune_loop())

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

    # --- Chat API -----------------------------------------------------------
    class ChatRequest(PydanticBaseModel):
        message: str
        session_id: str = "api-default"

    @fastapi_app.post("/api/chat")
    async def _api_chat(req: ChatRequest):
        result = await chat(req.message, req.session_id)
        parts = [m["content"] for m in result if m.get("role") == "assistant" and m.get("content")]
        return {"response": "\n\n".join(parts), "messages": result}

    @fastapi_app.delete("/api/chat/sessions/{session_id}")
    async def _api_delete_session(session_id: str):
        """Retire a chat session: harvest its conversation into the knowledge
        base (if enabled), then purge its checkpoints. Called when the operator
        deletes a chat tab, so deleted conversations don't linger to the TTL —
        their substance lives on as searchable memory instead."""
        chunk_id = await _retire_thread(f"a2a:{session_id}")
        return {"deleted": True, "harvested": chunk_id is not None}

    # --- Goal mode API ------------------------------------------------------
    # Programmatic status/clear for a session's goal (setting is done via the
    # `/goal ...` control message through chat/A2A). Returns 404-style payloads
    # as plain JSON to keep the surface dependency-free.
    @fastapi_app.get("/api/goal/{session_id}")
    async def _api_goal_status(session_id: str):
        if STATE.goal_controller is None:
            return {"enabled": False, "goal": None}
        state = STATE.goal_controller.store.get(session_id)
        return {"enabled": True, "goal": state.to_dict() if state else None}

    @fastapi_app.delete("/api/goal/{session_id}")
    async def _api_goal_clear(session_id: str):
        if STATE.goal_controller is None:
            return {"enabled": False, "cleared": False}
        return {"enabled": True, "cleared": STATE.goal_controller.store.clear(session_id)}

    # --- Health / readiness (ADR 0010) -------------------------------------
    # Reflects whether the graph actually compiled — the only readiness signal
    # in the 'none' tier (no UI to eyeball). 503 until ready, for k8s probes.
    @fastapi_app.get("/healthz", include_in_schema=False)
    async def _healthz():
        from graph.config_io import is_setup_complete
        ready = STATE.graph is not None
        return JSONResponse(
            {
                "ok": ready,
                "graph_compiled": ready,
                "setup_complete": is_setup_complete(),
                "ui": ui,
                # Surface the active model so eval reports can be tagged with the
                # model under test without guessing (evals.runner auto-detects).
                "model": STATE.graph_config.model_name if STATE.graph_config else None,
            },
            status_code=200 if ready else 503,
        )

    # --- Playbooks (skills surface, ADR 0009) ------------------------------
    # Browse + manage the procedural-memory skill index (skills.db) the operator
    # was otherwise blind to. "Playbooks" is the operator-facing name for the
    # skill-v1 artifacts (disk = pinned SKILL.md, emitted = agent-learned).
    @fastapi_app.get("/api/playbooks")
    async def _api_playbooks():
        if STATE.skills_index is None:
            return {"enabled": False, "playbooks": []}
        try:
            skills = STATE.skills_index.all_skills()
        except Exception:  # noqa: BLE001 — never 500 the console
            log.exception("[playbooks] all_skills failed")
            return {"enabled": True, "playbooks": []}
        # Drop the (potentially large) prompt_template from the list payload;
        # the table only needs metadata. Sort pinned-first, then by confidence.
        out = [
            {k: v for k, v in s.items() if k != "prompt_template"}
            for s in skills
        ]
        out.sort(key=lambda s: (s.get("source") != "disk", -(s.get("confidence") or 0)))
        return {"enabled": True, "playbooks": out}

    @fastapi_app.delete("/api/playbooks/{skill_id}")
    async def _api_playbook_delete(skill_id: int):
        if STATE.skills_index is None:
            return {"enabled": False, "deleted": False}
        try:
            STATE.skills_index.delete_skill(skill_id)
            return {"enabled": True, "deleted": True}
        except Exception as exc:  # noqa: BLE001
            log.exception("[playbooks] delete failed")
            return {"enabled": True, "deleted": False, "error": str(exc)}

    # --- Knowledge store (ADR 0020) ----------------------------------------
    # Searchable view of the agent's knowledge base (knowledge/store.py, FTS5):
    # findings, daily-log entries, harvested sessions, operator notes — the same
    # store KnowledgeMiddleware queries before each turn. An empty ``q`` returns
    # the most-recent chunks (a browsable default); a non-empty ``q`` runs FTS5
    # search. Read-only; never 500s the console.
    def _knowledge_row(d: dict) -> dict:
        """Normalize a search()/list_chunks() row to the console's shape."""
        heading = d.get("heading") or ""
        content = d.get("content") or ""
        preview = d.get("preview") or ((heading + ": " if heading else "") + content)[:240]
        return {
            "id": d.get("id"),
            "heading": heading,
            "content": content,
            "preview": preview,
            "domain": d.get("domain") or "general",
            "source": d.get("source"),
            "source_type": d.get("source_type"),
            "finding_type": d.get("finding_type"),
            "created_at": d.get("created_at"),
        }

    @fastapi_app.get("/api/knowledge/search")
    async def _api_knowledge_search(q: str = "", k: int = 30, domain: str | None = None):
        if STATE.knowledge_store is None:
            return {"enabled": False, "query": q, "results": [], "stats": {}}
        results: list[dict] = []
        try:
            if q and q.strip():
                results = [_knowledge_row(r) for r in STATE.knowledge_store.search(q, k=k, domain=domain or None)]
            else:
                results = [_knowledge_row(c.as_dict()) for c in STATE.knowledge_store.list_chunks(domain=domain or None, limit=k)]
        except Exception:  # noqa: BLE001 — never 500 the console
            log.exception("[knowledge] search failed")
        try:
            stats = STATE.knowledge_store.stats()
        except Exception:  # noqa: BLE001
            stats = {}
        return {"enabled": True, "query": q, "results": results, "stats": stats}

    # --- Telemetry (ADR 0006 Slice 2) --------------------------------------
    # Per-turn cost/latency rollups from the local store. Powers the operator
    # console's cost/latency surface (Slice 3) and ad-hoc "what's expensive"
    # queries. Read-only; returns {enabled:false} when the store is off.
    @fastapi_app.get("/api/telemetry/summary")
    async def _api_telemetry_summary(since: str | None = None):
        if STATE.telemetry_store is None:
            return {"enabled": False, "summary": None}
        return {"enabled": True, "summary": STATE.telemetry_store.summary(since_iso=since)}

    @fastapi_app.get("/api/telemetry/recent")
    async def _api_telemetry_recent(limit: int = 50):
        if STATE.telemetry_store is None:
            return {"enabled": False, "turns": []}
        return {"enabled": True, "turns": STATE.telemetry_store.recent(limit=min(max(1, limit), 500))}

    @fastapi_app.get("/api/telemetry/insights")
    async def _api_telemetry_insights():
        # Advise-only flywheel signal (ADR 0006 Slice 4): flag outlier turns +
        # prove the levers we can measure from the per-turn store. Read-only.
        if STATE.telemetry_store is None:
            return {"enabled": False, "insights": None}
        import pricing

        s = STATE.telemetry_store.summary()
        flagged = STATE.telemetry_store.outliers()
        # Cache lever (proven): estimated $ saved by prompt-cache reads, billed at
        # the dominant model's input rate (the per-turn store keeps no per-call
        # model breakdown of cache reads).
        by_model = s.get("by_model") or []
        dom_model = by_model[0]["model"] if by_model else ((STATE.graph_config.model_name if STATE.graph_config else "") or "")
        cache_saved = pricing.cache_read_savings_usd(dom_model, s.get("cache_read_input_tokens", 0))
        return {
            "enabled": True,
            "insights": {
                "turns": s.get("turns", 0),
                "flagged": flagged,
                "flagged_count": len(flagged),
                "levers": {
                    "cache": {
                        "hit_ratio": s.get("cache_hit_ratio", 0.0),
                        "read_tokens": s.get("cache_read_input_tokens", 0),
                        "est_savings_usd": cache_saved,
                    },
                    "routing": {"by_model": by_model},
                    "success_rate": s.get("success_rate", 0.0),
                },
                # Every optimization lever is now measured: routing per-turn
                # (actual models on each row); tool deferral + compaction live via
                # Prometheus (*_llm_tools_deferred_total, *_compactions_total).
                "unproven_levers": [],
            },
        }

    # --- Live config / SOUL editing ----------------------------------------
    # GET returns the current config + persona so external clients (the
    # Gradio drawer is one; curl is another) can mirror what's running.
    # POST accepts partial edits — pass only the sections you want to
    # change. Reload is automatic.
    class ConfigReloadRequest(PydanticBaseModel):
        config: dict | None = None
        soul: str | None = None

    @fastapi_app.get("/api/config")
    async def _api_get_config():
        from graph.config_io import config_to_dict, read_soul
        return {
            "config": config_to_dict(STATE.graph_config),
            "soul": read_soul(),
        }

    @fastapi_app.post("/api/config")
    async def _api_post_config(req: ConfigReloadRequest):
        # Offload off the event loop (#497) — the reload's graph compile is heavy
        # and would otherwise freeze the server for its duration.
        ok, messages = await asyncio.to_thread(
            _apply_settings_changes, config=req.config, soul=req.soul
        )
        return {"ok": ok, "messages": messages}

    class ModelsProbeRequest(PydanticBaseModel):
        api_base: str = ""
        api_key: str = ""
        # Only used by the connection test (a real completion needs a model);
        # the model-list probe ignores it. Blank falls back to the saved config.
        model: str = ""

    @fastapi_app.post("/api/config/models")
    async def _api_list_models(req: ModelsProbeRequest | None = None):
        """Fetch the gateway's model list.

        POST (body) not GET (query) so the caller's API key doesn't
        end up in browser history, reverse-proxy access logs, or the
        uvicorn request log. A blank body falls back to whatever key
        and base are stored in the current config — useful for the
        drawer's initial render where there's nothing to POST yet.
        """
        from graph.config_io import list_gateway_models

        body = req or ModelsProbeRequest()
        base = body.api_base or (STATE.graph_config.api_base if STATE.graph_config else "")
        key = body.api_key or (STATE.graph_config.api_key if STATE.graph_config else "")
        models, error = list_gateway_models(base, key)
        return {"models": models, "error": error}

    @fastapi_app.post("/api/config/test-model")
    async def _api_test_model(req: ModelsProbeRequest | None = None):
        """Verify the model can actually complete (the true auth check).

        Powers the wizard's + Settings' "Test connection" button. POST (body)
        so the key never lands in a URL/log. A blank field falls back to the
        saved config, so Settings can re-test the live agent with one click.
        Offloaded to a thread — a real completion is a blocking network call,
        and we never want the connection test to freeze the event loop.
        """
        from graph.config_io import validate_model_connection

        body = req or ModelsProbeRequest()
        base = body.api_base or (STATE.graph_config.api_base if STATE.graph_config else "")
        key = body.api_key or (STATE.graph_config.api_key if STATE.graph_config else "")
        model = body.model or (STATE.graph_config.model_name if STATE.graph_config else "")
        ok, error = await asyncio.to_thread(validate_model_connection, base, key, model)
        return {"ok": ok, "error": error}

    # `/api/config/test-discord` (discord plugin) and `/api/config/google/status`
    # + `/connect` (google plugin) are now mounted by their plugin routers (ADR
    # 0018/0019), at the same paths — the console Test/Connect buttons are
    # unchanged.

    # --- Setup wizard state -------------------------------------------------
    @fastapi_app.get("/api/config/setup-status")
    async def _api_setup_status():
        from graph.config_io import is_setup_complete, list_soul_presets
        return {
            "setup_complete": is_setup_complete(),
            "presets": list_soul_presets(),
        }

    @fastapi_app.post("/api/config/setup")
    async def _api_finish_setup(req: ConfigReloadRequest):
        """Terminal wizard action over HTTP. Same semantics as the
        drawer's ``finish_setup`` callback — writes everything, marks
        setup complete, optionally installs autostart, then reloads.
        """
        callbacks = _build_settings_callbacks()
        # Offload off the event loop (#497) — finish-setup validates the model +
        # compiles the graph, both heavy; running inline froze the server ~30s.
        ok, msg = await asyncio.to_thread(callbacks["finish_setup"], req.config, req.soul)
        return {"ok": ok, "message": msg}

    @fastapi_app.post("/api/config/reset-setup")
    async def _api_reset_setup():
        from graph.config_io import reset_setup
        reset_setup()
        return {"ok": True, "message": "setup marker removed"}

    @fastapi_app.get("/api/config/presets/{name}")
    async def _api_read_preset(name: str):
        from graph.config_io import read_soul_preset
        return {"name": name, "content": read_soul_preset(name)}

    # --- Generic settings (schema-driven UI) --------------------------------
    @fastapi_app.get("/api/settings/schema")
    async def _api_settings_schema():
        """All editable settings, grouped, with current values + metadata
        (type, default, restart-vs-hot-reload, description). Drives the
        operator console's Settings surface."""
        from graph.config_io import list_gateway_models
        from graph.settings_schema import build_schema

        models: list[str] = []
        if STATE.graph_config is not None:
            models, _ = list_gateway_models(STATE.graph_config.api_base, STATE.graph_config.api_key)
        return {"groups": build_schema(STATE.graph_config, model_options=models)}

    class SettingsUpdateRequest(PydanticBaseModel):
        updates: dict[str, Any] = {}

    @fastapi_app.post("/api/settings")
    async def _api_save_settings(req: SettingsUpdateRequest):
        """Validate a flat {key: value} payload, persist it to YAML (secrets
        split out), and hot-reload the graph. Returns any keys that need a
        full process restart to take effect."""
        from graph.settings_schema import nest_updates, restart_keys, validate_flat

        ok, err = validate_flat(req.updates)
        if not ok:
            return {"ok": False, "messages": [f"validation: {err}"], "restart_required": []}
        # Offload off the event loop (#497) — a model change recompiles the graph.
        ok, messages = await asyncio.to_thread(
            _apply_settings_changes, config=nest_updates(req.updates)
        )
        return {"ok": ok, "messages": messages, "restart_required": restart_keys(req.updates)}

    # --- OpenAI-compatible chat completions --------------------------------
    # Lets this agent be registered as a model in the LiteLLM gateway /
    # OpenWebUI without any protocol adapter.
    @fastapi_app.post("/v1/chat/completions")
    async def _openai_chat_completions(req: dict):
        messages = req.get("messages", [])
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if not user_msgs:
            return {"error": "No user message provided"}, 400
        prompt = user_msgs[-1].get("content", "")
        session_id = f"openai-compat-{int(time.time())}"
        stream = req.get("stream", False)

        result = await chat(prompt, session_id)
        parts = [m["content"] for m in result if m.get("role") == "assistant" and m.get("content")]
        content = "\n\n".join(parts)
        created = int(time.time())
        completion_id = f"{agent_name()}-{session_id}"

        if stream:
            async def _stream():
                chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": agent_name(),
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                done_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": agent_name(),
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(done_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(_stream(), media_type="text/event-stream")

        return {
            "id": completion_id, "object": "chat.completion",
            "created": created, "model": agent_name(),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    @fastapi_app.get("/v1/models")
    async def _openai_models():
        return {
            "object": "list",
            "data": [{"id": agent_name(), "object": "model", "created": 1774600000, "owned_by": "protolabs"}],
        }

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

        fastapi_app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # --- Mount Gradio at root (only the 'full' tier) ------------------------
    if ui == "full" and blocks is not None:
        import gradio as gr

        app = gr.mount_gradio_app(
            fastapi_app, blocks, path="/",
            footer_links=[],
            favicon_path=str(static_dir / "favicon.svg") if (static_dir / "favicon.svg").exists() else None,
        )
        log.info("Starting %s (ui=full) on http://0.0.0.0:%d", agent_name(), args.port)
    else:
        app = fastapi_app
        log.info("Starting %s (ui=%s) on http://0.0.0.0:%d", agent_name(), ui, args.port)

    # Don't outlive the launcher. When run as a desktop sidecar the Tauri shell
    # sets PROTOAGENT_PARENT_PID; a PyInstaller-frozen onefile runs as a
    # bootloader + child, so the shell killing the bootloader can leave this
    # server orphaned (holding its port). Poll the launcher and exit if it dies.
    _install_parent_death_watchdog()

    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    _main()
