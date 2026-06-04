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

_graph = None          # LangGraph compiled graph
_graph_config = None   # LangGraphConfig
_checkpointer = None   # checkpointer for session persistence (sqlite or memory)
_checkpoint_path = None  # resolved sqlite path when persistent (for the pruner)
_checkpoint_prune_task = None  # background prune loop handle
_knowledge_store = None  # KnowledgeStore bound into the active graph, or None.
_skills_index = None     # SkillsIndex (human-authored SKILL.md store), or None.
_workflow_registry = None  # WorkflowRegistry (declarative workflow recipes), or None.
_telemetry_store = None  # TelemetryStore (per-turn cost/latency rollups, ADR 0006), or None.
_inbox_store = None    # InboxStore — durable inbound inbox (ADR 0003), or None.
_beads_store = None    # BeadsStore — in-process issue tracker (Sprint B), or None.
_storm_guard = None    # StormGuard for the now→Activity fire path (ADR 0003).
_mcp_clients = []        # Live MultiServerMCPClient handles (kept alive for reconnect).
_mcp_tools = []          # MCP-server tools appended to the active graph.
_mcp_meta = []           # Per-server {name, transport, tool_count} for runtime status.
_plugin_tools = []       # Tools contributed by enabled plugins.
_plugin_skill_dirs = []  # SKILL.md dirs bundled by enabled plugins.
_plugin_routers = []     # FastAPI routers contributed by plugins (ADR 0018) —
                         # mounted ONCE at init; not hot-reloaded.
_plugin_surfaces = []    # Lifecycle surfaces (start/stop) from plugins (ADR 0018)
                         # — started in the startup hook, stopped on shutdown.
_plugin_surface_handles = []  # Started surfaces ({name, stop, handle}) for shutdown.
_plugin_meta = []        # Per-plugin {id, name, enabled, loaded, tools, skills, ...} for status.
_active_port = 7870    # populated by _main() — the port this process is actually bound to.
                       # Read by the autostart installer so the LaunchAgent reboots
                       # on the same port the operator launched with, not the default.
_scheduler = None      # SchedulerBackend (LocalScheduler or WorkstaceanScheduler).
                       # Constructed at init, started on FastAPI startup, stopped
                       # on shutdown. Lifecycle is hooked in _main() so the
                       # polling coroutine doesn't leak on server reload.
_cache_warmer = None   # Optional CacheWarmer (off by default). Same start/stop
                       # lifecycle as _scheduler; keeps the prompt cache warm.
_goal_controller = None  # Optional GoalController (goal mode). Parses /goal
                         # control messages and runs the goal-completion loop.
_discord_task = None     # The inbound Discord gateway task (ADR 0015/0016).
                         # Held so a config change (Settings/wizard) can stop +
                         # restart it live, not just at process boot.
_main_loop = None        # The server's event loop, captured at startup (#497).
                         # A config reload's heavy graph compile is offloaded to a
                         # worker thread so it no longer freezes the loop; the
                         # scheduler/Discord restart that follows still has to run
                         # ON the loop, so the worker thread schedules it here via
                         # run_coroutine_threadsafe instead of get_running_loop()
                         # (which would silently no-op in the thread — the trap).

_event_bus = EventBus()  # Server→client SSE push channel (ADR 0003). Process-
                         # lifetime singleton; producers publish, /api/events
                         # streams to connected consoles.


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
    return str(Path(__file__).parent.resolve())


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


def _init_langgraph_agent(headless_setup: bool = False):
    """Initialize the LangGraph backend — setup-aware.

    ``headless_setup`` (ADR 0010): when True (the ``none`` UI tier or
    ``PROTOAGENT_HEADLESS_SETUP``), there is no wizard to finish setup, so a
    validated config auto-completes setup; an invalid one fails fast (SystemExit)
    rather than silently serving a dead graph.

    Always loads the config + checkpointer so the wizard and drawer
    can introspect what's on disk. The compiled graph is only built
    when the setup wizard has been completed (``.setup-complete``
    marker present). This lets the server boot cleanly on a fresh
    clone with no model credentials — the wizard drives the user to
    provide them, then triggers a reload.
    """
    global _graph, _graph_config, _checkpointer, _knowledge_store, _skills_index
    global _workflow_registry
    global _mcp_clients, _mcp_tools, _mcp_meta
    global _plugin_tools, _plugin_skill_dirs, _plugin_meta

    from graph.config import LangGraphConfig
    from graph.config_io import (
        CONFIG_YAML_PATH,
        ensure_live_config,
        is_setup_complete,
        mark_setup_complete,
        validate_for_headless,
    )

    # Seed the untracked live config from the .example template on first run.
    # CONFIG_YAML_PATH honors PROTOAGENT_CONFIG_DIR (the desktop sidecar points
    # it at per-user app-data), so load through it rather than a fixed path.
    ensure_live_config()
    _graph_config = LangGraphConfig.from_yaml(CONFIG_YAML_PATH)
    # Fork tool denylist (config ``tools.disabled``) — applied before any
    # get_all_tools() call so dropped tools never reach the graph.
    from tools.lg_tools import set_disabled_tools
    set_disabled_tools(_graph_config.tools_disabled)
    # Egress allowlist (ADR 0008): deny-by-default outbound hosts for fetch_url.
    import egress
    egress.set_allowed_hosts(_graph_config.egress_allowed_hosts)
    # Multi-instance scoping (ADR 0004): seed PROTOAGENT_INSTANCE from config so
    # every store (incl. the env-reading knowledge/scheduler/memory modules) nests
    # under the same id. Opt-in — empty config.instance_id leaves paths unchanged.
    # Set before any store is built or the memory middleware is imported.
    _seed_instance_env(_graph_config)
    # Conversation checkpointer: durable SQLite when a path is configured (chat
    # history survives restarts), else in-memory. Bound into the graph at
    # compile time below — a checkpointer in the invoke config is ignored.
    _checkpointer = _build_checkpointer(_graph_config)

    if not is_setup_complete():
        if headless_setup:
            # No wizard in this tier — auto-complete from a validated config,
            # else fail fast (ADR 0010) rather than serve a dead graph.
            ok, reason = validate_for_headless(_graph_config)
            if not ok:
                log.error("Headless setup cannot complete: %s", reason)
                raise SystemExit(2)
            mark_setup_complete()
            log.info("Headless setup auto-completed from a validated config.")
        else:
            _graph = None
            _knowledge_store = None
            log.info(
                "Setup wizard has not been completed — graph not compiled. "
                "Open the UI to finish setup (or run headless: --ui none / --setup).",
            )
            return

    from graph.agent import create_agent_graph
    from tools.lg_tools import get_all_tools

    # Construct the default KnowledgeStore so memory tools (memory_ingest,
    # memory_recall, daily_log) and KnowledgeMiddleware have something to
    # bind to. Forks that don't want a store can set
    # ``middleware.knowledge: false`` and remove the memory tools from
    # the worker subagent — the store is still cheap to construct.
    _knowledge_store = _build_knowledge_store(_graph_config)

    # Scheduler — local sqlite by default, swaps to a WorkstaceanScheduler
    # automatically when WORKSTACEAN_API_BASE + WORKSTACEAN_API_KEY env
    # vars are set. Both backends share the same agent-tool surface
    # (schedule_task / list_schedules / cancel_schedule).
    global _scheduler
    _scheduler = _build_scheduler(_graph_config)

    # MCP — external Model Context Protocol servers; their tools become agent
    # tools (namespaced <server>__<tool>). Off unless mcp.enabled.
    _mcp_clients, _mcp_tools, _mcp_meta = _build_mcp(_graph_config)

    # Plugins — drop-in packages (tools + bundled skills). Loaded after the
    # core + MCP tools so plugin tools that would shadow them are skipped.
    _plugins = _build_plugins(
        _graph_config,
        existing_tools=get_all_tools(_knowledge_store, scheduler=_scheduler) + _mcp_tools,
    )
    _plugin_tools, _plugin_skill_dirs, _plugin_meta = (
        _plugins.tools, _plugins.skill_dirs, _plugins.meta,
    )
    # Surfaces / routes / subagents (ADR 0018). Routers + surfaces are captured
    # here and consumed once by _main (mount) + the startup hook (start) — they
    # don't hot-reload. Subagents register into SUBAGENT_REGISTRY before the graph
    # build below so the first compile (and every reload) can delegate to them.
    global _plugin_routers, _plugin_surfaces
    _plugin_routers, _plugin_surfaces = _plugins.routers, _plugins.surfaces
    _register_plugin_subagents(_plugins.subagents)

    # Skills — human-authored SKILL.md folders (bundle + live + plugin-bundled)
    # seeded into the FTS index; KnowledgeMiddleware retrieves + injects them.
    _skills_index = _build_skills_index(_graph_config, extra_skill_dirs=_plugin_skill_dirs)

    _workflow_registry = _build_workflow_registry(_graph_config)

    global _inbox_store, _storm_guard, _beads_store
    _inbox_store = _build_inbox_store(_graph_config)
    from beads import BeadsStore
    if _beads_store is None:  # may have been created early (pre-setup) for the routes
        _beads_store = BeadsStore()  # in-process issue tracker (Sprint B), instance-scoped
    if _storm_guard is None:
        from inbox import StormGuard
        _storm_guard = StormGuard()

    _graph = create_agent_graph(
        _graph_config, knowledge_store=_knowledge_store, scheduler=_scheduler,
        skills_index=_skills_index, extra_tools=_mcp_tools + _plugin_tools,
        checkpointer=_checkpointer, workflow_registry=_workflow_registry,
        inbox_store=_inbox_store, beads_store=_beads_store,
    )

    # Cache-warming heartbeat — off by default; start() no-ops unless enabled
    # for an Anthropic-family model (see graph/cache_warmer.py).
    global _cache_warmer
    from graph.cache_warmer import CacheWarmer
    _cache_warmer = CacheWarmer(
        _graph_config, knowledge_store=_knowledge_store, scheduler=_scheduler,
    )

    # Goal mode — parses /goal control messages and runs the goal-completion
    # loop around graph invocations. Machinery only; no goal is active until set.
    global _goal_controller
    if _graph_config.goal_enabled:
        from graph.goals import GoalController, GoalStore
        _goal_controller = GoalController(_graph_config, GoalStore())
    else:
        _goal_controller = None
    log.info(
        "LangGraph agent initialized (model: %s, knowledge_db: %s, scheduler: %s)",
        _graph_config.model_name,
        getattr(_knowledge_store, "path", "(disabled)"),
        getattr(_scheduler, "name", "disabled"),
    )


def _build_knowledge_store(config):
    """Return a ``KnowledgeStore`` bound to the configured DB path.

    Best-effort: any sqlite-level failure is logged and the store
    falls back to ``~/.protoagent/knowledge/agent.db`` automatically
    (see ``knowledge.store._resolve_path``). Returns ``None`` only when
    knowledge is disabled in config — kept as a separate code path so
    forks can audit when the agent is running KB-less.
    """
    if not getattr(config, "knowledge_middleware", True):
        return None
    try:
        from knowledge import KnowledgeStore
        return KnowledgeStore(db_path=config.knowledge_db_path)
    except Exception as exc:
        log.warning("[server] knowledge store init failed: %s; running KB-less", exc)
        return None


def _build_skills_index(config, extra_skill_dirs=None):
    """Return a ``SkillsIndex`` seeded from on-disk ``SKILL.md`` folders, or None.

    ``extra_skill_dirs`` are additional roots (e.g. skill dirs bundled by
    enabled plugins) seeded alongside the bundle + live skill roots.

    Resolves a writable DB path (the configured ``/sandbox/skills.db`` →
    ``~/.protoagent/skills.db`` fallback, mirroring the knowledge store), then
    rebuilds the index from the bundled example skills (``config/skills``) plus
    the operator's drop-in skills (``<config_dir>/skills`` or ``skills.dir``).
    Best-effort: any failure logs and returns None so a bad skill never blocks
    boot.
    """
    if not getattr(config, "skills_enabled", True):
        return None
    try:
        from pathlib import Path

        from graph.config_io import _BUNDLE_CONFIG_DIR, _live_config_dir
        from graph.skills.index import SkillsIndex
        from graph.skills.loader import seed_skills_index

        db_path = _resolve_skills_db(config.skills_db_path)
        index = SkillsIndex(db_path=db_path)

        live_root = Path(config.skills_dir).expanduser() if config.skills_dir else (
            _live_config_dir() / "skills"
        )
        roots = [_BUNDLE_CONFIG_DIR / "skills", live_root]  # bundle first, live overrides
        roots.extend(Path(d) for d in (extra_skill_dirs or []))  # plugin-bundled skills
        count = seed_skills_index(index, roots)
        log.info("[skills] indexed %d SKILL.md skill(s) into %s", count, db_path)
        return index
    except Exception as exc:  # noqa: BLE001 — skills are optional, never fatal
        log.warning("[skills] index init failed: %s; running without SKILL.md skills", exc)
        return None


