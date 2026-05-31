"""protoAgent — FastAPI server wrapping a LangGraph agent with A2A.

This is the main entry point. It:

1. Initializes LangGraph (``graph/agent.py``) + the LiteLLM gateway
   connection via ``graph/llm.py``.
2. Mounts the full A2A surface (``a2a_handler.register_a2a_routes``)
   — JSON-RPC on ``POST /a2a``, SSE streaming, push notifications,
   ``tasks/*`` CRUD, agent card at ``/.well-known/agent.json``.
3. Mounts an OpenAI-compatible chat-completions endpoint so the agent
   can be registered as a model in the LiteLLM gateway / OpenWebUI.
4. Optionally mounts a Gradio chat UI for direct operator access.
5. Exposes a Prometheus ``/metrics`` endpoint when the ``metrics``
   module is active.

### Forking checklist

- Change the agent identity in ``_build_agent_card`` (name, description,
  skills, extensions).
- Drop ``SOUL.md`` in the workspace to override the default agent prompt.
- Add your real tools to ``tools/lg_tools.py`` and wire them into
  ``graph/subagents/config.py`` if you want specialized delegation.
- Set the ``<AGENT>_API_KEY`` env var name below to match your agent's
  auth naming convention.
"""

import argparse
import json
import logging
import os
import re
import time
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
# a2a_handler, making the A2A/webhook path invisible in docker logs.
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
_inbox_store = None    # InboxStore — durable inbound inbox (ADR 0003), or None.
_storm_guard = None    # StormGuard for the now→Activity fire path (ADR 0003).
_mcp_clients = []        # Live MultiServerMCPClient handles (kept alive for reconnect).
_mcp_tools = []          # MCP-server tools appended to the active graph.
_mcp_meta = []           # Per-server {name, transport, tool_count} for runtime status.
_plugin_tools = []       # Tools contributed by enabled plugins.
_plugin_skill_dirs = []  # SKILL.md dirs bundled by enabled plugins.
_plugin_meta = []        # Per-plugin {id, name, enabled, loaded, tools, skills} for status.
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

_event_bus = EventBus()  # Server→client SSE push channel (ADR 0003). Process-
                         # lifetime singleton; producers publish, /api/events
                         # streams to connected consoles.


