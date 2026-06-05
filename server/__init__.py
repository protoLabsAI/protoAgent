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
    STATE.graph_config = LangGraphConfig.from_yaml(CONFIG_YAML_PATH)
    # Fork tool denylist (config ``tools.disabled``) — applied before any
    # get_all_tools() call so dropped tools never reach the graph.
    from tools.lg_tools import set_disabled_tools
    set_disabled_tools(STATE.graph_config.tools_disabled)
    # Egress allowlist (ADR 0008): deny-by-default outbound hosts for fetch_url.
    import egress
    egress.set_allowed_hosts(STATE.graph_config.egress_allowed_hosts)
    # Multi-instance scoping (ADR 0004): seed PROTOAGENT_INSTANCE from config so
    # every store (incl. the env-reading knowledge/scheduler/memory modules) nests
    # under the same id. Opt-in — empty config.instance_id leaves paths unchanged.
    # Set before any store is built or the memory middleware is imported.
    _seed_instance_env(STATE.graph_config)
    # Conversation checkpointer: durable SQLite when a path is configured (chat
    # history survives restarts), else in-memory. Bound into the graph at
    # compile time below — a checkpointer in the invoke config is ignored.
    STATE.checkpointer = _build_checkpointer(STATE.graph_config)

    if not is_setup_complete():
        if headless_setup:
            # No wizard in this tier — auto-complete from a validated config,
            # else fail fast (ADR 0010) rather than serve a dead graph.
            ok, reason = validate_for_headless(STATE.graph_config)
            if not ok:
                log.error("Headless setup cannot complete: %s", reason)
                raise SystemExit(2)
            mark_setup_complete()
            log.info("Headless setup auto-completed from a validated config.")
        else:
            STATE.graph = None
            STATE.knowledge_store = None
            # Load plugins for their ROUTES + SURFACES even without a compiled
            # graph. The Connect Discord / Connect Google / Test-connection routes
            # are how the setup wizard *configures* the agent, so they must be
            # mounted during first-run setup — not only after a restart. (Without
            # this the first-run wizard's Connect/Test buttons 404 until the app is
            # relaunched.) register() needs no graph; the tools/subagents that feed
            # the graph are (re)loaded when setup completes and the graph builds.
            _pre = _build_plugins(STATE.graph_config)
            STATE.plugin_routers, STATE.plugin_surfaces, STATE.plugin_meta = (
                _pre.routers, _pre.surfaces, _pre.meta,
            )
            _register_plugin_subagents(_pre.subagents)
            log.info(
                "Setup wizard has not been completed — graph not compiled "
                "(plugin routes/surfaces still mounted). "
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
    STATE.knowledge_store = _build_knowledge_store(STATE.graph_config)

    # Scheduler — local sqlite by default, swaps to a WorkstaceanScheduler
    # automatically when WORKSTACEAN_API_BASE + WORKSTACEAN_API_KEY env
    # vars are set. Both backends share the same agent-tool surface
    # (schedule_task / list_schedules / cancel_schedule).
    STATE.scheduler = _build_scheduler(STATE.graph_config)

    # Plugins — drop-in packages (tools + bundled skills + surfaces/routes +
    # managed MCP servers). Loaded BEFORE MCP so a plugin's managed MCP server
    # (register_mcp_server, e.g. Google) is injected into the MCP discovery
    # below. Collision check uses core tools only — MCP tools are namespaced
    # (<server>__<tool>) so they can't be shadowed by a plugin tool anyway.
    _plugins = _build_plugins(
        STATE.graph_config,
        existing_tools=get_all_tools(STATE.knowledge_store, scheduler=STATE.scheduler),
    )
    STATE.plugin_tools, STATE.plugin_skill_dirs, STATE.plugin_meta = (
        _plugins.tools, _plugins.skill_dirs, _plugins.meta,
    )
    # Surfaces / routes / subagents (ADR 0018). Routers + surfaces are captured
    # here and consumed once by _main (mount) + the startup hook (start) — they
    # don't hot-reload. Subagents register into SUBAGENT_REGISTRY before the graph
    # build below so the first compile (and every reload) can delegate to them.
    # (`global STATE.plugin_routers, STATE.plugin_surfaces` is declared at the top of the fn.)
    STATE.plugin_routers, STATE.plugin_surfaces = _plugins.routers, _plugins.surfaces
    _register_plugin_subagents(_plugins.subagents)

    # MCP — external Model Context Protocol servers; their tools become agent
    # tools (namespaced <server>__<tool>). Off unless mcp.enabled OR a plugin
    # contributes a managed server (ADR 0019).
    STATE.mcp_clients, STATE.mcp_tools, STATE.mcp_meta = _build_mcp(
        STATE.graph_config, plugin_servers=[s["factory"] for s in _plugins.mcp_servers]
    )

    # Skills — human-authored SKILL.md folders (bundle + live + plugin-bundled)
    # seeded into the FTS index; KnowledgeMiddleware retrieves + injects them.
    STATE.skills_index = _build_skills_index(STATE.graph_config, extra_skill_dirs=STATE.plugin_skill_dirs)

    STATE.workflow_registry = _build_workflow_registry(STATE.graph_config)

    STATE.inbox_store = _build_inbox_store(STATE.graph_config)
    if STATE.activity_log is None:
        STATE.activity_log = _build_activity_log(STATE.graph_config)
    from beads import BeadsStore
    if STATE.beads_store is None:  # may have been created early (pre-setup) for the routes
        STATE.beads_store = BeadsStore()  # in-process issue tracker (Sprint B), instance-scoped
    if STATE.storm_guard is None:
        from inbox import StormGuard
        STATE.storm_guard = StormGuard()

    STATE.graph = create_agent_graph(
        STATE.graph_config, knowledge_store=STATE.knowledge_store, scheduler=STATE.scheduler,
        skills_index=STATE.skills_index, extra_tools=STATE.mcp_tools + STATE.plugin_tools,
        checkpointer=STATE.checkpointer, workflow_registry=STATE.workflow_registry,
        inbox_store=STATE.inbox_store, beads_store=STATE.beads_store,
    )

    # Cache-warming heartbeat — off by default; start() no-ops unless enabled
    # for an Anthropic-family model (see graph/cache_warmer.py).
    from graph.cache_warmer import CacheWarmer
    STATE.cache_warmer = CacheWarmer(
        STATE.graph_config, knowledge_store=STATE.knowledge_store, scheduler=STATE.scheduler,
    )

    # Goal mode — parses /goal control messages and runs the goal-completion
    # loop around graph invocations. Machinery only; no goal is active until set.
    if STATE.graph_config.goal_enabled:
        from graph.goals import GoalController, GoalStore
        STATE.goal_controller = GoalController(STATE.graph_config, GoalStore())
    else:
        STATE.goal_controller = None
    log.info(
        "LangGraph agent initialized (model: %s, knowledge_db: %s, scheduler: %s)",
        STATE.graph_config.model_name,
        getattr(STATE.knowledge_store, "path", "(disabled)"),
        getattr(STATE.scheduler, "name", "disabled"),
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
        # Semantic recall (ADR 0021): when knowledge.embeddings is on, use the
        # HybridKnowledgeStore (FTS5 + vector, fused with RRF) with an embed_fn
        # wired to the gateway. Any failure degrades to keyword-only FTS5 — never
        # KB-less — and the store's circuit breaker handles runtime outages.
        if getattr(config, "knowledge_embeddings", False):
            try:
                from graph.llm import create_embed_fn
                from knowledge.hybrid_store import HybridKnowledgeStore

                embed_fn = create_embed_fn(config)
                if embed_fn is not None:
                    log.info("[server] knowledge: hybrid store (FTS5 + embeddings via %s)", config.embed_model)
                    return HybridKnowledgeStore(db_path=config.knowledge_db_path, embed_fn=embed_fn)
                log.warning("[server] knowledge.embeddings on but no embed_model — FTS5 only")
            except Exception as exc:  # noqa: BLE001 — degrade to FTS5, never fail
                log.warning("[server] hybrid store init failed: %s; FTS5 only", exc)
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


def _build_mcp(config, plugin_servers=None):
    """Discover tools from configured MCP servers. Returns (clients, tools, meta).

    ``plugin_servers`` are managed-MCP-server factories contributed by plugins
    (``register_mcp_server``, ADR 0019) — e.g. the Google surface's OAuth-gated
    server — injected alongside the configured ``mcp.servers``.

    Best-effort and per-server isolated (see tools/mcp_tools.build_mcp_tools):
    a bad/unreachable server is logged and skipped, never fatal. Returns empty
    lists when MCP is disabled.
    """
    try:
        from tools.mcp_tools import build_mcp_tools

        clients, tools, meta = build_mcp_tools(config, plugin_servers=plugin_servers)
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
        path = _resolve_checkpoint_db(config.checkpoint_db_path)
        saver = build_sqlite_checkpointer(path)
        STATE.checkpoint_path = path
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
        cfg = STATE.graph_config
        path = STATE.checkpoint_path
        interval_h = getattr(cfg, "checkpoint_prune_interval_hours", 0) if cfg else 0
        if path and cfg and interval_h > 0:
            try:
                max_age = (
                    cfg.checkpoint_max_age_days * 86400 if cfg.checkpoint_max_age_days else None
                )
                harvest = bool(
                    max_age
                    and cfg.checkpoint_harvest_enabled
                    and STATE.knowledge_store is not None
                    and STATE.checkpointer is not None
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
    if STATE.graph_config is not None and getattr(STATE.graph_config, "checkpoint_harvest_enabled", False):
        from graph.conversation_harvest import harvest_thread
        chunk_id = await harvest_thread(
            thread_id,
            checkpointer=STATE.checkpointer,
            knowledge_store=STATE.knowledge_store,
            config=STATE.graph_config,
        )
    if STATE.checkpoint_path:
        await asyncio.to_thread(delete_thread, STATE.checkpoint_path, thread_id)
    elif STATE.checkpointer is not None and hasattr(STATE.checkpointer, "delete_thread"):
        try:
            STATE.checkpointer.delete_thread(thread_id)
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


def _build_activity_log(config):
    """Provenance feed store (ADR 0022). Path resolves like the inbox store
    (/sandbox → ~/.protoagent fallback), namespaced by agent name."""
    from activity import ActivityLog

    name = re.sub(r"[^a-zA-Z0-9._-]", "_", agent_name()) or "agent"
    configured = scope_leaf(Path("/sandbox/activity") / f"{name}.db")
    try:
        configured.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(configured.parent, os.W_OK):
            raise OSError
        path = str(configured)
    except OSError:
        fallback = scope_leaf(Path.home() / ".protoagent" / "activity" / f"{name}.db")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        path = str(fallback)
    try:
        return ActivityLog(path)
    except Exception:
        log.exception("[activity] failed to build log at %s; feed disabled", path)
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
        bundled = _bundle_root() / "workflows"
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
    reload (the trap). We instead schedule it on the captured ``STATE.main_loop`` via
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

    if STATE.main_loop is not None and STATE.main_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(make_coro(), STATE.main_loop)
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
            f"http://127.0.0.1:{STATE.active_port}",
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
    ``STATE.checkpointer`` so active session threads stay addressable
    — a fresh MemorySaver would orphan every in-flight thread.

    Rebinding ``STATE.graph`` is atomic in CPython; in-flight
    ``astream_events`` iterators hold their own reference to the
    prior graph and finish cleanly on the old instance.

    If the setup marker is absent this returns early without
    compiling — the wizard is still in front of the user, so there
    is nothing to hot-swap yet.
    """

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
    # would leave the process serving the prior compiled STATE.graph under
    # fresh STATE.graph_config + rotated bearer auth on failure — the
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
    scheduler_wanted = getattr(new_config, "scheduler_enabled", True)
    next_scheduler: "SchedulerBackend | None"
    pending_start: "SchedulerBackend | None" = None
    pending_stop: "SchedulerBackend | None" = None
    if not scheduler_wanted:
        next_scheduler = None
        pending_stop = STATE.scheduler  # may be None — stopper is no-op then
    elif STATE.scheduler is None:
        next_scheduler = _build_scheduler(new_config)
        pending_start = next_scheduler
    else:
        next_scheduler = STATE.scheduler

    new_store = None
    new_skills = None
    new_mcp_clients, new_mcp_tools, new_mcp_meta = [], [], []
    new_plugin_tools, new_plugin_skill_dirs, new_plugin_meta = [], [], []
    if is_setup_complete():
        try:
            new_store = _build_knowledge_store(new_config)
            # Plugins before MCP — a plugin's managed MCP server (e.g. Google)
            # is injected into the MCP discovery below (matches _main ordering).
            new_plugins = _build_plugins(
                new_config,
                existing_tools=get_all_tools(new_store, scheduler=next_scheduler),
            )
            new_mcp_clients, new_mcp_tools, new_mcp_meta = _build_mcp(
                new_config, plugin_servers=[s["factory"] for s in new_plugins.mcp_servers]
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
                checkpointer=STATE.checkpointer, workflow_registry=new_workflow_registry,
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
    old_config = STATE.graph_config
    # Commit: config → A2A bearer → graph. All three reference the
    # same ``new_config`` so they stay consistent.
    STATE.graph_config = new_config
    STATE.knowledge_store = new_store
    STATE.skills_index = new_skills
    STATE.mcp_clients, STATE.mcp_tools, STATE.mcp_meta = new_mcp_clients, new_mcp_tools, new_mcp_meta
    STATE.plugin_tools, STATE.plugin_skill_dirs, STATE.plugin_meta = (
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
    STATE.graph = new_graph
    STATE.workflow_registry = new_workflow_registry
    STATE.inbox_store = new_inbox_store
    # Commit the scheduler swap. start/stop are async — fire-and-forget
    # onto the active loop so reload stays sync. We've already verified
    # the graph rebuild succeeded; if start/stop fails we log but
    # don't roll back (the agent is already serving the new graph).
    STATE.scheduler = next_scheduler
    if pending_stop is not None:
        _stop_scheduler_async(pending_stop)
    if pending_start is not None:
        _start_scheduler_async(pending_start)

    # Plugin surfaces with a reload hook (ADR 0018/0019) reconnect on a config
    # change without a restart — this is how the Discord plugin live-reconnects
    # when its token/admin/enabled changes (was a bespoke discord_changed block).
    _reload_plugin_surfaces(new_config)

    if new_graph is None:
        log.info("[reload] setup not complete — config reloaded, graph not compiled")
        return True, "config reloaded • setup not complete"

    log.info("LangGraph agent reloaded (model: %s)", STATE.graph_config.model_name)
    return True, f"reloaded • model={STATE.graph_config.model_name}"


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
        HOST.config = lambda: STATE.graph_config
        HOST.apply_settings = lambda patch: _apply_settings_changes(config=patch)
    except Exception:  # noqa: BLE001
        log.exception("[plugins] failed to populate plugin host")


def _reload_plugin_surfaces(new_config) -> None:
    """Notify started plugin surfaces of a config change (ADR 0018/0019).

    Each surface that registered a ``reload`` callback gets it called with the new
    ``LangGraphConfig`` on the server loop, so a migrated Discord/Google-style
    surface can reconnect on a Settings save without a restart. Best-effort.
    """
    for h in STATE.plugin_surface_handles:
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
            or (STATE.graph_config.identity_name if STATE.graph_config else "")
            or "protoagent"
        )
        if want:
            ok, msg = install_autostart(agent_name=as_name, port=STATE.active_port)
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
        return config_to_dict(STATE.graph_config)

    def list_models(api_base: str = "", api_key: str = "") -> tuple[list[str], str]:
        """UI-friendly model lookup.

        Uses the form-local api_base/api_key when the user is trying a
        different endpoint before saving; falls back to the currently
        loaded graph config so the initial render works without
        arguments.
        """
        base = api_base or (STATE.graph_config.api_base if STATE.graph_config else "")
        key = api_key or (STATE.graph_config.api_key if STATE.graph_config else "")
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
           setup-incomplete and stays ``STATE.graph = None``.
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
            test_base = m.get("api_base") or (STATE.graph_config.api_base if STATE.graph_config else "")
            test_key = m.get("api_key") or (STATE.graph_config.api_key if STATE.graph_config else "")
            test_model = m.get("name") or (STATE.graph_config.model_name if STATE.graph_config else "")
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

            name = (STATE.graph_config.identity_name if STATE.graph_config else "") or "protoagent"
            return autostart_status(name)
        except Exception as e:
            return {"supported": False, "installed": False, "reason": str(e)}

    def toggle_autostart(enabled: bool) -> tuple[bool, str]:
        """Install or uninstall the OS autostart artifact, mirroring
        the YAML field. Called from the drawer's checkbox handler so
        toggling takes effect immediately without waiting for Save."""
        try:
            from autostart import install_autostart, uninstall_autostart

            name = (STATE.graph_config.identity_name if STATE.graph_config else "") or "protoagent"
            if enabled:
                return install_autostart(agent_name=name, port=STATE.active_port)
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