def _build_mcp(config):
    """Discover tools from configured MCP servers. Returns (clients, tools, meta).

    Best-effort and per-server isolated (see tools/mcp_tools.build_mcp_tools):
    a bad/unreachable server is logged and skipped, never fatal. Returns empty
    lists when MCP is disabled.
    """
    try:
        from tools.mcp_tools import build_mcp_tools

        clients, tools, meta = build_mcp_tools(config)
        if tools:
            log.info("[mcp] %d tool(s) from %d server(s)", len(tools), len(meta))
        return clients, tools, meta
    except Exception as exc:  # noqa: BLE001 — MCP is optional, never fatal
        log.warning("[mcp] init failed: %s; running without MCP tools", exc)
        return [], [], []


_plugin_subagent_names: set[str] = set()


def _register_plugin_subagents(subagents) -> None:
    """Add plugin-contributed SubagentConfigs to SUBAGENT_REGISTRY (ADR 0018).

    Idempotent by name (re-registering a plugin's own subagent on a later call is
    fine) but won't let a plugin shadow a built-in subagent (logged + skipped).
    """
    if not subagents:
        return
    try:
        from graph.subagents.config import SUBAGENT_REGISTRY
    except Exception:  # noqa: BLE001
        log.warning("[plugins] subagent registry unavailable; skipping plugin subagents")
        return
    for cfg in subagents:
        name = getattr(cfg, "name", None)
        if not name:
            continue
        if name in SUBAGENT_REGISTRY and name not in _plugin_subagent_names:
            log.warning("[plugins] subagent %r collides with a built-in — skipped", name)
            continue
        SUBAGENT_REGISTRY[name] = cfg
        _plugin_subagent_names.add(name)
        log.info("[plugins] registered subagent: %s", name)


def _build_plugins(config, existing_tools=None):
    """Load enabled drop-in plugins. Returns the PluginLoadResult (tools +
    bundled skill dirs + per-plugin meta). Best-effort — never fatal.

    ``existing_tools`` (core + MCP tools already assembled) are passed so a
    plugin tool that would shadow them is skipped.
    """
    try:
        from graph.plugins import load_plugins

        core_names = {getattr(t, "name", None) for t in (existing_tools or [])}
        core_names.discard(None)
        result = load_plugins(config, core_tool_names=core_names)
        loaded = [m for m in result.meta if m.get("loaded")]
        if loaded:
            log.info("[plugins] loaded %d plugin(s): %s",
                     len(loaded), ", ".join(m["id"] for m in loaded))
        return result
    except Exception as exc:  # noqa: BLE001 — plugins are optional, never fatal
        log.warning("[plugins] init failed: %s; running without plugins", exc)
        from graph.plugins.loader import PluginLoadResult

        return PluginLoadResult()


def _seed_instance_env(config) -> None:
    """Seed PROTOAGENT_INSTANCE from config.instance_id (ADR 0004), unless the
    env is already set (env wins). Opt-in: no id → no scoping → legacy paths."""
    if os.environ.get("PROTOAGENT_INSTANCE", "").strip():
        return
    iid = (getattr(config, "instance_id", "") or "").strip()
    if iid:
        os.environ["PROTOAGENT_INSTANCE"] = iid
        log.info("[instance] data scoped to instance id %r (ADR 0004)", iid)


def _resolve_checkpoint_db(configured: str) -> str:
    """Pick a writable checkpoint DB path; fall back to ~/.protoagent when the
    configured dir (default /sandbox) isn't creatable (e.g. local dev)."""
    import os
    from pathlib import Path

    candidate = Path(configured).expanduser()
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if os.access(candidate.parent, os.W_OK):
            scoped = scope_leaf(candidate)
            scoped.parent.mkdir(parents=True, exist_ok=True)
            return str(scoped)
    except OSError:
        pass
    fallback = scope_leaf(Path.home() / ".protoagent" / "checkpoints.db")
    fallback.parent.mkdir(parents=True, exist_ok=True)
    return str(fallback)


def _build_checkpointer(config):
    """Durable SQLite checkpointer when ``checkpoint_db_path`` is set, else an
    in-memory saver (history cleared on restart). Falls back to in-memory if the
    SQLite saver can't be built so a bad path never blocks boot."""
    if not getattr(config, "checkpoint_db_path", ""):
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()
    try:
        from graph.checkpointer import build_sqlite_checkpointer
        global _checkpoint_path
        path = _resolve_checkpoint_db(config.checkpoint_db_path)
        saver = build_sqlite_checkpointer(path)
        _checkpoint_path = path
        log.info("[checkpointer] persistent chat history at %s", path)
        return saver
    except Exception:
        log.exception("[checkpointer] SQLite init failed; using in-memory (history won't persist)")
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()


async def _checkpoint_prune_loop() -> None:
    """Periodically trim the SQLite checkpoint DB (per-thread cap + age TTL).

    Reads the path + knobs from the live globals each pass so a config reload
    takes effect without restarting the loop. Failures are logged, never fatal.
    """
    import asyncio

    from graph.checkpoint_prune import find_aged_threads, prune_checkpoints

    await asyncio.sleep(60)  # let boot settle before the first sweep
    while True:
        cfg = _graph_config
        path = _checkpoint_path
        interval_h = getattr(cfg, "checkpoint_prune_interval_hours", 0) if cfg else 0
        if path and cfg and interval_h > 0:
            try:
                max_age = (
                    cfg.checkpoint_max_age_days * 86400 if cfg.checkpoint_max_age_days else None
                )
                harvest = bool(
                    max_age
                    and cfg.checkpoint_harvest_enabled
                    and _knowledge_store is not None
                    and _checkpointer is not None
                )
                if harvest:
                    # Summarize each aged thread into knowledge, then drop it —
                    # past conversations stay searchable, raw checkpoints freed.
                    for thread_id in await asyncio.to_thread(find_aged_threads, path, max_age):
                        await _retire_thread(thread_id)
                # Per-thread cap on the survivors (SQL age-TTL is the fallback
                # delete path when harvesting is off).
                res = await asyncio.to_thread(
                    prune_checkpoints,
                    path,
                    keep_per_thread=cfg.checkpoint_keep_per_thread,
                    max_age_seconds=(None if harvest else max_age),
                )
                if res["threads_deleted"] or res["checkpoints_deleted"]:
                    log.info(
                        "[checkpoint-prune] removed %d idle thread(s), %d old checkpoint(s)",
                        res["threads_deleted"], res["checkpoints_deleted"],
                    )
            except Exception:
                log.exception("[checkpoint-prune] sweep failed")
        await asyncio.sleep(max(1, interval_h) * 3600)


async def _retire_thread(thread_id: str) -> str | None:
    """Harvest a thread to the knowledge base (best-effort) then delete its
    checkpoints. Shared by the prune sweep and explicit tab deletion. Returns
    the harvested knowledge chunk id, if any."""
    import asyncio

    from graph.checkpoint_prune import delete_thread

    chunk_id = None
    if _graph_config is not None and getattr(_graph_config, "checkpoint_harvest_enabled", False):
        from graph.conversation_harvest import harvest_thread
        chunk_id = await harvest_thread(
            thread_id,
            checkpointer=_checkpointer,
            knowledge_store=_knowledge_store,
            config=_graph_config,
        )
    if _checkpoint_path:
        await asyncio.to_thread(delete_thread, _checkpoint_path, thread_id)
    elif _checkpointer is not None and hasattr(_checkpointer, "delete_thread"):
        try:
            _checkpointer.delete_thread(thread_id)
        except Exception:
            log.exception("[retire] in-memory delete_thread failed for %s", thread_id)
    return chunk_id


def _build_inbox_store(config):
    """Durable inbound inbox (ADR 0003). Path resolves like the other stores
    (/sandbox → ~/.protoagent fallback), namespaced by agent name."""
    from inbox import InboxStore

    name = re.sub(r"[^a-zA-Z0-9._-]", "_", agent_name()) or "agent"
    configured = scope_leaf(Path(getattr(config, "inbox_db_path", "") or "/sandbox/inbox") / f"{name}.db")
    try:
        configured.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(configured.parent, os.W_OK):
            raise OSError
        path = str(configured)
    except OSError:
        fallback = scope_leaf(Path.home() / ".protoagent" / "inbox" / f"{name}.db")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        path = str(fallback)
    try:
        return InboxStore(path)
    except Exception:
        log.exception("[inbox] failed to build store at %s; inbox disabled", path)
        return None


def _build_telemetry_store(config):
    """Local per-turn telemetry store (ADR 0006 Slice 2). Path resolves like the
    other stores (/sandbox → ~/.protoagent fallback) and is instance-scoped
    (ADR 0004). Off when ``telemetry.enabled`` is false; best-effort otherwise."""
    if not getattr(config, "telemetry_enabled", True):
        return None
    from telemetry_store import TelemetryStore

    configured = scope_leaf(Path(getattr(config, "telemetry_db_path", "") or "/sandbox/telemetry.db"))
    try:
        configured.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(configured.parent, os.W_OK):
            raise OSError
        path = str(configured)
    except OSError:
        fallback = scope_leaf(Path.home() / ".protoagent" / "telemetry.db")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        path = str(fallback)
    try:
        store = TelemetryStore(path)
        log.info("[telemetry] store ready at %s", path)
        return store
    except Exception:
        log.exception("[telemetry] failed to build store at %s; telemetry disabled", path)
        return None


def _build_workflow_registry(config):
    """Load workflow recipes (ADR 0002) from the bundled repo ``workflows/`` dir
    plus a writable dir (user/agent-emitted). Best-effort; never blocks boot."""
    if not getattr(config, "workflows_enabled", True):
        return None
    try:
        import os
        from pathlib import Path

        from graph.workflows.registry import WorkflowRegistry

        dirs: list[str] = []
        bundled = Path(__file__).resolve().parent / "workflows"
        if bundled.is_dir():
            dirs.append(str(bundled))
        # Writable dir for user / agent-emitted recipes (same fallback shape).
        writable = scope_leaf(Path(config.workflow_dir).expanduser())
        try:
            writable.mkdir(parents=True, exist_ok=True)
            if not os.access(writable, os.W_OK):
                raise OSError
        except OSError:
            writable = scope_leaf(Path.home() / ".protoagent" / "workflows")
            writable.mkdir(parents=True, exist_ok=True)
        dirs.append(str(writable))
        return WorkflowRegistry(dirs, writable_dir=str(writable))
    except Exception:
        log.exception("[workflows] registry init failed; running without workflows")
        return None


def _resolve_skills_db(configured: str) -> str:
    """Pick a writable skills DB path; fall back to ~/.protoagent when the
    configured dir (default /sandbox) isn't creatable — same idea as the
    knowledge store's ``_resolve_path``."""
    import os
    from pathlib import Path

    candidate = Path(configured)
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if os.access(candidate.parent, os.W_OK):
            scoped = scope_leaf(candidate)
            scoped.parent.mkdir(parents=True, exist_ok=True)
            return str(scoped)
    except OSError:
        pass
    fallback = scope_leaf(Path.home() / ".protoagent" / "skills.db")
    fallback.parent.mkdir(parents=True, exist_ok=True)
    return str(fallback)