def _init_langgraph_agent():
    """Initialize the LangGraph backend — setup-aware.

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
    from graph.config_io import CONFIG_YAML_PATH, ensure_live_config, is_setup_complete

    # Seed the untracked live config from the .example template on first run.
    # CONFIG_YAML_PATH honors PROTOAGENT_CONFIG_DIR (the desktop sidecar points
    # it at per-user app-data), so load through it rather than a fixed path.
    ensure_live_config()
    _graph_config = LangGraphConfig.from_yaml(CONFIG_YAML_PATH)
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
        _graph = None
        _knowledge_store = None
        log.info(
            "Setup wizard has not been completed — graph not compiled. "
            "Open the UI to finish setup.",
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

    # Skills — human-authored SKILL.md folders (bundle + live + plugin-bundled)
    # seeded into the FTS index; KnowledgeMiddleware retrieves + injects them.
    _skills_index = _build_skills_index(_graph_config, extra_skill_dirs=_plugin_skill_dirs)

    _workflow_registry = _build_workflow_registry(_graph_config)

    global _inbox_store, _storm_guard
    _inbox_store = _build_inbox_store(_graph_config)
    if _storm_guard is None:
        from inbox import StormGuard
        _storm_guard = StormGuard()

    _graph = create_agent_graph(
        _graph_config, knowledge_store=_knowledge_store, scheduler=_scheduler,
        skills_index=_skills_index, extra_tools=_mcp_tools + _plugin_tools,
        checkpointer=_checkpointer, workflow_registry=_workflow_registry,
        inbox_store=_inbox_store,
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


def _start_scheduler_async(backend: "SchedulerBackend") -> None:
    """Fire-and-forget scheduler.start() onto the running loop.

    Reload paths are sync but invoked from FastAPI request handlers,
    so the running loop is available. Awaiting would force the entire
    reload chain to become async — not worth it for one no-await
    coroutine.
    """
    import asyncio
    try:
        asyncio.get_running_loop().create_task(backend.start())
    except RuntimeError:
        log.warning(
            "[reload] no running event loop; scheduler will start "
            "on next process boot",
        )
    except Exception:
        log.exception("[reload] scheduler start failed")


def _stop_scheduler_async(backend: "SchedulerBackend") -> None:
    """Fire-and-forget scheduler.stop() onto the running loop.

    Used when the YAML toggle flips off mid-reload. The polling task
    cancels cleanly; the next graph rebuild registers no scheduler
    tools.
    """
    import asyncio
    try:
        asyncio.get_running_loop().create_task(backend.stop())
    except RuntimeError:
        log.warning("[reload] no running event loop; scheduler not stopped")
    except Exception:
        log.exception("[reload] scheduler stop failed")


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
        from a2a_handler import set_a2a_token

        set_a2a_token(new_config.auth_token or None)
    except ImportError:
        # a2a_handler not yet imported (e.g. during early-boot reload
        # before _main wires routes) — harmless.
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

    if new_graph is None:
        log.info("[reload] setup not complete — config reloaded, graph not compiled")
        return True, "config reloaded • setup not complete"

    log.info("LangGraph agent reloaded (model: %s)", _graph_config.model_name)
    return True, f"reloaded • model={_graph_config.model_name}"


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
            write_soul,
        )

        messages: list[str] = []

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


async def _run_turn_stream(message: str, session_id: str, config: dict):
    """Run one graph turn over ``astream_events``.

    Yields the same ``(kind, payload)`` status/usage frames the A2A handler
    consumes, then a final ``("__raw__", accumulated_raw)`` sentinel the caller
    intercepts to get the turn's raw model text. Factored out so the initial
    turn, the dropped-scratch kicker retry, and goal-mode continuations all
    share one event loop instead of copy-pasting it.
    """
    from langchain_core.messages import HumanMessage

    accumulated_raw = ""
    streamed_len = 0  # chars of visible <output> already emitted as text frames
    async for event in _graph.astream_events(
        {"messages": [HumanMessage(content=message)], "session_id": session_id},
        config=config,
        version="v2",
    ):
        kind = event.get("event", "")
        name = event.get("name", "")
        if kind == "on_tool_start":
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
            if usage:
                yield ("usage", {
                    "input_tokens": int(usage.get("input_tokens", 0) or 0),
                    "output_tokens": int(usage.get("output_tokens", 0) or 0),
                })
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


async def _run_parsed_workflow(name: str, inputs: dict) -> str:
    """Run a workflow command and format its output as the assistant reply."""
    from graph.agent import run_manual_workflow

    try:
        result = await run_manual_workflow(
            _graph_config, _workflow_registry,
            knowledge_store=_knowledge_store, scheduler=_scheduler,
            name=name, inputs=inputs,
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
):
    """Async generator — yields (event_type, payload) tuples from the
    LangGraph run. Consumed by ``a2a_handler.register_a2a_routes`` to
    drive the background task runner + SSE streaming.

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
            # run the recipe and return its output. A tool_start/end pair renders
            # a card so the operator sees it running.
            parsed = _parse_workflow_command(message)
            if parsed is not None:
                wf_name, wf_inputs = parsed
                tool_id = f"workflow:{wf_name}"
                yield ("tool_start", {"id": tool_id, "name": tool_id, "input": _coerce_tool_value(wf_inputs)})
                wf_out = await _run_parsed_workflow(wf_name, wf_inputs)
                yield ("tool_end", {"id": tool_id, "name": tool_id, "output": wf_out[:300]})
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
            with goal_turn(goal_active):
                async for kind, payload in _run_turn_stream(message, session_id, config):
                    if kind == "__raw__":
                        accumulated_raw = payload
                    else:
                        yield (kind, payload)

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