def _run_on_server_loop(make_coro, what: str) -> None:
    """Fire-and-forget a coroutine onto the server's event loop.

    Works whether we're called **on** the loop (a direct, on-loop reload) or
    **from a worker thread** (the reload offloaded off the loop, #497). In the
    thread case ``get_running_loop()`` raises, and the old code logged + dropped
    the coroutine — silently killing the scheduler/briefing on every offloaded
    reload (the trap). We instead schedule it on the captured ``_main_loop`` via
    ``run_coroutine_threadsafe``. ``make_coro`` is a zero-arg factory so the
    coroutine is only created once we have a loop to run it on (no
    "coroutine was never awaited" leak when none is available).
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        try:
            loop.create_task(make_coro())
        except Exception:
            log.exception("[reload] %s failed", what)
        return

    if _main_loop is not None and _main_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(make_coro(), _main_loop)
        except Exception:
            log.exception("[reload] %s failed (threadsafe)", what)
        return

    log.warning("[reload] no event loop available; %s deferred to next process boot", what)


def _start_scheduler_async(backend: "SchedulerBackend") -> None:
    """Start the scheduler on the server loop (see :func:`_run_on_server_loop`)."""
    _run_on_server_loop(lambda: backend.start(), "scheduler start")


def _stop_scheduler_async(backend: "SchedulerBackend") -> None:
    """Stop the scheduler on the server loop (used when the toggle flips off)."""
    _run_on_server_loop(lambda: backend.stop(), "scheduler stop")


def _build_scheduler(config) -> "SchedulerBackend | None":
    """Return the active scheduler backend, or ``None`` when disabled.

    **The bundled ``LocalScheduler`` (sqlite) is the default.** The remote
    ``WorkstaceanScheduler`` is **opt-in**: it's used only when
    ``SCHEDULER_BACKEND=workstacean`` is set *and* ``WORKSTACEAN_API_BASE`` +
    ``WORKSTACEAN_API_KEY`` are present. Having the Workstacean env vars alone
    no longer switches the backend — local stays the default unless explicitly
    opted in.

    Returns ``None`` when explicitly disabled via ``SCHEDULER_DISABLED=1``
    so a fork can ship without a scheduler at all.

    The agent's auth token + api-key are passed into the local backend
    so its self-invocation HTTP call can pass through bearer / X-API-Key
    auth — the scheduler hits the same A2A endpoint as a real caller.
    """
    # Two opt-out paths, in priority order:
    # 1. ``middleware.scheduler: false`` in YAML (drawer / wizard).
    #    This is the canonical opt-out — symmetric with
    #    ``middleware.knowledge`` / ``middleware.memory``.
    # 2. ``SCHEDULER_DISABLED=1`` env var. Runtime escape hatch for
    #    fleet operators who need to kill the scheduler without
    #    editing config (e.g. emergency rollback).
    if not getattr(config, "scheduler_enabled", True):
        log.info("[server] scheduler disabled via middleware.scheduler config")
        return None
    if os.environ.get("SCHEDULER_DISABLED", "").lower() in ("1", "true", "yes"):
        log.info("[server] scheduler disabled via SCHEDULER_DISABLED env")
        return None

    name = agent_name()
    # Workstacean is opt-in: require an explicit SCHEDULER_BACKEND=workstacean,
    # not merely the presence of the API env vars (default stays local).
    workstacean_opt_in = os.environ.get("SCHEDULER_BACKEND", "").strip().lower() == "workstacean"
    workstacean_base = os.environ.get("WORKSTACEAN_API_BASE", "").strip()
    workstacean_key = os.environ.get("WORKSTACEAN_API_KEY", "").strip()
    if workstacean_opt_in:
        if workstacean_base and workstacean_key:
            try:
                from scheduler import WorkstaceanScheduler
                return WorkstaceanScheduler(
                    agent_name=name,
                    base_url=workstacean_base,
                    api_key=workstacean_key,
                    topic_prefix=os.environ.get("WORKSTACEAN_TOPIC_PREFIX") or None,
                )
            except Exception as exc:
                log.warning(
                    "[server] WorkstaceanScheduler init failed: %s; falling back to local",
                    exc,
                )
        else:
            log.warning(
                "[server] SCHEDULER_BACKEND=workstacean but WORKSTACEAN_API_BASE/"
                "API_KEY missing; falling back to local scheduler",
            )

    try:
        from scheduler import LocalScheduler
        invoke_url = os.environ.get(
            "SCHEDULER_INVOKE_URL",
            f"http://127.0.0.1:{_active_port}",
        )
        bearer = (config.auth_token or os.environ.get("A2A_AUTH_TOKEN", "")).strip()
        # The A2A handler reads X-API-Key from ``<AGENT_NAME_ENV>_API_KEY``
        # (server.py L893 — note: the env-derived name, NOT the wizard-set
        # ``identity.name``). Match that here so a wizard rename doesn't
        # break self-invocation auth.
        api_key_env = f"{AGENT_NAME_ENV.upper()}_API_KEY"
        api_key = os.environ.get(api_key_env, "").strip()
        return LocalScheduler(
            agent_name=name,
            invoke_url=invoke_url,
            api_key=api_key,
            bearer_token=bearer,
        )
    except Exception as exc:
        log.warning(
            "[server] LocalScheduler init failed: %s; running scheduler-less",
            exc,
        )
        return None


def _reload_langgraph_agent() -> tuple[bool, str]:
    """Rebuild the compiled graph from the latest config YAML.

    Called by the drawer's Save & Reload action and the
    ``/api/config/reload`` endpoint. Preserves the existing
    ``_checkpointer`` so active session threads stay addressable
    — a fresh MemorySaver would orphan every in-flight thread.

    Rebinding ``_graph`` is atomic in CPython; in-flight
    ``astream_events`` iterators hold their own reference to the
    prior graph and finish cleanly on the old instance.

    If the setup marker is absent this returns early without
    compiling — the wizard is still in front of the user, so there
    is nothing to hot-swap yet.
    """
    global _graph, _graph_config, _knowledge_store, _skills_index, _workflow_registry
    global _mcp_clients, _mcp_tools, _mcp_meta
    global _plugin_tools, _plugin_skill_dirs, _plugin_meta
    global _inbox_store

    from graph.agent import create_agent_graph
    from graph.config import LangGraphConfig
    from graph.config_io import CONFIG_YAML_PATH, ensure_live_config, is_setup_complete
    from tools.lg_tools import get_all_tools

    ensure_live_config()
    try:
        new_config = LangGraphConfig.from_yaml(CONFIG_YAML_PATH)
    except Exception as e:
        log.exception("[reload] config load failed")
        return False, f"config load failed: {e}"

    # Fork tool denylist — apply the new config's denylist before the rebuild's
    # get_all_tools() calls (live-reloadable like the rest of the config).
    from tools.lg_tools import set_disabled_tools
    set_disabled_tools(new_config.tools_disabled)

    # Build the graph FIRST (when setup is complete) — only commit
    # runtime state after the rebuild succeeds. Doing the swap first
    # would leave the process serving the prior compiled _graph under
    # fresh _graph_config + rotated bearer auth on failure — the
    # metrics / card / auth all de-sync from what's actually running.
    # Plan the scheduler swap *before* attempting the graph rebuild so
    # the polling loop isn't torn down (or a fresh one started) until
    # we know the rebuild will succeed. Three states:
    #
    # 1. Toggle flipped OFF, scheduler currently running → next graph
    #    uses None; we stop the running scheduler only after commit.
    # 2. Toggle ON, none running (first-run after setup completes) →
    #    construct now (cheap), start only after commit.
    # 3. Toggle ON, already running → reuse. Drawer saves don't tear
    #    down the polling loop.
    #
    # Env-driven config (WORKSTACEAN_API_BASE) only takes effect on
    # full process restart; the YAML toggle is the canonical
    # reload-time switch.
    global _scheduler
    scheduler_wanted = getattr(new_config, "scheduler_enabled", True)
    next_scheduler: "SchedulerBackend | None"
    pending_start: "SchedulerBackend | None" = None
    pending_stop: "SchedulerBackend | None" = None
    if not scheduler_wanted:
        next_scheduler = None
        pending_stop = _scheduler  # may be None — stopper is no-op then
    elif _scheduler is None:
        next_scheduler = _build_scheduler(new_config)
        pending_start = next_scheduler
    else:
        next_scheduler = _scheduler

    new_store = None
    new_skills = None
    new_mcp_clients, new_mcp_tools, new_mcp_meta = [], [], []
    new_plugin_tools, new_plugin_skill_dirs, new_plugin_meta = [], [], []
    if is_setup_complete():
        try:
            new_store = _build_knowledge_store(new_config)
            new_mcp_clients, new_mcp_tools, new_mcp_meta = _build_mcp(new_config)
            new_plugins = _build_plugins(
                new_config,
                existing_tools=get_all_tools(new_store, scheduler=next_scheduler) + new_mcp_tools,
            )
            new_plugin_tools = new_plugins.tools
            new_plugin_skill_dirs = new_plugins.skill_dirs
            new_plugin_meta = new_plugins.meta
            new_skills = _build_skills_index(new_config, extra_skill_dirs=new_plugin_skill_dirs)
            new_workflow_registry = _build_workflow_registry(new_config)
            new_inbox_store = _build_inbox_store(new_config)
            new_graph = create_agent_graph(
                new_config, knowledge_store=new_store, scheduler=next_scheduler,
                skills_index=new_skills, extra_tools=new_mcp_tools + new_plugin_tools,
                checkpointer=_checkpointer, workflow_registry=new_workflow_registry,
                inbox_store=new_inbox_store,
            )
        except Exception as e:
            log.exception("[reload] graph rebuild failed")
            # Scheduler state hasn't been committed yet — caller's
            # running scheduler keeps polling, no orphaned tasks.
            return False, f"graph rebuild failed: {e}"
    else:
        new_graph = None
        new_workflow_registry = None
        new_inbox_store = None

    # Capture the outgoing config before the swap so we can tell whether the
    # Discord surface needs a live reconnect (token/admin/enabled changed).
    old_config = _graph_config
    # Commit: config → A2A bearer → graph. All three reference the
    # same ``new_config`` so they stay consistent.
    _graph_config = new_config
    _knowledge_store = new_store
    _skills_index = new_skills
    _mcp_clients, _mcp_tools, _mcp_meta = new_mcp_clients, new_mcp_tools, new_mcp_meta
    _plugin_tools, _plugin_skill_dirs, _plugin_meta = (
        new_plugin_tools, new_plugin_skill_dirs, new_plugin_meta,
    )
    try:
        import egress

        egress.set_allowed_hosts(new_config.egress_allowed_hosts)  # live-reload (ADR 0008)
    except Exception:  # noqa: BLE001 — never block a reload on the egress update
        pass
    try:
        import a2a_auth

        a2a_auth.set_bearer_token(new_config.auth_token or None)
    except ImportError:
        # a2a_auth not yet imported (e.g. during early-boot reload before
        # _main wires routes) — harmless.
        pass
    _graph = new_graph
    _workflow_registry = new_workflow_registry
    _inbox_store = new_inbox_store
    # Commit the scheduler swap. start/stop are async — fire-and-forget
    # onto the active loop so reload stays sync. We've already verified
    # the graph rebuild succeeded; if start/stop fails we log but
    # don't roll back (the agent is already serving the new graph).
    _scheduler = next_scheduler
    if pending_stop is not None:
        _stop_scheduler_async(pending_stop)
    if pending_start is not None:
        _start_scheduler_async(pending_start)

    # Discord surface: reconnect live if its config changed (token / admin_ids /
    # enabled), so a Settings save or wizard finish applies without a restart.
    discord_changed = (
        old_config is None
        or old_config.discord_enabled != new_config.discord_enabled
        or old_config.discord_bot_token != new_config.discord_bot_token
        or list(old_config.discord_admin_ids) != list(new_config.discord_admin_ids)
    )
    if discord_changed:
        _restart_discord_async()

    # Plugin surfaces with a reload hook (ADR 0018/0019) — let them reconnect on a
    # config change (e.g. a migrated Discord/Google surface) without a restart.
    _reload_plugin_surfaces(new_config)

    if new_graph is None:
        log.info("[reload] setup not complete — config reloaded, graph not compiled")
        return True, "config reloaded • setup not complete"

    log.info("LangGraph agent reloaded (model: %s)", _graph_config.model_name)
    return True, f"reloaded • model={_graph_config.model_name}"


def _start_discord_surface() -> None:
    """(Re)start the inbound Discord gateway from the live config (ADR 0016).

    Start rule: the UI path starts only when ``discord.enabled`` is on AND a
    token is configured; the env path (Docker ``DISCORD_BOT_TOKEN``, no UI token)
    starts as before for back-compat. Injects token + admin_ids via
    ``configure`` so the bundled desktop app uses its per-user secret, not an
    ambient env var. Stores the task in ``_discord_task`` for live restart.
    """
    global _discord_task
    import os as _os

    from surfaces.discord import configure as _configure
    from surfaces.discord import start_in_background as _start_discord

    cfg_token = (_graph_config.discord_bot_token or "").strip() if _graph_config else ""
    env_token = (_os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    admin_ids = list(_graph_config.discord_admin_ids) if _graph_config else []
    enabled = bool(_graph_config.discord_enabled) if _graph_config else False

    _configure(cfg_token, admin_ids)

    # UI token requires the enabled toggle; a bare env token (no UI token) starts
    # for back-compat (Docker deploys that only set DISCORD_BOT_TOKEN).
    should_start = (bool(cfg_token) and enabled) or (not cfg_token and bool(env_token))
    if not should_start:
        log.info("[discord] gateway not started (enabled=%s, token=%s)",
                 enabled, "set" if (cfg_token or env_token) else "unset")
        return

    async def _discord_invoke(prompt: str, session_id: str) -> str:
        result = await chat(prompt, session_id)
        return "\n\n".join(
            m["content"] for m in result
            if m.get("role") == "assistant" and m.get("content")
        )

    # subscribe enables return-address delivery: reactive Activity-thread output
    # (scheduler/inbox/proactive) is forwarded to the operator's captured DM.
    _discord_task = _start_discord(
        _discord_invoke, publish=_event_bus.publish, subscribe=_event_bus.subscribe
    )


async def _plugin_agent_invoke(prompt: str, session_id: str) -> str:
    """Agent invoke exposed to plugin surfaces via the plugin host (ADR 0018) — a
    chat turn joined to its assistant text (mirrors the Discord surface invoker)."""
    result = await chat(prompt, session_id)
    return "\n\n".join(
        m["content"] for m in result
        if m.get("role") == "assistant" and m.get("content")
    )


def _populate_plugin_host() -> None:
    """Wire the plugin host (ADR 0018) — agent invoke + event bus — so a plugin
    surface/route can reach them. Called once in _main, before startup."""
    try:
        from graph.plugins.host import HOST

        HOST.invoke = _plugin_agent_invoke
        HOST.publish = _event_bus.publish
        HOST.subscribe = _event_bus.subscribe
    except Exception:  # noqa: BLE001
        log.exception("[plugins] failed to populate plugin host")


def _reload_plugin_surfaces(new_config) -> None:
    """Notify started plugin surfaces of a config change (ADR 0018/0019).

    Each surface that registered a ``reload`` callback gets it called with the new
    ``LangGraphConfig`` on the server loop, so a migrated Discord/Google-style
    surface can reconnect on a Settings save without a restart. Best-effort.
    """
    for h in _plugin_surface_handles:
        reload_cb = h.get("reload")
        if not callable(reload_cb):
            continue

        def _make(cb=reload_cb, name=h.get("name")):
            async def _run():
                try:
                    res = cb(new_config)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    log.exception("[plugins] surface %s reload failed", name)
            return _run()

        _run_on_server_loop(_make, f"surface reload ({h.get('name')})")


def _restart_discord_async() -> None:
    """Fire-and-forget: stop the running gateway, then start from new config.

    Scheduled on the server loop via :func:`_run_on_server_loop`, so it works
    from an offloaded (worker-thread) reload too — not just an on-loop one.
    """
    async def _swap() -> None:
        global _discord_task
        try:
            from surfaces.discord import stop as _discord_stop

            if _discord_task is not None:
                _discord_task.cancel()
                _discord_task = None
            await _discord_stop()
        except Exception:
            log.exception("[discord] stop during restart failed")
        try:
            _start_discord_surface()
        except Exception:
            log.exception("[discord] restart failed")

    _run_on_server_loop(_swap, "discord restart")


def _sync_autostart_with_config(config: dict | None) -> str | None:
    """Align the OS autostart artifact with the YAML runtime flag.

    Returns a short status string to append to the caller's message
    log, or ``None`` when the config doesn't touch the runtime
    section. Shared by ``finish_setup`` (wizard path) and
    ``_apply_settings_changes`` (drawer path) so both surfaces
    produce the same side effect when the checkbox flips.
    """
    if not (config and "runtime" in config):
        return None
    want = bool(config.get("runtime", {}).get("autostart_on_boot", False))

    try:
        from autostart import install_autostart, uninstall_autostart

        as_name = (
            config.get("identity", {}).get("name")
            or (_graph_config.identity_name if _graph_config else "")
            or "protoagent"
        )
        if want:
            ok, msg = install_autostart(agent_name=as_name, port=_active_port)
        else:
            ok, msg = uninstall_autostart(agent_name=as_name)
    except Exception as e:
        log.exception("[autostart] sync raised")
        return f"autostart failed: {e}"

    if not ok:
        log.warning("[autostart] sync failed: %s", msg)
    return f"autostart: {msg}"


def _apply_settings_changes(
    config: dict | None = None,
    soul: str | None = None,
) -> tuple[bool, list[str]]:
    """Persist config YAML + SOUL.md then reload the graph once.

    Passing ``None`` for either argument skips that write — a bare
    call with both None acts as a pure reload (useful for picking up
    external file edits).
    """
    from graph.config_io import (
        apply_updates_to_yaml,
        load_yaml_doc,
        save_secrets,
        save_yaml_doc,
        split_secret_updates,
        strip_secrets_from_doc,
        validate_config_dict,
        write_soul,
    )

    messages: list[str] = []

    if config is not None:
        ok, err = validate_config_dict(config)
        if not ok:
            return False, [f"validation: {err}"]
        try:
            main_config, secret_updates = split_secret_updates(config)
            save_secrets(secret_updates)
            doc = load_yaml_doc()
            apply_updates_to_yaml(doc, main_config)
            strip_secrets_from_doc(doc)
            save_yaml_doc(doc)
            messages.append("config saved")
        except Exception as e:
            log.exception("[config] YAML write failed")
            return False, [f"config write: {e}"]

    if soul is not None:
        try:
            paths = write_soul(soul)
            messages.append(f"SOUL saved ({len(paths)} path{'s' if len(paths) != 1 else ''})")
        except Exception as e:
            log.exception("[config] SOUL write failed")
            return False, [f"soul write: {e}"]

    # Drawer toggles of runtime.autostart_on_boot ride this path,
    # not the wizard's finish_setup, so the LaunchAgent plist has
    # to be installed/removed here too.
    as_msg = _sync_autostart_with_config(config)
    if as_msg:
        messages.append(as_msg)

    ok, reload_msg = _reload_langgraph_agent()
    messages.append(reload_msg)
    return ok, messages


def _build_settings_callbacks() -> dict[str, Any]:
    """Callbacks consumed by the Gradio Configuration drawer + wizard."""
    from graph.config_io import (
        config_to_dict,
        is_setup_complete,
        list_available_tools,
        list_gateway_models,
        list_soul_presets,
        mark_setup_complete,
        read_soul,
        read_soul_preset,
        reset_setup,
    )

    def get_config() -> dict[str, Any]:
        return config_to_dict(_graph_config)

    def list_models(api_base: str = "", api_key: str = "") -> tuple[list[str], str]:
        """UI-friendly model lookup.

        Uses the form-local api_base/api_key when the user is trying a
        different endpoint before saving; falls back to the currently
        loaded graph config so the initial render works without
        arguments.
        """
        base = api_base or (_graph_config.api_base if _graph_config else "")
        key = api_key or (_graph_config.api_key if _graph_config else "")
        return list_gateway_models(base, key)

    def save_all(config: dict | None, soul: str | None) -> tuple[bool, str]:
        ok, messages = _apply_settings_changes(config=config, soul=soul)
        return ok, " • ".join(messages)

    def finish_setup(config: dict | None, soul: str | None) -> tuple[bool, str]:
        """Wizard terminal action — write everything, mark complete, reload.

        Ordering matters:

        1. Write config YAML + SOUL.md (no reload yet).
        2. ``mark_setup_complete()`` — flip the marker BEFORE the
           reload so ``_reload_langgraph_agent`` actually compiles
           the graph. Doing it after means the reload sees
           setup-incomplete and stays ``_graph = None``.
        3. Sync autostart (LaunchAgent plist is independent of the
           graph, so it can happen any time after the config is
           written).
        4. Reload — marker present, graph compiles, chat works.

        Returns a single status string joining per-step messages.
        """
        from graph.config_io import (
            apply_updates_to_yaml,
            load_yaml_doc,
            save_secrets,
            save_yaml_doc,
            split_secret_updates,
            strip_secrets_from_doc,
            validate_config_dict,
            validate_model_connection,
            write_soul,
        )

        messages: list[str] = []

        # 0. Verify the model can actually complete BEFORE we touch anything —
        # otherwise the graph compiles fine but every chat 401s, with no UI
        # signal (the bug that motivated this gate). A real 1-token completion
        # exercises the same auth path as chat, so a bad key / wrong model /
        # unreachable gateway is caught here and returned to the wizard verbatim
        # (e.g. "expected to start with 'sk-'"). Setup stays incomplete, so the
        # operator fixes it in the UI and retries — no file editing required.
        if config is not None and isinstance(config.get("model"), dict):
            m = config["model"]
            test_base = m.get("api_base") or (_graph_config.api_base if _graph_config else "")
            test_key = m.get("api_key") or (_graph_config.api_key if _graph_config else "")
            test_model = m.get("name") or (_graph_config.model_name if _graph_config else "")
            ok, verr = validate_model_connection(test_base, test_key, test_model)
            if not ok:
                return False, f"model connection failed — {verr}"

        # 1. Persist (secrets to the untracked overlay, never the tracked YAML)
        if config is not None:
            ok, err = validate_config_dict(config)
            if not ok:
                return False, f"validation: {err}"
            try:
                main_config, secret_updates = split_secret_updates(config)
                save_secrets(secret_updates)
                doc = load_yaml_doc()
                apply_updates_to_yaml(doc, main_config)
                strip_secrets_from_doc(doc)
                save_yaml_doc(doc)
                messages.append("config saved")
            except Exception as e:
                log.exception("[setup] YAML write failed: %s", e)
                return False, f"config write: {e}"

        if soul is not None:
            try:
                paths = write_soul(soul)
                messages.append(f"SOUL saved ({len(paths)} path{'s' if len(paths) != 1 else ''})")
            except Exception as e:
                log.exception("[setup] SOUL write failed: %s", e)
                return False, f"soul write: {e}"

        # 2. Flip the marker — MUST be before reload so the graph builds
        mark_setup_complete()
        messages.append("setup marked complete")

        # 3. Autostart sync (shared helper — drawer path runs the same)
        as_msg = _sync_autostart_with_config(config)
        if as_msg:
            messages.append(as_msg)

        # 4. Reload — now picks up setup_complete=True and compiles.
        # On failure, roll back the marker so the next page load
        # drops the user back into the wizard instead of landing
        # them in the chat UI with the "setup required" fallback
        # and no obvious way to retry.
        ok, reload_msg = _reload_langgraph_agent()
        messages.append(reload_msg)
        if not ok:
            reset_setup()
            messages.append("setup marker rolled back — re-run the wizard after fixing the error above")

        return ok, " • ".join(messages)

    def restart_setup() -> str:
        """Drawer action — delete the marker so the wizard runs again."""
        reset_setup()
        log.info("[setup] marker removed — wizard will run on next page load")
        return "setup marker removed • reload the page to run the wizard"

    def autostart_info() -> dict[str, Any]:
        """Report platform support + current on-disk state. The drawer
        uses this to render the toggle correctly and to print the
        plist path for debugging."""
        try:
            from autostart import autostart_status

            name = (_graph_config.identity_name if _graph_config else "") or "protoagent"
            return autostart_status(name)
        except Exception as e:
            return {"supported": False, "installed": False, "reason": str(e)}

    def toggle_autostart(enabled: bool) -> tuple[bool, str]:
        """Install or uninstall the OS autostart artifact, mirroring
        the YAML field. Called from the drawer's checkbox handler so
        toggling takes effect immediately without waiting for Save."""
        try:
            from autostart import install_autostart, uninstall_autostart

            name = (_graph_config.identity_name if _graph_config else "") or "protoagent"
            if enabled:
                return install_autostart(agent_name=name, port=_active_port)
            return uninstall_autostart(agent_name=name)
        except Exception as e:
            return False, str(e)

    return {
        "get_config": get_config,
        "get_soul": read_soul,
        "list_models": list_models,
        "list_tools": list_available_tools,
        "list_soul_presets": list_soul_presets,
        "read_soul_preset": read_soul_preset,
        "save_all": save_all,
        "finish_setup": finish_setup,
        "restart_setup": restart_setup,
        "is_setup_complete": is_setup_complete,
        "autostart_info": autostart_info,
        "toggle_autostart": toggle_autostart,
    }


def _setup_required_message() -> list[dict[str, Any]]:
    """Returned by chat endpoints when the wizard hasn't been run.

    The Gradio UI hides the chat pane until setup completes, but the
    HTTP /api/chat, OpenAI-compat, and A2A endpoints don't know the
    UI state — so they emit a plain-text "finish setup first"
    message instead of 500ing on ``_graph is None``.
    """
    return [{
        "role": "assistant",
        "content": (
            "**Setup required.** The setup wizard has not been completed. "
            "Open the UI and finish the wizard, or POST the completed config "
            "to `/api/config/setup` before calling chat endpoints."
        ),
    }]


# ---------------------------------------------------------------------------
# Chat backend — called by the A2A handler + OpenAI-compat endpoint
# ---------------------------------------------------------------------------

async def chat(message: str, session_id: str) -> list[dict[str, Any]]:
    """Route a user message through LangGraph and return the final assistant
    response as a list of ``{"role": "assistant", "content": ...}`` dicts.

    This is the non-streaming entry point used by Gradio + the OpenAI-compat
    endpoint. The A2A handler uses ``_chat_langgraph_stream`` instead to
    capture tool events and emit the cost-v1 DataPart on the terminal
    artifact.
    """
    if _graph is None:
        return _setup_required_message()
    return await _chat_langgraph(message, session_id)


# Cap tool input/output previews so a single frame stays small on the wire.
_TOOL_PREVIEW_CHARS = 800


def _coerce_tool_value(value) -> str:
    """Render a tool input/output for a tool-call card.

    Structured values (dict/list) become compact JSON with double quotes so
    the console can pretty-print them — Python's ``str()`` would emit a repr
    with single quotes that no JSON parser accepts. Everything else is
    stringified. Always truncated to keep the SSE frame small.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)[:_TOOL_PREVIEW_CHARS]
        except (TypeError, ValueError):
            pass
    return str(value)[:_TOOL_PREVIEW_CHARS]


def _coerce_tool_output(value) -> str:
    """Unwrap a tool result to its payload.

    ``on_tool_end`` hands back the LangChain ``ToolMessage``, whose ``str()``
    leaks ``name=``/``tool_call_id=`` noise — the card wants the actual
    ``.content``. Falls back to the raw value for plain returns.
    """
    return _coerce_tool_value(getattr(value, "content", value))


def _interrupt_payload(val) -> dict:
    """Shape a LangGraph interrupt value into the ``input-required`` payload the
    A2A layer parks and the console renders. Richer HITL shapes pass through:
    ``ask_human`` → ``{"question": …}``; ``request_user_input`` → ``{"kind":"form",
    "title", "description", "steps":[…]}``; ``run_command`` approval →
    ``{"kind":"approval", "title", "detail", …}``. Anything else degrades to a
    question with the stringified value. The console renders by shape (prompt vs
    JSON-schema form vs Approve/Deny); the resume value is a string for a
    question, a dict for a form, and a decision for an approval."""
    if isinstance(val, dict) and (val.get("question") or val.get("kind") in ("form", "approval")):
        return val
    return {"question": (str(val) if val is not None else "Input required.")}


async def _run_turn_stream(message: str, session_id: str, config: dict, *, resume_value=None):
    """Run one graph turn over ``astream_events``.

    Yields the same ``(kind, payload)`` status/usage frames the A2A handler
    consumes, then a final ``("__raw__", accumulated_raw)`` sentinel the caller
    intercepts to get the turn's raw model text. Factored out so the initial
    turn, the dropped-scratch kicker retry, and goal-mode continuations all
    share one event loop instead of copy-pasting it.

    When ``resume_value`` is given, the turn resumes a graph paused at an
    ``ask_human`` interrupt (LangGraph HITL) by feeding ``Command(resume=…)``
    instead of a fresh user message. If the turn pauses (the agent called
    ``ask_human``), yields a terminal ``("input_required", {"question": …})``
    frame instead of ``__raw__`` so the A2A layer can park the task (ADR 0003).
    """
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    graph_input = (
        Command(resume=resume_value)
        if resume_value is not None
        else {"messages": [HumanMessage(content=message)], "session_id": session_id}
    )
    import metrics
    import pricing

    accumulated_raw = ""
    streamed_len = 0  # chars of visible <output> already emitted as text frames
    _llm_started: dict[str, float] = {}  # run_id → monotonic start (per-call latency)
    async for event in _graph.astream_events(
        graph_input,
        config=config,
        version="v2",
    ):
        kind = event.get("event", "")
        name = event.get("name", "")
        if kind == "on_chat_model_start":
            # Stamp the per-call start so on_chat_model_end can measure latency.
            rid = event.get("run_id")
            if rid:
                _llm_started[rid] = time.monotonic()
        elif kind == "on_tool_start":
            tool_input = event.get("data", {}).get("input", "")
            # Structured frame (id pairs start↔end) so consumers can render a
            # per-tool card; the A2A handler also derives a text status from it.
            yield ("tool_start", {
                "id": event.get("run_id") or name,
                "name": name,
                "input": _coerce_tool_value(tool_input),
            })
        elif kind == "on_tool_end":
            output = event.get("data", {}).get("output", "")
            yield ("tool_end", {
                "id": event.get("run_id") or name,
                "name": name,
                "output": _coerce_tool_output(output),
            })
        elif kind == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            if chunk and hasattr(chunk, "content") and chunk.content:
                accumulated_raw += chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                # Stream only the user-facing <output> region, token by token —
                # never the scratch_pad. The terminal artifact (extract_output)
                # reconciles any partial tail held back here.
                visible = stream_visible_output(accumulated_raw)
                if len(visible) > streamed_len:
                    yield ("text", visible[streamed_len:])
                    streamed_len = len(visible)
        elif kind == "on_chat_model_end":
            output = event.get("data", {}).get("output")
            usage = getattr(output, "usage_metadata", None) if output else None
            rid = event.get("run_id")
            latency_s = max(0.0, time.monotonic() - _llm_started.pop(rid, time.monotonic())) if rid else 0.0
            model = (
                (event.get("metadata") or {}).get("ls_model_name")
                or getattr(output, "response_metadata", {}).get("model_name", "")
                or "model"
            )
            if usage:
                # Prompt-cache token details (best-effort — OpenAI-compat exposes
                # cached reads via prompt_tokens_details; cache_creation is
                # Anthropic-specific and may not round-trip every gateway).
                details = usage.get("input_token_details") or {}
                cache_read = int(details.get("cache_read", 0) or 0)
                cache_creation = int(details.get("cache_creation", 0) or 0)
                usage_out = {
                    "input_tokens": int(usage.get("input_tokens", 0) or 0),
                    "output_tokens": int(usage.get("output_tokens", 0) or 0),
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                }
                cost = pricing.cost_usd(model, usage_out)
                finish_reason = (
                    getattr(output, "response_metadata", {}).get("finish_reason", "")
                    or "stop"
                )
                # Wire the per-call Prometheus seam (no-op when unconfigured);
                # previously record_llm_call was defined but never called. The
                # per-call Langfuse generation span comes from the LiteLLM
                # gateway callback — we deliberately don't add a manual shim
                # that would bypass trace_session's nesting (see tracing.py).
                try:
                    metrics.record_llm_call(
                        model, finish_reason, latency_s,
                        tokens_input=usage_out["input_tokens"],
                        tokens_output=usage_out["output_tokens"],
                        cache_read=cache_read, cache_creation=cache_creation,
                        cost_usd=cost,
                    )
                except Exception:  # noqa: BLE001 — telemetry must never break a turn
                    pass
                # Carry cache fields + cost + the ACTUAL model to the A2A handler
                # for the cost-v1 artifact (accumulated across the turn's calls).
                # The model name proves routing per turn — incl. aux/fallback
                # models — vs. the statically-configured lead (ADR 0006 Slice 4b).
                yield ("usage", {**usage_out, "cost_usd": cost, "model": model})

    # HITL pause (ADR 0003): the agent called ask_human → LangGraph interrupt().
    # The graph is checkpointed at the interrupt; surface the question so the A2A
    # layer parks the task as input-required. Resume later with resume_value.
    try:
        snapshot = await _graph.aget_state(config)
        pending = list(getattr(snapshot, "interrupts", None) or [])
        if not pending:
            for t in getattr(snapshot, "tasks", ()) or ():
                pending.extend(getattr(t, "interrupts", ()) or ())
    except Exception:
        pending = []
    if pending:
        val = getattr(pending[0], "value", pending[0])
        yield ("input_required", _interrupt_payload(val))
        return

    yield ("__raw__", accumulated_raw)