def _build_security_schemes() -> dict:
    """Return securitySchemes dict, adding bearer only when A2A_AUTH_TOKEN is set."""
    schemes: dict = {"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}
    if os.environ.get("A2A_AUTH_TOKEN", "") or (_graph_config and _graph_config.auth_token):
        schemes["bearer"] = {"type": "http", "scheme": "bearer"}
    return schemes


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


def _build_agent_card(host: str) -> dict:
    """Build the A2A agent card served at /.well-known/agent-card.json.

    **Fork this.** Replace ``name``, ``description``, ``skills``, and
    any extensions with your agent's actual surface. Keep the
    ``capabilities`` block as-is unless you have a reason to turn off
    streaming or push notifications — the A2A handler supports both
    and consumers rely on the flags being honest.

    Extension declarations:

    - ``effect-domain-v1`` — declare per-skill world-state mutations
      so Workstacean's L1 planner can rank your agent against
      goals that target those state selectors. Only declare effects
      that actually mutate shared state.
    - ``cost-v1`` — declare that your agent emits a cost-v1 DataPart
      on every terminal task. This template DOES emit it automatically
      (see the ``on_chat_model_end`` handler in
      ``_chat_langgraph_stream``), so the declaration is kept — drop
      it only if you strip the usage-capture.
    """
    return {
        "name": agent_name(),
        "description": (
            "protoAgent template — A2A-compliant LangGraph agent. "
            "Replace this description with your agent's actual purpose."
        ),
        # A2A spec: the url field must point at the JSON-RPC endpoint
        # (where message/send is accepted), NOT the server root.
        "url": f"http://{host}/a2a",
        "version": _package_version(),
        "provider": {
            "organization": "protoLabsAI",
            "url": "https://github.com/protoLabsAI",
        },
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
            "stateTransitionHistory": False,
            "extensions": [
                # cost-v1 emission is wired by default via the `on_chat_model_end`
                # capture in _chat_langgraph_stream above.
                {"uri": "https://proto-labs.ai/a2a/ext/cost-v1"},
                # confidence-v1: emitted when the model self-reports a
                # <confidence> tag (see graph/output_format.py::extract_confidence
                # and the confidence handler in _run_task_background).
                {"uri": "https://proto-labs.ai/a2a/ext/confidence-v1"},
                # ── Per-skill policy metadata (optional; declarative only) ──────
                # Uncomment and fill with YOUR real skill IDs once you've replaced
                # the placeholder card below. A consumer (e.g. Workstacean) reads
                # these to gate execution; the template makes no claims for you.
                #
                # blast-v1 — scope of effect per skill (self | project | repo),
                # so higher-blast work can be policy-gated:
                # {
                #     "uri": "https://proto-labs.ai/a2a/ext/blast-v1",
                #     "params": {"skills": {"my_skill": {"radius": "self"}}},
                # },
                # hitl-mode-v1 — human-in-the-loop approval per skill
                # (autonomous | notification). Composes with blast-v1:
                # {
                #     "uri": "https://proto-labs.ai/a2a/ext/hitl-mode-v1",
                #     "params": {"skills": {"my_skill": {"mode": "autonomous"}}},
                # },
            ],
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/markdown"],
        "skills": [
            # REPLACE — template ships one placeholder skill.
            {
                "id": "chat",
                "name": "Chat",
                "description": "General-purpose chat interface. Replace with your agent's real skills.",
                "tags": ["template"],
                "examples": ["hello", "what can you do?"],
            },
        ],
        "securitySchemes": _build_security_schemes(),
        "security": [{"apiKey": []}],
    }


# ---------------------------------------------------------------------------
# Main — FastAPI + Gradio + A2A + OpenAI-compat + Prometheus
# ---------------------------------------------------------------------------

def _main():
    global _active_port

    parser = argparse.ArgumentParser(description=f"{AGENT_NAME_ENV} — protoAgent server")
    parser.add_argument("--port", type=int, default=7870)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument(
        "--headless",
        action="store_true",
        default=os.environ.get("PROTOAGENT_HEADLESS", "").lower() in ("1", "true", "yes"),
        help="Serve only the API / A2A / React console — skip the Gradio UI. "
             "Used by the desktop sidecar (the React console is the UI there, "
             "and Gradio is the heaviest dependency to freeze).",
    )
    args = parser.parse_args()
    _active_port = args.port
    headless = args.headless

    # Initialize observability
    import tracing
    import metrics
    tracing.init()
    metrics.init()

    _init_langgraph_agent()

    # Optional Gradio chat UI — skipped entirely in headless mode so the
    # frozen sidecar never imports Gradio (its biggest, PyInstaller-hostile
    # dependency). The React console is the UI in that mode.
    blocks = None
    if not headless:
        from chat_ui import create_chat_app
        blocks = create_chat_app(
            chat_fn=chat,
            title=agent_name(),
            subtitle="protoAgent",
            placeholder="Send a message...",
            pwa=True,
            settings=_build_settings_callbacks(),
        )

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

    _operator_repo_root = str(Path(__file__).parent.resolve())

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

    register_operator_routes(
        fastapi_app,
        runtime_status=_operator_runtime_status,
        subagent_list=_operator_subagent_list,
        subagent_run=_operator_subagent_run,
        subagent_batch=_operator_subagent_batch,
        allowed_dirs=_operator_allowed_dirs,
        scheduler_list=_operator_scheduler_list,
        scheduler_add=_operator_scheduler_add,
        scheduler_cancel=_operator_scheduler_cancel,
        goal_list=_operator_goals_list,
        goal_clear=_operator_goals_clear,
        chat_commands=_operator_chat_commands,
        workflows_list=_operator_workflows_list,
        workflows_run=_operator_workflow_run,
        events_subscribe=_event_bus.subscribe,
        activity_list=_operator_activity_list,
        inbox_add=_operator_inbox_add,
        inbox_authorized=_inbox_authorized,
        inbox_list=_operator_inbox_list,
        inbox_deliver=_operator_inbox_deliver,
    )

    # --- Scheduler lifecycle ------------------------------------------------
    # The local scheduler needs an asyncio polling task; the Workstacean
    # adapter is a no-op start/stop. Both implement the same contract so
    # we just call through. on_event is preferred over a lifespan
    # context manager here — the rest of the boot is sync (uvicorn.run
    # is the only blocking call) and FastAPI fires startup/shutdown
    # around it.
    @fastapi_app.on_event("startup")
    async def _scheduler_startup() -> None:
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

    @fastapi_app.on_event("shutdown")
    async def _scheduler_shutdown() -> None:
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
        ok, messages = _apply_settings_changes(config=req.config, soul=req.soul)
        return {"ok": ok, "messages": messages}

    class ModelsProbeRequest(PydanticBaseModel):
        api_base: str = ""
        api_key: str = ""

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
        ok, msg = callbacks["finish_setup"](req.config, req.soul)
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
        ok, messages = _apply_settings_changes(config=nest_updates(req.updates))
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

    # --- A2A agent card -----------------------------------------------------
    @fastapi_app.get("/.well-known/agent.json", include_in_schema=False)
    @fastapi_app.get("/.well-known/agent-card.json", include_in_schema=False)
    async def _a2a_agent_card(request: Request):
        host = request.headers.get("host", f"{agent_name()}:7870")
        return JSONResponse(
            content=_build_agent_card(host),
            headers={"Cache-Control": "public, max-age=60"},
        )

    # --- A2A protocol -------------------------------------------------------
    # JSON-RPC + REST, streaming, polling, cancel, push webhooks.
    from a2a_handler import register_a2a_routes

    # Two independent A2A auth surfaces:
    #
    # 1. **Bearer** (modern) — ``auth.token`` in YAML, captured by the
    #    wizard as "A2A bearer token". Passed via the ``auth_token``
    #    argument, with ``A2A_AUTH_TOKEN`` env as fallback. Updates
    #    from a wizard/drawer-driven reload propagate live through
    #    ``a2a_handler.set_a2a_token`` — no restart needed.
    # 2. **X-API-Key** (legacy) — ``<AGENT>_API_KEY`` env var, threaded
    #    through the ``api_key`` argument. Kept env-driven; forks that
    #    want it YAML-configurable can add a field later.
    yaml_bearer = _graph_config.auth_token if _graph_config else ""
    auth_env = f"{AGENT_NAME_ENV.upper()}_API_KEY"
    register_a2a_routes(
        app=fastapi_app,
        chat_stream_fn_factory=_chat_langgraph_stream,
        chat_fn=chat,
        api_key=os.environ.get(auth_env, ""),
        auth_token=yaml_bearer,
        agent_card={},
        register_card_route=False,  # card is already served above
        on_terminal=_publish_activity_terminal,  # ADR 0003: surface Activity turns
    )

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

    # --- React operator console --------------------------------------------
    from operator_api.web import mount_react_app

    web_dist_dir = Path(__file__).parent / "apps" / "web" / "dist"
    if mount_react_app(fastapi_app, web_dist_dir):
        log.info("React operator console mounted at /app")

    # --- Static + PWA assets -----------------------------------------------
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
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

    # --- Mount Gradio at root (skipped when headless) -----------------------
    if headless:
        app = fastapi_app
        log.info("Starting %s (headless — no Gradio) on http://0.0.0.0:%d", agent_name(), args.port)
    else:
        import gradio as gr

        app = gr.mount_gradio_app(
            fastapi_app, blocks, path="/",
            footer_links=[],
            favicon_path=str(static_dir / "favicon.svg") if (static_dir / "favicon.svg").exists() else None,
        )
        log.info("Starting %s on http://0.0.0.0:%d", agent_name(), args.port)

    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    _main()