# --- Workflow slash commands (ADR 0002) --------------------------------------
# A chat message like ``/research-and-brief quantum computing`` runs the named
# workflow instead of a normal model turn — the slash-command analogue of the
# run_workflow tool. Free text maps to the first unset (required) input; explicit
# ``key=value`` tokens set named inputs. Short-circuits the turn like /goal does.


def _parse_slash_command(message: str) -> tuple[str, str]:
    """Split ``/name rest`` → (name, rest). Returns ("", "") if not a slash msg."""
    s = (message or "").strip()
    if not s.startswith("/"):
        return "", ""
    parts = s[1:].split(None, 1)
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def _parse_workflow_inputs(recipe: dict, rest: str) -> dict:
    """Map a slash-command argument string to a workflow's named inputs.

    ``key=value`` tokens (quotes respected) set inputs explicitly; any leftover
    free text is assigned to the first not-yet-set input, preferring required
    ones — so ``/research-and-brief quantum computing`` fills ``topic``.
    """
    import shlex

    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    inputs: dict = {}
    leftover: list[str] = []
    for tok in tokens:
        if "=" in tok and tok.split("=", 1)[0].isidentifier():
            key, val = tok.split("=", 1)
            inputs[key] = val
        else:
            leftover.append(tok)
    if leftover:
        declared = recipe.get("inputs", []) or []
        target = next((i["name"] for i in declared if i["name"] not in inputs and i.get("required")), None)
        if target is None:
            target = next((i["name"] for i in declared if i["name"] not in inputs), None)
        if target:
            inputs[target] = " ".join(leftover)
    return inputs


def _parse_workflow_command(message: str):
    """Return (name, inputs) if ``message`` is ``/<known-workflow> …``, else None."""
    name, rest = _parse_slash_command(message)
    if not name or _workflow_registry is None:
        return None
    recipe = _workflow_registry.get(name)
    if recipe is None:
        return None
    return name, _parse_workflow_inputs(recipe, rest)


async def _run_parsed_workflow(name: str, inputs: dict, *, on_step=None) -> str:
    """Run a workflow command and format its output as the assistant reply.

    ``on_step`` is forwarded to ``run_manual_workflow`` so the caller can stream
    per-step progress (the chat path renders a tool card per step)."""
    from graph.agent import run_manual_workflow

    try:
        result = await run_manual_workflow(
            _graph_config, _workflow_registry,
            knowledge_store=_knowledge_store, scheduler=_scheduler,
            name=name, inputs=inputs, on_step=on_step,
        )
    except ValueError as exc:
        return f"⚠️ {exc}"
    raw = result.get("output") or ""
    # Strip subagent scratch_pad/output tags so the chat shows clean text,
    # matching how a normal turn is rendered.
    out = extract_output(raw) or raw or "(workflow produced no output)"
    failed = result.get("failed") or []
    if failed:
        out += f"\n\n_(failed steps: {', '.join(failed)})_"
    return out


async def _chat_langgraph_stream(
    message: str,
    session_id: str,
    *,
    caller_trace: dict | None = None,
    resume: bool = False,
):
    """Async generator — yields (event_type, payload) tuples from the
    LangGraph run. Consumed by ``a2a_executor.ProtoAgentExecutor`` to
    drive the SDK task lifecycle + SSE streaming.

    Event contract (matches what the A2A handler expects):

    - ``tool_start`` / ``tool_end`` — status frames w/ tool name + preview
    - ``usage`` — per-LLM-call token usage for the cost-v1 DataPart
    - ``done`` — terminal; payload is the final user-facing text
    - ``error`` — terminal; payload is the error string

    ``caller_trace`` is the ``a2a.trace`` metadata from the incoming
    A2A message. When present, Langfuse stamps ``caller_trace_id`` +
    ``caller_span_id`` so operators can cross-reference this trace to
    the dispatching agent's trace in the same project.
    """
    import tracing
    from langchain_core.messages import HumanMessage

    from graph.goals.goal_turn import goal_turn

    trace_meta: dict = {"message_preview": message[:100]}
    if caller_trace:
        if caller_trace.get("traceId"):
            trace_meta["caller_trace_id"] = caller_trace["traceId"]
        if caller_trace.get("spanId"):
            trace_meta["caller_span_id"] = caller_trace["spanId"]

    if _graph is None:
        yield ("error", "setup required — finish the setup wizard before calling A2A endpoints")
        return

    async with tracing.trace_session(
        session_id=session_id,
        name="a2a-stream",
        metadata=trace_meta,
    ):
        try:
            # Goal control messages (/goal ...) short-circuit the turn: set /
            # status / clear a goal and return the reply without running the graph.
            if _goal_controller is not None:
                reply = await _goal_controller.parse_control(message, session_id)
                if reply is not None:
                    yield ("done", reply)
                    return

            # Workflow slash command (/<workflow-name> …) short-circuits the turn:
            # run the recipe and return its output. Each step renders its own
            # tool card (gather → angles → brief) so a multi-step workflow shows
            # live progress instead of one opaque card that looks hung.
            parsed = _parse_workflow_command(message)
            if parsed is not None:
                wf_name, wf_inputs = parsed
                _WF_DONE = object()
                step_q: asyncio.Queue = asyncio.Queue()

                async def _on_step(event: dict) -> None:
                    await step_q.put(event)

                async def _runner() -> str:
                    try:
                        return await _run_parsed_workflow(wf_name, wf_inputs, on_step=_on_step)
                    finally:
                        await step_q.put(_WF_DONE)

                runner = asyncio.create_task(_runner())
                # An umbrella card for the whole workflow, then one per step.
                yield ("tool_start", {"id": f"workflow:{wf_name}", "name": f"workflow:{wf_name}",
                                      "input": _coerce_tool_value(wf_inputs)})
                while True:
                    event = await step_q.get()
                    if event is _WF_DONE:
                        break
                    sid = event.get("step_id", "")
                    step_tool_id = f"workflow:{wf_name}:{sid}"
                    label = f"{wf_name} · {sid}"
                    if event.get("phase") == "start":
                        yield ("tool_start", {"id": step_tool_id, "name": label,
                                              "input": event.get("subagent", "")})
                    else:
                        yield ("tool_end", {"id": step_tool_id, "name": label,
                                            "output": extract_output(event.get("output", "")) or event.get("output", "")})
                wf_out = await runner
                yield ("tool_end", {"id": f"workflow:{wf_name}", "name": f"workflow:{wf_name}", "output": wf_out[:300]})
                yield ("done", wf_out)
                return

            # thread_id keys this session's history in the checkpointer (bound
            # at compile time in create_agent_graph). The prefix isolates A2A
            # sessions from Gradio chat in the shared MemorySaver.
            config = {
                "configurable": {"thread_id": f"a2a:{session_id}"},
                "recursion_limit": 200,
            }

            # When a goal is already active, the whole turn is goal-driven —
            # suppress cross-session prior_sessions on the initial turn (and the
            # kicker retry below), matching the continuation turns.
            goal_active = (
                _goal_controller is not None
                and _goal_controller.active_goal(session_id) is not None
            )

            # One graph turn (model tokens accumulated silently; A2A consumers
            # get progress from tool_start/tool_end). Final text is extracted
            # once via extract_output().
            accumulated_raw = ""
            paused = False
            with goal_turn(goal_active):
                async for kind, payload in _run_turn_stream(
                    message, session_id, config,
                    resume_value=(message if resume else None),
                ):
                    if kind == "__raw__":
                        accumulated_raw = payload
                    elif kind == "input_required":
                        # Agent paused for human input — surface it and park the
                        # turn; the A2A runner sets the task input-required and the
                        # caller resumes via message/send on the same taskId.
                        yield (kind, payload)
                        paused = True
                    else:
                        yield (kind, payload)

            # A paused turn produced no final answer — don't run the
            # dropped-scratch kicker or goal verification; the task is parked.
            if paused:
                return

            final_text = extract_output(accumulated_raw)
            final_raw = accumulated_raw

            # Dropped-turn recovery: the model emitted only <scratch_pad>/<think>
            # — no <output>, no tool call — so extract_output is empty and the
            # turn would silently drop. Re-prompt once on the same thread with a
            # kicker (history is preserved by the checkpointer). Capped at 1 retry.
            if not final_text and is_dropped_scratch_turn(accumulated_raw):
                log.warning(
                    "[chat-stream] dropped scratch-only turn (session=%s) — kicker retry",
                    session_id,
                )
                yield ("tool_start", "↻ retry: prior turn dropped scratch-only")
                retry_raw = ""
                with goal_turn(goal_active):
                    async for kind, payload in _run_turn_stream(DROPPED_SCRATCH_KICKER, session_id, config):
                        if kind == "__raw__":
                            retry_raw = payload
                        else:
                            yield (kind, payload)
                recovered = extract_output(retry_raw)
                if recovered:
                    final_text, final_raw = recovered, retry_raw
                    log.info("[chat-stream] kicker recovered the turn (session=%s)", session_id)
                else:
                    log.warning(
                        "[chat-stream] kicker retry also empty (session=%s) — falling back",
                        session_id,
                    )

            # Goal mode: when an active goal exists for this session, verify the
            # outcome after the agent stops; if not met, re-invoke on the same
            # thread with a continuation prompt until the verifier passes, the
            # iteration budget is spent, or it's flagged unachievable.
            if _goal_controller is not None and _goal_controller.active_goal(session_id):
                guard, hard_cap = 0, _graph_config.goal_max_iterations + 2
                note = ""
                while guard < hard_cap:
                    guard += 1
                    decision = await _goal_controller.evaluate(session_id, last_text=final_text)
                    if decision is None:
                        break
                    note = decision.note
                    yield ("tool_start", f"🎯 {decision.note}")
                    if decision.action == "done":
                        break
                    cont_raw = ""
                    with goal_turn():
                        async for kind, payload in _run_turn_stream(decision.message, session_id, config):
                            if kind == "__raw__":
                                cont_raw = payload
                            else:
                                yield (kind, payload)
                    cont_text = extract_output(cont_raw)
                    if cont_text:
                        final_text, final_raw = cont_text, cont_raw
                # Append the terminal goal outcome to the answer so the A2A
                # terminal artifact carries it, matching the non-streaming path
                # (the 🎯 status frames above are transient and can coalesce).
                if note:
                    final_text = f"{final_text}\n\n---\n{note}"

            # Self-reported confidence (from whichever pass produced the answer),
            # yielded before "done" so the A2A handler records it on the
            # terminal artifact's confidence-v1 DataPart.
            confidence, explanation = extract_confidence(final_raw)
            if confidence is not None:
                yield ("confidence", {"confidence": confidence, "explanation": explanation})

            yield ("done", final_text)

        except GeneratorExit:
            # Expected: A2A consumers (e.g. Workstacean's A2AExecutor) break
            # out of the SSE loop after capturing the initial task event,
            # then hand off to TaskTracker for polling. Re-raise so Python
            # finalizes the generator cleanly; the OTel cross-context detach
            # noise this used to emit is silenced at the logger level in
            # tracing.py.
            raise
        except Exception as e:
            log.exception(
                "[a2a-stream] unhandled exception for session=%s: %s",
                session_id, e,
            )
            yield ("error", str(e))
        finally:
            tracing.flush()


async def _chat_langgraph(message: str, session_id: str) -> list[dict[str, Any]]:
    """Non-streaming LangGraph entry — used by Gradio + OpenAI-compat."""
    import tracing
    from langchain_core.messages import HumanMessage, AIMessage

    from graph.goals.goal_turn import goal_turn

    async with tracing.trace_session(
        session_id=session_id,
        name="chat",
        metadata={"message_preview": message[:100]},
    ):
        try:
            # Goal control messages short-circuit (set / status / clear).
            if _goal_controller is not None:
                reply = await _goal_controller.parse_control(message, session_id)
                if reply is not None:
                    return [{"role": "assistant", "content": reply}]

            # Workflow slash command (/<workflow-name> …) short-circuits the turn.
            parsed = _parse_workflow_command(message)
            if parsed is not None:
                return [{"role": "assistant", "content": await _run_parsed_workflow(*parsed)}]

            config = {"configurable": {"thread_id": f"gradio:{session_id}"}}

            def _last_ai(result) -> str:
                for msg in reversed(result.get("messages", [])):
                    if isinstance(msg, AIMessage) and msg.content:
                        return msg.content if isinstance(msg.content, str) else str(msg.content)
                return ""

            # When a goal is already active, the whole turn is goal-driven —
            # suppress cross-session prior_sessions on the initial turn too.
            goal_active = (
                _goal_controller is not None
                and _goal_controller.active_goal(session_id) is not None
            )
            with goal_turn(goal_active):
                result = await _graph.ainvoke(
                    {"messages": [HumanMessage(content=message)], "session_id": session_id},
                    config=config,
                )
            response = extract_output(_last_ai(result))

            # Goal mode: verify after the agent stops; re-invoke with a
            # continuation prompt until met / exhausted / unachievable.
            if _goal_controller is not None and _goal_controller.active_goal(session_id):
                guard, hard_cap = 0, _graph_config.goal_max_iterations + 2
                note = ""
                while guard < hard_cap:
                    guard += 1
                    decision = await _goal_controller.evaluate(session_id, last_text=response)
                    if decision is None:
                        break
                    note = decision.note
                    if decision.action == "done":
                        break
                    with goal_turn():
                        result = await _graph.ainvoke(
                            {"messages": [HumanMessage(content=decision.message)], "session_id": session_id},
                            config=config,
                        )
                    nxt = extract_output(_last_ai(result))
                    if nxt:
                        response = nxt
                if note:
                    response = f"{response}\n\n---\n{note}"

            return [{"role": "assistant", "content": response}]
        except Exception as e:
            log.exception(
                "[chat] unhandled exception for session=%s: %s",
                session_id, e,
            )
            return [{"role": "assistant", "content": f"**Error:** {e}"}]
        finally:
            tracing.flush()


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
    if _graph_config and _graph_config.identity_name and _graph_config.identity_name != "protoagent":
        return _graph_config.identity_name
    return AGENT_NAME_ENV


def _bearer_configured() -> bool:
    return bool(os.environ.get("A2A_AUTH_TOKEN", "") or (_graph_config and _graph_config.auth_token))


# Skill declarations (ADR-0006 addendum / #476). A skill MAY declare an
# ``output_schema`` (JSON Schema) + ``result_mime`` — when present, the agent
# enforces the schema via a forced-tool-call finalizer in the executor and emits
# the result as a typed DataPart (``protolabs_a2a.emit_skill_result``), and the
# card advertises the MIME in that skill's ``output_modes``. No schema ⇒ free
# text (today's default). The schema lives HERE (skill config), not on the card
# — ``AgentSkill`` only carries ``output_modes`` (the MIME), per the A2A spec.
#
# REPLACE when forking. The template ships one free-text placeholder so a fresh
# clone is callable; the commented fields below show a structured skill.
_SKILL_SPECS: list[dict] = [
    {
        "id": "chat",
        "name": "Chat",
        "description": "General-purpose chat interface. Replace with your agent's real skills.",
        "tags": ["template"],
        "examples": ["hello", "what can you do?"],
        # To make a skill return schema-enforced structured output, add:
        #   "output_schema": {"type": "object", "properties": {...}, "required": [...]},
        #   "result_mime": "application/vnd.protolabs.<your-skill>-v1+json",
    },
]


def _agent_skills():
    """Build the card's ``AgentSkill`` list from ``_SKILL_SPECS``. A spec with a
    ``result_mime`` advertises it in ``output_modes`` (the A2A-native way to tell
    consumers the skill emits that structured type)."""
    from a2a.types import AgentSkill

    skills = []
    for s in _SKILL_SPECS:
        kwargs = dict(
            id=s["id"],
            name=s["name"],
            description=s["description"],
            tags=s.get("tags", []),
            examples=s.get("examples", []),
        )
        if s.get("result_mime"):
            kwargs["output_modes"] = [s["result_mime"]]
        skills.append(AgentSkill(**kwargs))
    return skills


def structured_skill_schema(skill_id: str) -> dict | None:
    """For a skill that declares structured output, return
    ``{"schema": <JSON Schema>, "mime": <result_mime>}``; else ``None`` (free
    text). The executor's structured finalizer (#476) reads this to run the
    forced-tool-call against the schema and emit the validated object as a
    ``result_mime`` DataPart. The schema isn't on the card (``AgentSkill`` has no
    schema field) — it lives in ``_SKILL_SPECS``."""
    for s in _SKILL_SPECS:
        if s["id"] == skill_id and s.get("output_schema") and s.get("result_mime"):
            return {"schema": s["output_schema"], "mime": s["result_mime"]}
    return None


def _package_version() -> str:
    """Single-source the agent-card version from the package metadata.

    ``pyproject.toml`` ``[project].version`` is the one source of truth (the
    release pipeline bumps it). Prefer installed-package metadata; fall back
    to reading pyproject.toml (it's shipped in the image via ``COPY .``);
    final fallback keeps the card valid if neither is available.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("protoagent")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover - importlib.metadata always present on 3.11+
        pass

    pyproject = Path(__file__).parent / "pyproject.toml"
    try:
        m = re.search(
            r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE
        )
        if m:
            return m.group(1)
    except OSError:
        pass
    return "0.0.0"


def _a2a_card_url() -> str:
    """The reachable JSON-RPC endpoint to advertise in the A2A card's interface.

    The card tells other agents *where to send* ``message/send``, so this must
    be the agent's externally-reachable address — not the bind host. Prefer an
    explicit ``A2A_PUBLIC_URL`` (set this for any deployed agent: behind a proxy
    / in a container the public address isn't the bound port). Fall back to the
    actually-bound loopback port (``_active_port``) for local + desktop runs —
    correct there because the client is on the same host (and the desktop's port
    is dynamic). The ``/a2a`` suffix is the JSON-RPC route.
    """
    base = (os.environ.get("A2A_PUBLIC_URL") or "").strip().rstrip("/")
    if not base:
        base = f"http://127.0.0.1:{_active_port}"
    return f"{base}/a2a"


def _build_agent_card_proto():
    """Build the A2A 1.0 ``AgentCard`` (proto) served at
    ``/.well-known/agent-card.json``, applying the protoLabs fleet conventions
    via ``protolabs_a2a.build_agent_card``.

    **Fork this.** Replace ``name``, ``description``, and ``_agent_skills()``
    with your agent's actual surface. The four custom extensions
    (cost / confidence / worldstate-delta / tool-call) are declared by default
    — this template emits cost-v1 + confidence-v1 from ``_chat_langgraph_stream``
    and worldstate-delta / tool-call when a tool reports them.

    The interface ``url`` (``_a2a_card_url``) targets the JSON-RPC endpoint
    (``/a2a``) at the agent's reachable address — set ``A2A_PUBLIC_URL`` when
    deployed; otherwise it's the bound loopback port.
    """
    import protolabs_a2a as pa

    return pa.build_agent_card(
        name=agent_name(),
        description=(
            "protoAgent template — A2A 1.0 LangGraph agent. "
            "Replace this description with your agent's actual purpose."
        ),
        url=_a2a_card_url(),
        version=_package_version(),
        skills=_agent_skills(),
        bearer=_bearer_configured(),
    )


def _record_a2a_telemetry(outcome) -> None:
    """Write one per-turn telemetry row from an executor ``TurnOutcome``
    (ADR 0006 Slice 2). No-op when the telemetry store is off; best-effort so a
    failure never affects the turn."""
    store = _telemetry_store
    if store is None:
        return
    try:
        u = outcome.usage or {}
        primary_model = outcome.models[0] if outcome.models else (
            (_graph_config.model_name if _graph_config else "") or ""
        )
        input_tokens = int(u.get("input_tokens", 0) or 0)
        output_tokens = int(u.get("output_tokens", 0) or 0)
        from datetime import datetime, timedelta, timezone
        ended = datetime.now(timezone.utc)
        created = ended - timedelta(milliseconds=int(outcome.duration_ms or 0))
        store.record({
            "task_id": outcome.task_id,
            "session_id": outcome.context_id,
            "state": outcome.state,
            "success": 1 if outcome.state == "completed" else 0,
            "model": primary_model,
            "models": ",".join(outcome.models),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
            "cost_usd": float(outcome.cost_usd or 0.0),
            "duration_ms": int(outcome.duration_ms or 0),
            "llm_calls": int(outcome.llm_calls),
            "tool_calls": int(outcome.tool_calls),
            "created_at": created.isoformat(),
            "ended_at": ended.isoformat(),
        })
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        log.exception("[telemetry] failed to record turn %s", outcome.task_id)


def _a2a_terminal(outcome) -> None:
    """A2A terminal hook (ADR 0003 / 0006). Fired by ``ProtoAgentExecutor`` with
    a ``TurnOutcome`` when a turn reaches a terminal state. Records the per-turn
    telemetry row and surfaces the Activity thread's answer on the event bus.
    Best-effort — never raises into the executor."""
    _record_a2a_telemetry(outcome)
    if outcome.context_id != ACTIVITY_CONTEXT:
        return
    text = extract_output(outcome.text) or outcome.text
    if not text.strip():
        return
    _event_bus.publish(
        "activity.message",
        {"role": "assistant", "text": text, "context_id": ACTIVITY_CONTEXT},
    )


# ---------------------------------------------------------------------------
# Main — FastAPI + Gradio + A2A + OpenAI-compat + Prometheus
# ---------------------------------------------------------------------------

def _main():
    global _active_port

    # Frozen-binary entrypoint for the managed Google MCP server (ADR 0017): the
    # bundled desktop app has no `python` on PATH, so the google MCP entry
    # re-invokes this binary with --mcp-google instead of `-m
    # mcp_servers.google.server`. Handle it before argparse/server startup.
    if "--mcp-google" in sys.argv:
        from mcp_servers.google.server import main as _google_mcp_main

        _google_mcp_main()
        return

    parser = argparse.ArgumentParser(description=f"{AGENT_NAME_ENV} — protoAgent server")
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
    _active_port = args.port

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
        if _graph_config is not None:
            roots.extend(getattr(_graph_config, "operator_allowed_dirs", []) or [])
        return roots

    def _operator_runtime_status():
        return _build_operator_status(
            config=_graph_config,
            setup_complete=_operator_setup_complete(),
            graph_loaded=_graph is not None,
            project_path=_operator_repo_root,
            allowed_dirs=_operator_allowed_dirs(),
            knowledge_store=_knowledge_store,
            scheduler=_scheduler,
            cache_warmer=_cache_warmer,
            goal_controller=_goal_controller,
            skills_index=_skills_index,
            mcp={
                "enabled": bool(getattr(_graph_config, "mcp_enabled", False)) if _graph_config else False,
                "servers": _mcp_meta,
                "tool_count": len(_mcp_tools),
            },
            plugins=_plugin_meta,
        )

    def _operator_subagent_list():
        return _operator_list_subagents(_graph_config)

    async def _operator_subagent_run(req: dict):
        if _graph is None:
            raise RuntimeError("agent graph is not loaded; finish setup first")
        return await _operator_run_manual_subagent(
            config=_graph_config,
            knowledge_store=_knowledge_store,
            scheduler=_scheduler,
            description=req.get("description", ""),
            prompt=req.get("prompt", ""),
            subagent_type=req.get("type") or req.get("subagent_type", "researcher"),
            emit_skill=bool(req.get("emit_skill", False)),
        )

    async def _operator_subagent_batch(req: dict):
        if _graph is None:
            raise RuntimeError("agent graph is not loaded; finish setup first")
        return await _operator_run_manual_subagent_batch(
            config=_graph_config,
            knowledge_store=_knowledge_store,
            scheduler=_scheduler,
            tasks=req.get("tasks", []),
        )

    async def _operator_scheduler_list() -> dict:
        import asyncio
        if _scheduler is None:
            return {"jobs": [], "backend": "disabled"}
        jobs = await asyncio.to_thread(_scheduler.list_jobs)
        return {
            "jobs": [j.as_dict() for j in jobs],
            "backend": getattr(_scheduler, "name", "local"),
        }

    async def _operator_scheduler_add(req: dict) -> dict:
        import asyncio
        if _scheduler is None:
            raise RuntimeError("scheduler is not loaded (disabled or setup incomplete)")
        prompt = (req.get("prompt") or "").strip()
        schedule = (req.get("schedule") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        if not schedule:
            raise ValueError("schedule is required")
        job = await asyncio.to_thread(
            _scheduler.add_job, prompt, schedule, job_id=req.get("job_id") or None
        )
        return job.as_dict()

    async def _operator_scheduler_cancel(job_id: str) -> dict:
        import asyncio
        if _scheduler is None:
            raise RuntimeError("scheduler is not loaded (disabled or setup incomplete)")
        canceled = await asyncio.to_thread(_scheduler.cancel_job, job_id)
        return {"canceled": bool(canceled)}

    async def _operator_goals_list() -> dict:
        import asyncio
        if _goal_controller is None:
            return {"goals": [], "enabled": False}
        states = await asyncio.to_thread(_goal_controller.store.all)
        return {"goals": [s.to_dict() for s in states], "enabled": True}

    async def _operator_goals_clear(session_id: str) -> dict:
        import asyncio
        if _goal_controller is None:
            return {"cleared": False, "enabled": False}
        cleared = await asyncio.to_thread(_goal_controller.store.clear, session_id)
        return {"cleared": bool(cleared)}

    def _operator_workflows_list() -> dict:
        if _workflow_registry is None:
            return {"workflows": []}
        return {"workflows": _workflow_registry.list()}

    async def _operator_workflow_run(name: str, inputs: dict) -> dict:
        if _graph is None:
            raise RuntimeError("agent graph is not loaded; finish setup first")
        from graph.agent import run_manual_workflow
        return await run_manual_workflow(
            _graph_config, _workflow_registry,
            knowledge_store=_knowledge_store, scheduler=_scheduler,
            name=name, inputs=inputs or {},
        )

    def _operator_workflow_save(recipe: dict) -> dict:
        # Validate against the live subagent registry before writing, so a
        # UI-authored recipe can't reference an unknown subagent / bad DAG.
        if _workflow_registry is None:
            raise RuntimeError("workflows are not available")
        from graph.subagents.config import SUBAGENT_REGISTRY
        from graph.workflows.engine import validate_recipe
        errors = validate_recipe(recipe, known_subagents=set(SUBAGENT_REGISTRY))
        if errors:
            raise ValueError("invalid recipe: " + "; ".join(errors))
        path = _workflow_registry.save(recipe)
        return {"saved": True, "name": recipe.get("name"), "path": path}

    def _operator_workflow_delete(name: str) -> dict:
        if _workflow_registry is None:
            raise RuntimeError("workflows are not available")
        return {"deleted": _workflow_registry.delete(name)}

    def _publish_activity_terminal(record) -> None:
        """Terminal hook (ADR 0003): when a turn in the Activity thread completes,
        push the assistant's visible output to the event bus so connected
        consoles append it live. No-op for every other context."""
        if getattr(record, "context_id", "") != ACTIVITY_CONTEXT:
            return
        raw = getattr(record, "accumulated_text", "") or ""
        text = extract_output(raw) or raw
        if not text.strip():
            return
        _event_bus.publish(
            "activity.message",
            {"role": "assistant", "text": text, "context_id": ACTIVITY_CONTEXT},
        )

    async def _operator_activity_list() -> dict:
        """Return the Activity thread's message history from the checkpointer
        (ADR 0003). The console loads this when opening the Activity surface."""
        messages: list[dict] = []
        if _checkpointer is not None:
            thread_id = f"a2a:{ACTIVITY_CONTEXT}"
            try:
                tup = await _checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
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
        return {"context_id": ACTIVITY_CONTEXT, "messages": messages}

    def _inbox_authorized(token: str | None) -> bool:
        """Validate the inbound bearer token (ADR 0003). Mirrors the A2A posture:
        when no token is configured the endpoint is open (dev), else it must match."""
        active = ((_graph_config.auth_token if _graph_config else "") or os.environ.get("A2A_AUTH_TOKEN", "") or "").strip()
        if not active:
            return True
        return (token or "") == active

    async def _fire_activity_from_inbox(item: dict) -> bool:
        """Fire a now-priority inbox item as a turn into the Activity thread.
        Self-POSTs to /a2a (parity with the scheduler), guarded against storms."""
        import time
        from uuid import uuid4
        import httpx

        if _storm_guard is not None and not _storm_guard.allow(time.monotonic()):
            log.warning("[inbox] storm guard suppressed now-fire for item %s", item.get("id"))
            return False
        headers = {"Content-Type": "application/json"}
        bearer = ((_graph_config.auth_token if _graph_config else "") or os.environ.get("A2A_AUTH_TOKEN", "")).strip()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        api_key = os.environ.get(f"{AGENT_NAME_ENV.upper()}_API_KEY", "").strip()
        if api_key:
            headers["X-API-Key"] = api_key
        mid = str(uuid4())
        body = {
            "jsonrpc": "2.0", "id": mid, "method": "message/send",
            "params": {
                "contextId": ACTIVITY_CONTEXT,
                "message": {"role": "user", "parts": [{"kind": "text", "text": item["text"]}], "messageId": mid},
                "metadata": {"origin": "inbox", "inbox_id": item.get("id"), "inbox_source": item.get("source", "")},
            },
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"http://127.0.0.1:{_active_port}/a2a", headers=headers, json=body)
            return r.status_code < 400
        except Exception:
            log.exception("[inbox] now-fire failed for item %s", item.get("id"))
            return False

    async def _operator_inbox_add(payload: dict) -> dict:
        """Ingest an inbound item (ADR 0003). now-priority fires an Activity turn;
        others queue for check_inbox. Dedup is handled by the store."""
        if _inbox_store is None:
            raise RuntimeError("inbox not loaded; finish setup first")
        item = _inbox_store.add(
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
        if _inbox_store is None:
            return {"items": []}
        items = _inbox_store.list(
            priority_floor=floor or "later", include_delivered=include_delivered, limit=200,
        )
        return {"items": items}

    async def _operator_inbox_deliver(item_id: int) -> dict:
        if _inbox_store is None:
            raise RuntimeError("inbox not loaded; finish setup first")
        return {"ok": True, "delivered": _inbox_store.mark_delivered([item_id])}

    def _operator_chat_commands() -> dict:
        """Slash commands the chat understands — drives the composer autocomplete.

        Currently just `/goal` (when goal mode is loaded). Register a new
        server-handled control command here and the console picks it up.
        """
        commands = []
        if _goal_controller is not None:
            commands.append({
                "name": "goal",
                "description": "Set, check, or clear a self-driving goal for this chat session.",
                "usage": "/goal <condition>   ·   /goal  (status)   ·   /goal clear",
            })
        # Each registered workflow is runnable as /<name> (ADR 0002).
        if _workflow_registry is not None:
            for wf in _workflow_registry.list():
                declared = wf.get("inputs", []) or []
                req = "".join(f" <{i['name']}>" for i in declared if i.get("required"))
                opt = "".join(f" [{i['name']}]" for i in declared if not i.get("required"))
                commands.append({
                    "name": wf["name"],
                    "description": wf.get("description") or f"Run the {wf['name']} workflow.",
                    "usage": f"/{wf['name']}{req}{opt}",
                })
        return {"commands": commands}

    # The in-process beads store is agent-global + graph-independent, but it's
    # otherwise created in _init_langgraph_agent (which only runs once setup is
    # complete). For a fresh, unconfigured agent (first launch, before the wizard)
    # ensure it exists now — otherwise the beads routes bind the CLI fallback
    # service that raises "project_path is required" (the agent-global adapter
    # ignores project_path). Reused by _init_langgraph_agent later.
    global _beads_store
    if _beads_store is None:
        from beads import BeadsStore
        _beads_store = BeadsStore()

    register_operator_routes(
        fastapi_app,
        runtime_status=_operator_runtime_status,
        subagent_list=_operator_subagent_list,
        subagent_run=_operator_subagent_run,
        subagent_batch=_operator_subagent_batch,
        beads_store=_beads_store,
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
    for r in _plugin_routers:
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
        global _main_loop
        import asyncio

        _main_loop = asyncio.get_running_loop()
        if _scheduler is not None:
            try:
                await _scheduler.start()
            except Exception:
                log.exception("[scheduler] startup failed")
        if _cache_warmer is not None:
            try:
                await _cache_warmer.start()
            except Exception:
                log.exception("[cache-warmer] startup failed")
        # Checkpoint pruner — periodic sweep to keep the SQLite history DB bounded.
        global _checkpoint_prune_task
        if (
            _checkpoint_path
            and _graph_config is not None
            and _graph_config.checkpoint_prune_interval_hours > 0
        ):
            import asyncio
            _checkpoint_prune_task = asyncio.create_task(_checkpoint_prune_loop())

        # Inbound Discord gateway (ADR 0015/0016) — started from the live config
        # (UI token in secrets, or the DISCORD_BOT_TOKEN env fallback). A Discord
        # DM is conversational, so it invokes the agent as a chat surface with a
        # per-conversation session_id (the LangGraph thread key), NOT the single
        # system:activity inbox thread.
        try:
            _start_discord_surface()
        except Exception:
            log.exception("[discord] gateway startup failed")

        # Plugin-contributed surfaces (ADR 0018) — start each on the loop. `start`
        # may be sync or async and may return a handle (kept for shutdown).
        # Best-effort: a failing surface logs, never breaks boot.
        global _plugin_surface_handles
        for s in _plugin_surfaces:
            try:
                res = s["start"]()
                if asyncio.iscoroutine(res):
                    res = await res
                _plugin_surface_handles.append(
                    {"name": s["name"], "stop": s.get("stop"), "reload": s.get("reload"), "handle": res}
                )
                log.info("[plugins] started surface: %s", s["name"])
            except Exception:
                log.exception("[plugins] surface %s failed to start", s.get("name"))

    @fastapi_app.on_event("shutdown")
    async def _scheduler_shutdown() -> None:
        # Stop plugin surfaces first (ADR 0018) — best-effort.
        for h in _plugin_surface_handles:
            stop = h.get("stop")
            if not callable(stop):
                continue
            try:
                res = stop()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("[plugins] surface %s failed to stop", h.get("name"))
        if _scheduler is not None:
            try:
                await _scheduler.stop()
            except Exception:
                log.exception("[scheduler] shutdown failed")
        if _cache_warmer is not None:
            try:
                await _cache_warmer.stop()
            except Exception:
                log.exception("[cache-warmer] shutdown failed")
        try:
            from surfaces.discord import stop as _stop_discord
            await _stop_discord()
        except Exception:
            log.exception("[discord] shutdown failed")
        if _checkpoint_prune_task is not None:
            _checkpoint_prune_task.cancel()

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
        if _goal_controller is None:
            return {"enabled": False, "goal": None}
        state = _goal_controller.store.get(session_id)
        return {"enabled": True, "goal": state.to_dict() if state else None}

    @fastapi_app.delete("/api/goal/{session_id}")
    async def _api_goal_clear(session_id: str):
        if _goal_controller is None:
            return {"enabled": False, "cleared": False}
        return {"enabled": True, "cleared": _goal_controller.store.clear(session_id)}

    # --- Health / readiness (ADR 0010) -------------------------------------
    # Reflects whether the graph actually compiled — the only readiness signal
    # in the 'none' tier (no UI to eyeball). 503 until ready, for k8s probes.
    @fastapi_app.get("/healthz", include_in_schema=False)
    async def _healthz():
        from graph.config_io import is_setup_complete
        ready = _graph is not None
        return JSONResponse(
            {
                "ok": ready,
                "graph_compiled": ready,
                "setup_complete": is_setup_complete(),
                "ui": ui,
                # Surface the active model so eval reports can be tagged with the
                # model under test without guessing (evals.runner auto-detects).
                "model": _graph_config.model_name if _graph_config else None,
            },
            status_code=200 if ready else 503,
        )

    # --- Playbooks (skills surface, ADR 0009) ------------------------------
    # Browse + manage the procedural-memory skill index (skills.db) the operator
    # was otherwise blind to. "Playbooks" is the operator-facing name for the
    # skill-v1 artifacts (disk = pinned SKILL.md, emitted = agent-learned).
    @fastapi_app.get("/api/playbooks")
    async def _api_playbooks():
        if _skills_index is None:
            return {"enabled": False, "playbooks": []}
        try:
            skills = _skills_index.all_skills()
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
        if _skills_index is None:
            return {"enabled": False, "deleted": False}
        try:
            _skills_index.delete_skill(skill_id)
            return {"enabled": True, "deleted": True}
        except Exception as exc:  # noqa: BLE001
            log.exception("[playbooks] delete failed")
            return {"enabled": True, "deleted": False, "error": str(exc)}

    # --- Telemetry (ADR 0006 Slice 2) --------------------------------------
    # Per-turn cost/latency rollups from the local store. Powers the operator
    # console's cost/latency surface (Slice 3) and ad-hoc "what's expensive"
    # queries. Read-only; returns {enabled:false} when the store is off.
    @fastapi_app.get("/api/telemetry/summary")
    async def _api_telemetry_summary(since: str | None = None):
        if _telemetry_store is None:
            return {"enabled": False, "summary": None}
        return {"enabled": True, "summary": _telemetry_store.summary(since_iso=since)}

    @fastapi_app.get("/api/telemetry/recent")
    async def _api_telemetry_recent(limit: int = 50):
        if _telemetry_store is None:
            return {"enabled": False, "turns": []}
        return {"enabled": True, "turns": _telemetry_store.recent(limit=min(max(1, limit), 500))}

    @fastapi_app.get("/api/telemetry/insights")
    async def _api_telemetry_insights():
        # Advise-only flywheel signal (ADR 0006 Slice 4): flag outlier turns +
        # prove the levers we can measure from the per-turn store. Read-only.
        if _telemetry_store is None:
            return {"enabled": False, "insights": None}
        import pricing

        s = _telemetry_store.summary()
        flagged = _telemetry_store.outliers()
        # Cache lever (proven): estimated $ saved by prompt-cache reads, billed at
        # the dominant model's input rate (the per-turn store keeps no per-call
        # model breakdown of cache reads).
        by_model = s.get("by_model") or []
        dom_model = by_model[0]["model"] if by_model else ((_graph_config.model_name if _graph_config else "") or "")
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
            "config": config_to_dict(_graph_config),
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
        base = body.api_base or (_graph_config.api_base if _graph_config else "")
        key = body.api_key or (_graph_config.api_key if _graph_config else "")
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
        base = body.api_base or (_graph_config.api_base if _graph_config else "")
        key = body.api_key or (_graph_config.api_key if _graph_config else "")
        model = body.model or (_graph_config.model_name if _graph_config else "")
        ok, error = await asyncio.to_thread(validate_model_connection, base, key, model)
        return {"ok": ok, "error": error}

    class DiscordProbeRequest(PydanticBaseModel):
        bot_token: str = ""

    @fastapi_app.post("/api/config/test-discord")
    async def _api_test_discord(req: DiscordProbeRequest | None = None):
        """Verify a Discord bot token by fetching its identity (Test connection).

        POST (body) so the token never lands in a URL/log. Blank falls back to
        the saved token, so Settings can re-test the live config. Returns the bot
        username so the UI can show "Connected as <bot>".
        """
        from surfaces.discord import validate_token

        body = req or DiscordProbeRequest()
        token = body.bot_token or (_graph_config.discord_bot_token if _graph_config else "")
        ok, bot_user, error = await validate_token(token or "")
        return {
            "ok": ok,
            "error": error,
            "bot_user": (bot_user or {}).get("username") if ok else None,
        }

    def _google_env_from_config() -> None:
        """Mirror the configured Google OAuth client + token path into the env so
        ``mcp_servers.google.auth`` (which reads env) can run the consent/status
        in-process for the connect + status endpoints."""
        from graph.config_io import _live_config_dir

        cid = (_graph_config.google_client_id if _graph_config else "") or ""
        sec = (_graph_config.google_client_secret if _graph_config else "") or ""
        if cid:
            os.environ["GOOGLE_CLIENT_ID"] = cid
        if sec:
            os.environ["GOOGLE_CLIENT_SECRET"] = sec
        os.environ["GOOGLE_TOKEN_PATH"] = str(_live_config_dir() / "google-token.json")

    @fastapi_app.get("/api/config/google/status")
    async def _api_google_status():
        """Report (configured, connected, email) for the Google surface."""
        try:
            from mcp_servers.google.auth import connection_status
        except Exception as e:  # noqa: BLE001 — google extra may be absent
            return {"configured": False, "connected": False, "email": None,
                    "error": f"google support unavailable: {e}"}
        _google_env_from_config()
        try:
            return await asyncio.to_thread(connection_status)
        except Exception as e:  # noqa: BLE001
            return {"configured": False, "connected": False, "email": None, "error": str(e)}

    @fastapi_app.post("/api/config/google/connect")
    async def _api_google_connect():
        """Run the OAuth consent (opens the operator's browser), cache the token,
        enable the Google surface, and reload so the tools register. Long-lived:
        it blocks until the operator approves in the browser (3-min cap)."""
        try:
            from mcp_servers.google.auth import run_consent
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"google support unavailable: {e}"}
        if not (_graph_config and _graph_config.google_client_id and _graph_config.google_client_secret):
            return {"ok": False, "error": "Set the OAuth client ID + secret first, then connect."}
        _google_env_from_config()
        try:
            email = await asyncio.wait_for(asyncio.to_thread(run_consent), timeout=180)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Timed out waiting for Google consent (3 min). Try again."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        # Persist enabled + reload so the managed google MCP server starts and the
        # tools register without a restart.
        ok, msg = await asyncio.to_thread(
            _apply_settings_changes, config={"google": {"enabled": True}}
        )
        return {"ok": True, "email": email, "reload": msg if ok else None}

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
        if _graph_config is not None:
            models, _ = list_gateway_models(_graph_config.api_base, _graph_config.api_key)
        return {"groups": build_schema(_graph_config, model_options=models)}

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

    global _telemetry_store
    _telemetry_store = _build_telemetry_store(_graph_config)

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
        bearer_token=((_graph_config.auth_token if _graph_config else "") or None),
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
            skill_id, spec["schema"], spec["mime"], final_text, _graph_config
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
    static_dir = Path(__file__).parent / "static"
    if ui != "none":
        from operator_api.web import mount_react_app

        web_dist_dir = Path(__file__).parent / "apps" / "web" / "dist"
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
