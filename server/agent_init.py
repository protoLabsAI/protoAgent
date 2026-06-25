"""Agent initialization, the component builders, hot-reload, and settings.

Extracted from ``server/__init__.py`` (ADR 0023, phase 2). This module owns the
composition of the LangGraph agent from config: ``_init_langgraph_agent`` and the
``_build_*`` builders (knowledge / skills / MCP / plugins / checkpointer / inbox /
activity / telemetry / workflow / scheduler), the checkpoint-prune + thread-retire
loops, the plugin host wiring, ``_reload_langgraph_agent`` (the hot-reload path),
and the settings-callbacks the operator console drives.

The builders read and mutate the shared ``runtime.state.STATE`` container; the few
``server/__init__`` symbols they need (``agent_name``, ``AGENT_NAME_ENV``,
``_event_bus``, ``_bundle_root``) are imported from ``server`` — all defined
before the re-export line in ``__init__`` that triggers this import, so it is not
a cycle. ``server/__init__.py`` re-exports every public name so ``server.<symbol>``
keeps resolving for ``_main``'s wiring and for the test suite.
"""

import asyncio
import functools
import logging
import os
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from infra.paths import scope_leaf
from runtime.state import STATE
from server import AGENT_NAME_ENV, _event_bus, agent_name
from server.chat import chat

if TYPE_CHECKING:
    from scheduler.interface import SchedulerBackend

log = logging.getLogger("protoagent.server")

# Serializes every config read-modify-write + graph reload. The callers all
# run on worker threads (asyncio.to_thread from the routes), and a fleet hub
# makes concurrent saves routine (two console windows, a settings save racing
# a plugin toggle). Without this: classic lost-update on the YAML, and two
# interleaved reloads can commit graph A with STATE.graph_config B — the exact
# de-sync the reload path's build-then-commit choreography assumes can't
# happen. RLock because _apply_settings_changes/_reset_settings_keys call
# _reload_langgraph_agent, which is also lockable on its own (plugin routes
# call it directly).
_CONFIG_WRITE_LOCK = threading.RLock()


def _serialized_config_write(fn):
    """Run ``fn`` under ``_CONFIG_WRITE_LOCK`` (config RMW + reload guard)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _CONFIG_WRITE_LOCK:
            return fn(*args, **kwargs)

    return wrapper


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

    # Warn loudly if running UNSCOPED while the data home already has state — an unscoped
    # instance shares the loose root and can clobber a co-located sibling (#706).
    from infra.paths import unscoped_warning

    _unscoped = unscoped_warning()
    if _unscoped:
        log.warning("[instance] %s", _unscoped)

    # Data-dir version check (migration anchor): stamp or warn before any store opens.
    from infra.paths import check_data_version

    _dv_warn = check_data_version()
    if _dv_warn:
        log.warning("[data-version] %s", _dv_warn)

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
    from security import egress

    egress.set_allowed_hosts(STATE.graph_config.egress_allowed_hosts)
    # Opt-in CIDR allowlist for outbound A2A destinations — callbacks + delegate_to a2a delegates (#572).
    from security import policy

    policy.set_callback_allowlist(STATE.graph_config.security_callback_allowlist)
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
                _pre.routers,
                _pre.surfaces,
                _pre.meta,
            )
            STATE.plugin_public_paths = _pre.public_paths
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
    # memory_recall, memory_list, memory_stats) and KnowledgeMiddleware have something to
    # bind to. Forks that don't want a store can set
    # ``middleware.knowledge: false`` and remove the memory tools from
    # the worker subagent — the store is still cheap to construct.
    STATE.knowledge_store = _build_knowledge_store(STATE.graph_config)

    # Scheduler — the bundled local sqlite backend (or None when disabled).
    # Agent-tool surface: schedule_task / list_schedules / cancel_schedule.
    STATE.scheduler = _build_scheduler(STATE.graph_config)

    # Plugins — drop-in packages (tools + bundled skills + surfaces/routes +
    # managed MCP servers). Loaded BEFORE MCP so a plugin's managed MCP server
    # (register_mcp_server, e.g. Google) is injected into the MCP discovery
    # below. Collision check uses core tools only — MCP tools are namespaced
    # (<server>__<tool>) so they can't be shadowed by a plugin tool anyway.
    _plugins = _build_plugins(
        STATE.graph_config,
        existing_tools=get_all_tools(
            STATE.knowledge_store,
            scheduler=STATE.scheduler,
            goal_enabled=getattr(STATE.graph_config, "goal_enabled", True),
        ),
    )
    STATE.plugin_tools, STATE.plugin_skill_dirs, STATE.plugin_meta = (
        _plugins.tools,
        _plugins.skill_dirs,
        _plugins.meta,
    )
    STATE.plugin_workflow_dirs = _plugins.workflow_dirs
    STATE.plugin_a2a_skills = _plugins.a2a_skills  # A2A card skills (#570)
    STATE.plugin_chat_commands = _plugins.chat_commands  # user-only /<name> control commands
    STATE.thread_id_resolver = _plugins.thread_id_resolver  # thread_id seam (#571)
    # A plugin may provide the knowledge backend (ADR 0031) — swap it in now (the
    # graph compiles below with STATE.knowledge_store). Default built-in store stays
    # the collision-check binding + the degrade-safe fallback.
    STATE.knowledge_store = _apply_plugin_knowledge_backend(STATE.graph_config, STATE.knowledge_store, _plugins)
    # Register plugin-contributed goal verifiers (ADR 0028) — re-set on each
    # (re)load so a config change refreshes the available `plugin` verifiers.
    from graph.goals import hooks as _goal_hooks
    from graph.goals import verifiers as _goal_verifiers

    _goal_verifiers.set_plugin_verifiers(_plugins.goal_verifiers)
    _goal_hooks.set_goal_hooks(_plugins.goal_hooks)
    # Surfaces / routes / subagents (ADR 0018). Routers + surfaces are captured
    # here and consumed once by _main (mount) + the startup hook (start) — they
    # don't hot-reload. Subagents register into SUBAGENT_REGISTRY before the graph
    # build below so the first compile (and every reload) can delegate to them.
    # (`global STATE.plugin_routers, STATE.plugin_surfaces` is declared at the top of the fn.)
    STATE.plugin_routers, STATE.plugin_surfaces = _plugins.routers, _plugins.surfaces
    STATE.plugin_public_paths = _plugins.public_paths
    _register_plugin_subagents(_plugins.subagents)
    _apply_config_subagents(STATE.graph_config)  # YAML subagent overrides (tools/max_turns/model)
    STATE.plugin_middleware = _resolve_plugin_middleware(STATE.graph_config, _plugins.middleware)  # ADR 0032
    STATE.plugin_late_tool_factories = _plugins.late_tool_factories  # late-tools seam

    # MCP — external Model Context Protocol servers; their tools become agent
    # tools (namespaced <server>__<tool>). Off unless mcp.enabled OR a plugin
    # contributes a managed server (ADR 0019).
    STATE.mcp_clients, STATE.mcp_tools, STATE.mcp_meta = _build_mcp(
        STATE.graph_config, plugin_servers=[s["factory"] for s in _plugins.mcp_servers]
    )

    # Skills — human-authored SKILL.md folders (bundle + live + plugin-bundled)
    # seeded into the FTS index; KnowledgeMiddleware retrieves + injects them.
    STATE.skills_index = _build_skills_index(STATE.graph_config, extra_skill_dirs=STATE.plugin_skill_dirs)

    # STATE.workflow_registry is set by the workflows plugin (plugins/workflows) when
    # enabled — core no longer builds it (lean core, opt-in).

    STATE.inbox_store = _build_inbox_store(STATE.graph_config)
    if STATE.activity_log is None:
        STATE.activity_log = _build_activity_log(STATE.graph_config)
    from tasks import TaskStore

    if STATE.tasks_store is None:  # may have been created early (pre-setup) for the routes
        STATE.tasks_store = TaskStore()  # in-process issue tracker (Sprint B), instance-scoped
    if STATE.storm_guard is None:
        from inbox import StormGuard

        STATE.storm_guard = StormGuard()

    # Background subagent manager (ADR 0050) — must exist before the graph build so
    # the `task` tool's run_in_background path can reach it.
    STATE.background_mgr = _build_background_manager(STATE.graph_config)

    STATE.graph = create_agent_graph(
        STATE.graph_config,
        knowledge_store=STATE.knowledge_store,
        scheduler=STATE.scheduler,
        skills_index=STATE.skills_index,
        extra_tools=STATE.mcp_tools + STATE.plugin_tools,
        extra_middleware=STATE.plugin_middleware,
        late_tool_factories=STATE.plugin_late_tool_factories,
        checkpointer=STATE.checkpointer,
        inbox_store=STATE.inbox_store,
        tasks_store=STATE.tasks_store,
        background_mgr=STATE.background_mgr,
    )

    # Cache-warming heartbeat — off by default; start() no-ops unless enabled
    # for an Anthropic-family model (see graph/cache_warmer.py).
    from graph.cache_warmer import CacheWarmer

    STATE.cache_warmer = CacheWarmer(
        STATE.graph_config,
        knowledge_store=STATE.knowledge_store,
        scheduler=STATE.scheduler,
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
    """Return a ``KnowledgeStore`` — or a tiered store (ADR 0041 / bd-2wu) — bound to the
    configured DB path(s).

    ``knowledge.scope`` selects the tier: ``scoped`` (private, **default**) · ``shared``
    (the whole store is the host-level commons) · ``layered`` (read commons ∪ private,
    write private, operator-``promote``d). The commons is host-level + un-scoped — every
    agent on the box reads ``commons.path``/knowledge.db regardless of ``instance.id``.
    A fleet sharing a commons must share one embed model — **enforced**: the commons is
    stamped with the embed model it was built on, and an agent whose model differs serves
    the commons tier FTS5-only (no vector fusion of incompatible embeddings).

    Best-effort: failures degrade (hybrid→FTS5, never KB-less); returns ``None`` only when
    knowledge is disabled.
    """
    if not getattr(config, "knowledge_middleware", True):
        return None
    try:
        from knowledge import KnowledgeStore

        # Contextual Retrieval (ADR 0021): (doc, chunk) -> context fn, shared by both tiers.
        context_fn = None
        if getattr(config, "knowledge_contextual_enrichment", False):
            try:
                from graph.llm import create_context_fn

                context_fn = create_context_fn(config)
                if context_fn is not None:
                    log.info("[server] knowledge: contextual enrichment on (aux model)")
            except Exception as exc:  # noqa: BLE001 — enrichment is optional
                log.warning("[server] context fn init failed: %s; enrichment off", exc)

        # Semantic recall (ADR 0021): build the embed fns ONCE (hoisted so both tiers
        # share them). None → keyword-only FTS5 everywhere; failures degrade, never fail.
        embed_fn = embed_batch_fn = None
        if getattr(config, "knowledge_embeddings", False):
            try:
                from graph.llm import create_embed_batch_fn, create_embed_fn

                embed_fn = create_embed_fn(config)
                embed_batch_fn = create_embed_batch_fn(config) if embed_fn is not None else None
                if embed_fn is None:
                    log.warning("[server] knowledge.embeddings on but no embed_model — FTS5 only")
            except Exception as exc:  # noqa: BLE001
                log.warning("[server] embed fn init failed: %s; FTS5 only", exc)
                embed_fn = embed_batch_fn = None

        def _make(db_path, *, scoped, force_plain=False):
            """Build ONE store at *db_path* — hybrid when embeddings are on (unless
            *force_plain*, used for an embed-model-mismatched commons), else plain FTS5."""
            if embed_fn is not None and not force_plain:
                from knowledge.hybrid_store import HybridKnowledgeStore

                return HybridKnowledgeStore(
                    db_path=db_path, scoped=scoped, embed_fn=embed_fn, embed_batch_fn=embed_batch_fn,
                    vector_k=config.knowledge_vector_k, rrf_k=config.knowledge_rrf_k,
                    min_score=config.knowledge_min_score,
                    breaker_threshold=config.knowledge_embed_breaker_threshold,
                    breaker_cooldown_s=config.knowledge_embed_breaker_cooldown_s,
                    preview_chars=config.knowledge_recall_preview_chars,
                    chunk_max_chars=config.knowledge_chunk_max_chars,
                    chunk_overlap_chars=config.knowledge_chunk_overlap_chars,
                    chunk_min_chars=config.knowledge_chunk_min_chars, context_fn=context_fn,
                )
            return KnowledgeStore(
                db_path=db_path, scoped=scoped,
                preview_chars=config.knowledge_recall_preview_chars,
                chunk_max_chars=config.knowledge_chunk_max_chars,
                chunk_overlap_chars=config.knowledge_chunk_overlap_chars,
                chunk_min_chars=config.knowledge_chunk_min_chars, context_fn=context_fn,
            )

        private = _make(config.knowledge_db_path, scoped=True)

        scope = (getattr(config, "knowledge_scope", "") or "").strip().lower()
        if scope not in ("scoped", "shared", "layered"):
            scope = "scoped"
        if scope == "scoped":
            log.info("[knowledge] tier=scoped into %s", private.path)
            return private

        # shared/layered → build the host-level commons, enforcing one-fleet-one-embed-model.
        commons_path = str(_commons_dir(config) / "knowledge.db")
        force_plain = False
        if embed_fn is not None:
            stamp = KnowledgeStore(db_path=commons_path, scoped=False)  # creates schema + _kb_meta
            stamped = stamp.get_meta("embed_model")
            want = config.embed_model or ""
            if stamped is None:
                stamp.set_meta("embed_model", want)  # first build → this fleet claims the commons
            elif stamped != want:
                force_plain = True
                log.warning(
                    "[knowledge] commons %s was built with embed model %r but this agent uses %r — "
                    "serving the commons tier FTS5-only (no vector fusion). Align the fleet's embed_model, "
                    "or point this agent at a different commons.path.",
                    commons_path, stamped, want,
                )
        commons = _make(commons_path, scoped=False, force_plain=force_plain)

        if scope == "shared":
            log.info("[knowledge] tier=shared (commons) into %s", commons.path)
            return commons
        from knowledge.layered import LayeredKnowledgeStore

        log.info("[knowledge] tier=layered (%s ∪ %s)", private.path, commons.path)
        return LayeredKnowledgeStore(private, commons)
    except Exception as exc:
        log.warning("[server] knowledge store init failed: %s; running KB-less", exc)
        return None


def _apply_plugin_knowledge_backend(config, store, plugins):
    """ADR 0031 — swap in a plugin-provided knowledge **backend** (``knowledge.backend``)
    or, failing that, a plugin **embedder** for the built-in hybrid store
    (``knowledge.embedder``), selected by config. Degrade-safe: an unregistered name,
    a None return, or a factory error keeps ``store`` (never KB-less by surprise).
    Called after plugins load, at both init and reload."""
    backend = (getattr(config, "knowledge_backend", "") or "").strip()
    if backend:
        factory = (getattr(plugins, "knowledge_stores", {}) or {}).get(backend)
        if factory is None:
            log.warning("[server] knowledge.backend %r not registered by any plugin — built-in store", backend)
            return store
        try:
            built = factory(config)
        except Exception as exc:  # noqa: BLE001 — degrade to the built-in store
            log.warning("[server] knowledge backend %r failed: %s — built-in store", backend, exc)
            return store
        if built is None:
            log.warning("[server] knowledge backend %r returned None — built-in store", backend)
            return store
        log.info("[server] knowledge: plugin backend %r", backend)
        return built
    # No plugin store selected — maybe a plugin embedder for the built-in hybrid store.
    embedder = (getattr(config, "knowledge_embedder", "") or "").strip()
    if embedder:
        return _apply_plugin_embedder(config, store, plugins, embedder)
    return store


def _apply_plugin_embedder(config, store, plugins, name):
    """ADR 0031 follow-up — rebuild the built-in store as a HybridKnowledgeStore using
    a plugin-registered in-process embedder (``register_embedder``). Degrade-safe:
    unregistered / None / error keeps ``store`` (the gateway-embedder one)."""
    factory = (getattr(plugins, "embedders", {}) or {}).get(name)
    if factory is None:
        log.warning("[server] knowledge.embedder %r not registered by any plugin — gateway embedder", name)
        return store
    try:
        embed_fn = factory(config)
    except Exception as exc:  # noqa: BLE001
        log.warning("[server] embedder %r failed: %s — gateway embedder", name, exc)
        return store
    if embed_fn is None:
        log.warning("[server] embedder %r returned None — gateway embedder", name)
        return store
    try:
        from knowledge.hybrid_store import HybridKnowledgeStore

        rebuilt = HybridKnowledgeStore(db_path=config.knowledge_db_path, embed_fn=embed_fn)
        log.info("[server] knowledge: hybrid store with plugin embedder %r", name)
        return rebuilt
    except Exception as exc:  # noqa: BLE001
        log.warning("[server] hybrid store w/ embedder %r failed: %s — built-in store", name, exc)
        return store


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

        # Tier (ADR 0041): scoped (private) | shared (one commons) | layered
        # (read commons ∪ private, write private). Blank scope → derived from the
        # slice-1 `shared` bool for back-compat.
        scope = (getattr(config, "skills_scope", "") or "").strip().lower()
        if scope not in ("scoped", "shared", "layered"):
            scope = "shared" if getattr(config, "skills_shared", False) else "scoped"
        commons = _commons_dir(config)
        if scope == "layered":
            from graph.skills.layered import LayeredSkillsIndex

            private_path = _resolve_skills_db(config.skills_db_path, shared=False)
            shared_path = _resolve_skills_db(config.skills_db_path, shared=True, commons=commons)
            index = LayeredSkillsIndex(SkillsIndex(db_path=private_path), SkillsIndex(db_path=shared_path))
            db_path = f"layered({private_path} ∪ {shared_path})"
        else:
            db_path = _resolve_skills_db(config.skills_db_path, shared=(scope == "shared"), commons=commons)
            index = SkillsIndex(db_path=db_path)

        live_root = Path(config.skills_dir).expanduser() if config.skills_dir else (_live_config_dir() / "skills")
        roots = [_BUNDLE_CONFIG_DIR / "skills", live_root]  # bundle first, live overrides
        roots.extend(Path(d) for d in (extra_skill_dirs or []))  # plugin-bundled skills
        # Operator-authored skills (UI/console CRUD) live under the data home and
        # win last — an explicit edit always overrides a bundled/plugin example.
        from infra.paths import user_skills_dir

        roots.append(user_skills_dir())
        count = seed_skills_index(index, roots)
        # Name the tier explicitly: a `shared`/`layered` commons is host-level and
        # un-scoped (every agent on the box reads it), so making that visible at boot
        # guards the shared-host footgun (ADR 0041).
        log.info("[skills] tier=%s — indexed %d SKILL.md skill(s) into %s", scope, count, db_path)
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


def _resolve_plugin_middleware(config, factories) -> list:
    """Resolve plugin middleware factories ``(config) -> AgentMiddleware|None`` to
    instances (ADR 0032). Best-effort: a factory that raises or returns None is
    skipped + logged, so one bad plugin can't take down the graph build."""
    out = []
    for factory in factories or []:
        try:
            mw = factory(config)
        except Exception:  # noqa: BLE001
            log.exception("[plugins] middleware factory failed; skipping")
            continue
        if mw is not None:
            out.append(mw)
    if out:
        log.info("[plugins] %d middleware contributed", len(out))
    return out


# Built-in subagents whose runtime config the operator can override in YAML
# (subagents.<name>.{enabled,tools,max_turns,model}). Add an entry here + a
# SubagentDef field on LangGraphConfig when you make another built-in overridable.
_OVERRIDABLE_SUBAGENTS = ("researcher",)


def _apply_config_subagents(config) -> None:
    """Apply the YAML subagent override (``subagents.<name>``: enabled / tools /
    max_turns / model) onto the built-in registry entries — what makes the documented
    knobs actually take effect at runtime (the resolution path in ``_run_subagent``
    already existed). Derives each entry from its static default (SSOT, so it's
    idempotent across reloads and an un-overridden config is a true no-op);
    ``enabled: false`` removes the subagent (not delegatable). Runs at init + reload."""
    try:
        from dataclasses import replace

        from graph.subagents import config as _sub
        from graph.subagents.config import SUBAGENT_REGISTRY
    except Exception:  # noqa: BLE001
        return
    bases = {"researcher": getattr(_sub, "RESEARCHER_CONFIG", None)}
    for name in _OVERRIDABLE_SUBAGENTS:
        base = bases.get(name)
        sub = getattr(config, name, None)
        if base is None or sub is None:
            continue
        if not getattr(sub, "enabled", True):
            SUBAGENT_REGISTRY.pop(name, None)  # disabled → not delegatable
            continue
        SUBAGENT_REGISTRY[name] = replace(
            base,
            tools=list(sub.tools) if sub.tools else list(base.tools),
            max_turns=sub.max_turns or base.max_turns,
            model=(sub.model or "").strip() or base.model,
        )


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
            log.info("[plugins] loaded %d plugin(s): %s", len(loaded), ", ".join(m["id"] for m in loaded))
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
                max_age = cfg.checkpoint_max_age_days * 86400 if cfg.checkpoint_max_age_days else None
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
                    background_keep=cfg.checkpoint_background_keep,
                )
                if res["threads_deleted"] or res["checkpoints_deleted"]:
                    log.info(
                        "[checkpoint-prune] removed %d idle thread(s), %d old checkpoint(s)",
                        res["threads_deleted"],
                        res["checkpoints_deleted"],
                    )
                    # Reclaim freed space back to the OS (compact WAL + pages).
                    if getattr(cfg, "checkpoint_vacuum", True):
                        try:
                            from graph.checkpoint_prune import reclaim as _reclaim

                            vac = await asyncio.to_thread(_reclaim, path)
                            if vac["wal_truncated"] or vac["pages_reclaimed"]:
                                log.info(
                                    "[checkpoint-prune] reclaimed WAL=%d pages=%d",
                                    vac["wal_truncated"],
                                    vac["pages_reclaimed"],
                                )
                        except Exception:
                            log.exception("[checkpoint-prune] reclaim failed")
            except Exception:
                log.exception("[checkpoint-prune] sweep failed")
        # Telemetry retention guardrail (ADR 0006) — drop turns older than the
        # configured window so the per-turn store can't grow unbounded. 0 = keep all.
        keep_days = getattr(cfg, "telemetry_retention_days", 0) if cfg else 0
        if STATE.telemetry_store is not None and keep_days > 0:
            try:
                removed = await asyncio.to_thread(STATE.telemetry_store.prune, keep_days)
                if removed:
                    log.info("[telemetry-prune] removed %d turn(s) older than %dd", removed, keep_days)
            except Exception:
                log.exception("[telemetry-prune] sweep failed")
        # Inbox retention — delete delivered items older than the configured window
        # so the inbox DB can't grow unbounded. Pending (undelivered) items are never
        # pruned. 0 = keep all.
        inbox_keep = getattr(cfg, "inbox_retention_days", 0) if cfg else 0
        if STATE.inbox_store is not None and inbox_keep > 0:
            try:
                removed = await asyncio.to_thread(STATE.inbox_store.prune, inbox_keep)
                if removed:
                    log.info("[inbox-prune] removed %d delivered item(s) older than %dd", removed, inbox_keep)
            except Exception:
                log.exception("[inbox-prune] sweep failed")
        # Activity retention — delete feed entries older than the configured window
        # so the activity DB can't grow unbounded. 0 = keep all.
        activity_keep = getattr(cfg, "activity_retention_days", 0) if cfg else 0
        if STATE.activity_log is not None and activity_keep > 0:
            try:
                removed = await asyncio.to_thread(STATE.activity_log.prune, activity_keep)
                if removed:
                    log.info("[activity-prune] removed %d entry(ies) older than %dd", removed, activity_keep)
            except Exception:
                log.exception("[activity-prune] sweep failed")
        # A2A task TTL sweep (24h) — used to run only at boot, so an always-on
        # agent accumulated task rows forever between restarts. The store is
        # async (aiosqlite engine on this same loop), so it's awaited directly
        # rather than dispatched to a thread like the sync sqlite neighbors.
        if STATE.a2a_task_engine is not None:
            try:
                from a2a_impl.stores import sweep_expired_tasks

                swept = await sweep_expired_tasks(STATE.a2a_task_engine)
                if swept:
                    log.info("[a2a-task-prune] removed %d expired task record(s) (24h TTL)", swept)
                # Drop push-notification configs orphaned by the task sweep (ADR 0051).
                if STATE.a2a_push_engine is not None:
                    from a2a_impl.stores import sweep_orphaned_push_configs

                    orphaned = await sweep_orphaned_push_configs(STATE.a2a_task_engine, STATE.a2a_push_engine)
                    if orphaned:
                        log.info("[a2a-task-prune] removed %d orphaned push-config(s)", orphaned)
            except Exception:
                log.exception("[a2a-task-prune] sweep failed")
        # Tick at the checkpoint interval if set, else hourly (so telemetry pruning
        # still runs when checkpoint pruning is off).
        await asyncio.sleep(max(1, interval_h or 1) * 3600)


async def _monitor_goals_loop() -> None:
    """Out-of-band cadence for monitor goals (ADR 0030 D2.1): periodically run each
    active monitor goal's verifier — no agent turn, no model call — so a met
    long-horizon objective finishes (firing its on_achieved hook) without waiting
    for a session turn. Verifier-only; the `drive` loop is untouched."""
    await asyncio.sleep(15)  # let boot settle before the first tick
    while True:
        ctrl = STATE.goal_controller
        cfg = STATE.graph_config
        interval = getattr(cfg, "goal_monitor_interval", 60) if cfg else 60
        if ctrl is not None:
            try:
                n = await ctrl.tick_monitor_goals()
                if n:
                    log.info("[goal-monitor] %d monitor goal(s) reached a terminal state", n)
            except Exception:
                log.exception("[goal-monitor] tick failed")
        await asyncio.sleep(max(5, interval))


async def _retire_thread(thread_id: str, *, harvest: bool | None = None, cascade: bool = True) -> str | None:
    """Harvest a thread to the knowledge base (best-effort) then delete its
    checkpoints. Shared by the prune sweep and explicit tab deletion. Returns
    the harvested knowledge chunk id, if any.

    ``harvest`` — ``None`` defers to ``checkpoint_harvest_enabled`` (the TTL
    sweep's config-driven default); an explicit bool overrides it (the
    delete-chat dialog's opt-in checkbox: an unchecked box must not harvest
    just because the sweep is configured to, and a checked box is an explicit
    operator request).

    ``cascade`` — when True (the default), also deletes any
    ``:goal-iter-N`` sub-threads so goal-mode iteration checkpoints are not
    orphaned."""
    import asyncio

    from graph.checkpoint_prune import delete_thread

    chunk_id = None
    do_harvest = getattr(STATE.graph_config, "checkpoint_harvest_enabled", False) if harvest is None else harvest
    if STATE.graph_config is not None and do_harvest:
        from graph.conversation_harvest import harvest_thread

        chunk_id = await harvest_thread(
            thread_id,
            checkpointer=STATE.checkpointer,
            knowledge_store=STATE.knowledge_store,
            config=STATE.graph_config,
        )
    if STATE.checkpoint_path:
        await asyncio.to_thread(delete_thread, STATE.checkpoint_path, thread_id, cascade=cascade)
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


def _build_background_manager(config):
    """Background subagent manager (ADR 0050). Fires detached jobs as self-POSTed A2A
    turns, so it derives the invoke URL + auth exactly like ``_build_scheduler`` (so a
    wizard rename can't break self-invocation). The store path resolves like the other
    stores (/sandbox → ~/.protoagent fallback), namespaced by agent name. Reconciles any
    job left ``running`` by a prior crash on startup. Returns ``None`` when disabled or
    the store can't be built (the ``task`` tool then falls back to synchronous execution)."""
    if os.environ.get("BACKGROUND_DISABLED", "").lower() in ("1", "true", "yes"):
        log.info("[background] disabled via BACKGROUND_DISABLED env")
        return None
    from background import BackgroundManager, BackgroundStore

    name = re.sub(r"[^a-zA-Z0-9._-]", "_", agent_name()) or "agent"
    configured = scope_leaf(Path("/sandbox/background") / f"{name}.db")
    try:
        configured.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(configured.parent, os.W_OK):
            raise OSError
        path = str(configured)
    except OSError:
        fallback = scope_leaf(Path.home() / ".protoagent" / "background" / f"{name}.db")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        path = str(fallback)
    try:
        store = BackgroundStore(path)
    except Exception:
        log.exception("[background] failed to build store at %s; background disabled", path)
        return None
    try:
        reconciled = store.reconcile_interrupted()
        if reconciled:
            log.info("[background] reconciled %d interrupted job(s) on startup", reconciled)
    except Exception:
        log.exception("[background] startup reconcile failed")

    invoke_url = os.environ.get(
        "SCHEDULER_INVOKE_URL",
        f"http://127.0.0.1:{STATE.active_port}",
    )
    bearer = (config.auth_token or os.environ.get("A2A_AUTH_TOKEN", "")).strip()
    # Match the X-API-Key the A2A handler reads (env-derived name, NOT identity.name) —
    # see _build_scheduler for the rationale.
    api_key = os.environ.get(f"{AGENT_NAME_ENV.upper()}_API_KEY", "").strip()
    # The event bus → a still-open spawning chat gets a live ``background.started``
    # push (completion is published by the terminal hook). Imported lazily to keep
    # this builder import-cheap; tolerate its absence.
    try:
        from server import _event_bus

        publish = _event_bus.publish
    except Exception:  # noqa: BLE001
        publish = None
    try:
        return BackgroundManager(
            agent_name=name,
            invoke_url=invoke_url,
            store=store,
            api_key=api_key,
            bearer_token=bearer,
            event_publish=publish,
        )
    except Exception:
        log.exception("[background] manager init failed; background disabled")
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
    from observability.telemetry_store import TelemetryStore

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


def _commons_dir(config):
    """The shared commons base (ADR 0041) — read by every agent on the host, never
    per-instance scoped. Configurable via ``commons.path``; defaults to
    ``~/.protoagent/commons``."""
    from pathlib import Path

    raw = (getattr(config, "commons_path", "") or "").strip()
    return Path(raw).expanduser() if raw else (Path.home() / ".protoagent" / "commons")


def _resolve_skills_db(configured: str, *, shared: bool = False, commons=None) -> str:
    """Pick a writable skills DB path.

    When ``shared`` (ADR 0041, tiered stores), the skills library is the COMMONS:
    resolved un-scoped so every agent on the host shares one DB. Otherwise it's
    per-instance scoped (``scope_leaf``), falling back to ~/.protoagent when the
    configured dir (default /sandbox) isn't creatable."""
    import os
    from pathlib import Path

    if shared:
        path = Path(commons or (Path.home() / ".protoagent" / "commons")) / "skills.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

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
    """Return the scheduler backend (the bundled sqlite ``LocalScheduler``), or
    ``None`` when scheduling is disabled.

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
        try:
            from server import _event_bus

            publish = _event_bus.publish
        except Exception:  # noqa: BLE001
            publish = None
        return LocalScheduler(
            agent_name=name,
            invoke_url=invoke_url,
            api_key=api_key,
            bearer_token=bearer,
            event_publish=publish,  # scheduler.fired on the bus (ADR 0051)
        )
    except Exception as exc:
        log.warning(
            "[server] LocalScheduler init failed: %s; running scheduler-less",
            exc,
        )
        return None


def _mount_plugin_routers(routers: list[dict]) -> None:
    """Mount plugin routers (ADR 0018) onto the live app, skipping any already
    mounted (keyed ``(plugin_id, prefix)``). Called at boot AND on every config
    reload, so enabling a route-bearing plugin (e.g. ``delegates``) takes effect
    without a restart — the #797 fleet blocker ("hot-reload rebuilds the graph
    but routes bind at startup"). FastAPI accepts ``include_router`` after
    startup; new routes are appended (no existing /api catch-all shadows them).

    Plugin prefix enforcement (#870): plugin routes SHOULD live under
    ``/plugins/<id>/...``. Routes at non-conforming prefixes are still mounted
    (many plugins legitimately use ``/api/...`` prefixes for their data routes)
    but a WARNING is logged. The default-deny auth middleware guards all
    non-public paths regardless of prefix, so the security gap is closed.

    Disabling can't UNmount (FastAPI has no route-removal API) — a disabled
    plugin's routes stay until restart, same as before this helper existed.
    Best-effort per router so one bad plugin can't break boot or a reload."""
    app = STATE.fastapi_app
    if app is None:
        return
    for r in routers:
        plugin_id = r.get("plugin_id", "")
        prefix = r.get("prefix") or ""
        key = (plugin_id, prefix)
        if key in STATE.plugin_router_keys:
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
        # Warn for non-conforming prefixes (#870 plugin prefix enforcement).
        # The convention is /plugins/<id>/...; routes under other prefixes are
        # still mounted (existing plugins use /api/... for data routes) but
        # the warning surfaces mis-configurations. The default-deny middleware
        # guards all non-public paths regardless of prefix.
        if plugin_id and prefix and not prefix.startswith(f"/plugins/{plugin_id}"):
            log.warning(
                "[plugins] %s: router prefix %r does not start with /plugins/%s/ "
                "— plugin routes SHOULD live under /plugins/<id>/ (mounted as-is; "
                "the default-deny auth middleware guards it)",
                plugin_id,
                prefix,
                plugin_id,
            )
        try:
            app.include_router(r["router"], prefix=prefix)
            STATE.plugin_router_keys.add(key)
            log.info("[plugins] mounted router from %s at %s", plugin_id, prefix or "/")
        except Exception:  # noqa: BLE001
            log.exception("[plugins] failed to mount router from %s", plugin_id)


@_serialized_config_write
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
    new_plugin_chat_commands: dict = {}  # user-only /<name> control commands
    if is_setup_complete():
        try:
            new_store = _build_knowledge_store(new_config)
            # The workflows plugin re-sets these in its register() if still enabled;
            # reset first so disabling it on reload leaves them cleared.
            STATE.workflow_registry = STATE.workflow_run = None
            # Plugins before MCP — a plugin's managed MCP server (e.g. Google)
            # is injected into the MCP discovery below (matches _main ordering).
            new_plugins = _build_plugins(
                new_config,
                existing_tools=get_all_tools(
                    new_store, scheduler=next_scheduler, goal_enabled=getattr(new_config, "goal_enabled", True)
                ),
            )
            new_mcp_clients, new_mcp_tools, new_mcp_meta = _build_mcp(
                new_config, plugin_servers=[s["factory"] for s in new_plugins.mcp_servers]
            )
            new_plugin_tools = new_plugins.tools
            new_plugin_skill_dirs = new_plugins.skill_dirs
            new_plugin_meta = new_plugins.meta
            new_plugin_chat_commands = new_plugins.chat_commands  # user-only /<name> control commands
            # Plugin knowledge backend (ADR 0031) — swap before the graph rebuild.
            new_store = _apply_plugin_knowledge_backend(new_config, new_store, new_plugins)
            _register_plugin_subagents(new_plugins.subagents)
            _apply_config_subagents(new_config)  # YAML subagent overrides take effect on reload
            new_middleware = _resolve_plugin_middleware(new_config, new_plugins.middleware)  # ADR 0032
            new_late_tool_factories = new_plugins.late_tool_factories  # late-tools seam
            new_skills = _build_skills_index(new_config, extra_skill_dirs=new_plugin_skill_dirs)
            new_inbox_store = _build_inbox_store(new_config)
            new_graph = create_agent_graph(
                new_config,
                knowledge_store=new_store,
                scheduler=next_scheduler,
                skills_index=new_skills,
                extra_tools=new_mcp_tools + new_plugin_tools,
                extra_middleware=new_middleware,
                late_tool_factories=new_late_tool_factories,
                checkpointer=STATE.checkpointer,
                inbox_store=new_inbox_store,
                # The background manager (ADR 0050) survives reloads unchanged — its
                # store path + self-invoke URL/auth don't depend on reloadable config.
                background_mgr=STATE.background_mgr,
            )
        except Exception as e:
            log.exception("[reload] graph rebuild failed")
            # Scheduler state hasn't been committed yet — caller's
            # running scheduler keeps polling, no orphaned tasks.
            return False, f"graph rebuild failed: {e}"
    else:
        new_graph = None
        new_inbox_store = None
        # Setup pending → no graph build, so no middleware was resolved. Without
        # this, the commit below raises UnboundLocalError and EVERY pre-setup
        # reload 500s (e.g. installing a plugin during the wizard, whose
        # auto-enable reloads through here).
        new_middleware = []
        new_late_tool_factories = []  # late-tools seam

    # Commit: config → A2A bearer → graph. All three reference the
    # same ``new_config`` so they stay consistent.
    STATE.graph_config = new_config
    STATE.knowledge_store = new_store
    STATE.skills_index = new_skills
    STATE.mcp_clients, STATE.mcp_tools, STATE.mcp_meta = new_mcp_clients, new_mcp_tools, new_mcp_meta
    STATE.plugin_tools, STATE.plugin_skill_dirs, STATE.plugin_meta = (
        new_plugin_tools,
        new_plugin_skill_dirs,
        new_plugin_meta,
    )
    try:
        from security import egress
        from security import policy

        egress.set_allowed_hosts(new_config.egress_allowed_hosts)  # live-reload (ADR 0008)
        policy.set_callback_allowlist(new_config.security_callback_allowlist)  # live-reload (#572)
    except Exception:  # noqa: BLE001 — never block a reload on the egress update
        pass
    try:
        from a2a_impl import auth

        auth.set_bearer_token(new_config.auth_token or None)
    except ImportError:
        # a2a_impl.auth not yet imported (e.g. during early-boot reload before
        # _main wires routes) — harmless.
        pass
    STATE.graph = new_graph
    STATE.plugin_middleware = new_middleware  # ADR 0032
    STATE.plugin_late_tool_factories = new_late_tool_factories  # late-tools seam
    STATE.plugin_chat_commands = new_plugin_chat_commands  # user-only /<name> control commands
    # STATE.workflow_registry / workflow_run were (re)set by the workflows plugin above.
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

    # Hot-mount routes from newly-enabled plugins (e.g. delegates) — already-mounted
    # routers are skipped, so repeat reloads are no-ops. Keep STATE.plugin_routers
    # current for anything introspecting the live route set.
    if is_setup_complete():
        _mount_plugin_routers(new_plugins.routers)
        STATE.plugin_routers = new_plugins.routers

    if new_graph is None:
        log.info("[reload] setup not complete — config reloaded, graph not compiled")
        return True, "config reloaded • setup not complete"

    log.info("LangGraph agent reloaded (model: %s)", STATE.graph_config.model_name)
    return True, f"reloaded • model={STATE.graph_config.model_name}"


async def _plugin_agent_invoke(prompt: str, session_id: str) -> str:
    """Agent invoke exposed to plugin surfaces via the plugin host (ADR 0018) — a
    chat turn joined to its assistant text (mirrors the Discord surface invoker)."""
    result = await chat(prompt, session_id)
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
        from infra.autostart import install_autostart, uninstall_autostart

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


def _filter_nested_to_host_keys(config: dict) -> tuple[dict, list[str]]:
    """Keep only host-scoped (ADR 0047 ``scope=="host"``) leaves of a nested config
    dict; return ``(host_only, dropped)`` where ``dropped`` is the dotted keys that
    were not host-scoped (agent-only / secret) and so are refused on the Host layer.

    Mirrors ``graph.config._filter_to_host_keys`` (the READ-side guard) so the host
    file can't accumulate agent keys, and enforces D5: secret-typed keys are never
    written to the non-secret host file."""
    from graph.config import _get_dotted, _set_dotted
    from graph.settings_schema import host_keys, is_secret_key

    allowed = host_keys()
    host_only: dict = {}
    dropped: list[str] = []

    def _walk(node: Any, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        for k, v in node.items():
            dotted = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                _walk(v, dotted)
                continue
            if dotted in allowed and not is_secret_key(dotted):
                found, val = _get_dotted(config, dotted)
                if found:
                    _set_dotted(host_only, dotted, val)
            else:
                dropped.append(dotted)

    _walk(config, "")
    return host_only, dropped


def _prune_shadowing_agent_keys(host_only: dict) -> list[str]:
    """Delete the just-saved host-scoped keys from the AGENT leaf so the host
    default actually wins.

    A host-console save writes the box-shared host file, but the agent leaf
    (``langgraph-config.yaml``) sits ABOVE the host layer in the ADR 0047
    cascade (agent > host > app). An agent-layer copy of the same key — almost
    always an unmodified seed from ``langgraph-config.example.yaml`` — silently
    shadows the host value the operator just set on the Host console, so the
    edit appears to "reset". On a host save we therefore remove those keys from
    the agent leaf; the effective value then resolves from the host file.

    ``host_only`` is the nested dict of host-scoped leaves written to the host
    file. Returns the dotted paths cleared (for the operator message); empty
    when nothing shadowed."""
    import graph.config_io as _cio
    from graph.config_io import load_yaml_doc, save_yaml_doc

    leaf = _cio.CONFIG_YAML_PATH  # resolved at call time (honors a repoint)
    if not Path(leaf).exists():
        return []  # no agent leaf on disk → nothing can shadow; don't seed one
    doc = load_yaml_doc(leaf)
    removed: list[str] = []

    def _walk(node: Any, keys: Any, prefix: str) -> None:
        if not isinstance(node, dict) or not isinstance(keys, dict):
            return
        for k, v in list(keys.items()):
            if k not in node:
                continue
            dotted = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and isinstance(node.get(k), dict):
                _walk(node[k], v, dotted)
                if not node[k]:  # parent map emptied by the prune → drop it too
                    del node[k]
            else:
                del node[k]
                removed.append(dotted)

    _walk(doc, host_only, "")
    if removed:
        save_yaml_doc(doc, leaf)
    return removed


@_serialized_config_write
def _apply_settings_changes(
    config: dict | None = None,
    soul: str | None = None,
    layer: str = "agent",
) -> tuple[bool, list[str]]:
    """Persist config YAML + SOUL.md then reload the graph once.

    Passing ``None`` for either argument skips that write — a bare
    call with both None acts as a pure reload (useful for picking up
    external file edits).

    ``layer`` selects the cascade file the config write lands in (ADR 0047 slice 3):

    * ``"agent"`` (default) — TODAY's exact behavior: write the agent leaf
      ``langgraph-config.yaml`` (secrets split out to the sibling ``secrets.yaml``).
    * ``"host"`` — write the box-shared host file (``paths.host_config_path()``),
      filtered to host-scoped FIELDS keys only. No secrets land on the host layer
      (D5): a secret-typed key is refused. SOUL writes are agent-local regardless.
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
        if layer == "host":
            try:
                from infra.paths import host_config_path

                host_only, dropped = _filter_nested_to_host_keys(config)
                if dropped:
                    messages.append(f"host layer: ignored non-host key(s) {', '.join(sorted(dropped))}")
                hp = host_config_path()
                doc = load_yaml_doc(hp)
                apply_updates_to_yaml(doc, host_only)
                strip_secrets_from_doc(doc)  # belt-and-suspenders: never a secret on host
                save_yaml_doc(doc, hp)
                messages.append("host config saved")
                # The agent leaf outranks the host file (ADR 0047 agent > host),
                # so a leftover agent-layer copy of the same key would shadow the
                # value just set — clear it so the host default takes effect.
                cleared = _prune_shadowing_agent_keys(host_only)
                if cleared:
                    messages.append(
                        "cleared shadowing agent override(s) so the host value wins: "
                        + ", ".join(sorted(cleared))
                    )
            except Exception as e:
                log.exception("[config] host YAML write failed")
                return False, [f"host config write: {e}"]
        else:
            try:
                import graph.config_io as _cio

                main_config, secret_updates = split_secret_updates(config)
                save_secrets(secret_updates)
                leaf = _cio.CONFIG_YAML_PATH  # resolved at call time (honors a repoint)
                doc = load_yaml_doc(leaf)
                apply_updates_to_yaml(doc, main_config)
                strip_secrets_from_doc(doc)
                save_yaml_doc(doc, leaf)
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
    # to be installed/removed here too. runtime.* is agent-scoped, so this
    # only fires on the agent layer (a host write never carries it).
    if layer != "host":
        as_msg = _sync_autostart_with_config(config)
        if as_msg:
            messages.append(as_msg)

    ok, reload_msg = _reload_langgraph_agent()
    messages.append(reload_msg)
    return ok, messages


@_serialized_config_write
def _reset_settings_keys(keys: list[str]) -> tuple[bool, list[str]]:
    """Reset-to-inherited (ADR 0047 slice 3): pop ``keys`` from the AGENT leaf
    YAML, then reload so each field falls back to the Host/App layer.

    Always operates on the leaf (the layer the settings UI edits per-agent); the
    Host file is left untouched, so resetting an agent override surfaces the host
    default. A pure reload when ``keys`` is empty."""
    import graph.config_io as _cio
    from graph.config_io import load_yaml_doc, pop_keys_from_yaml, save_yaml_doc

    messages: list[str] = []
    if keys:
        try:
            leaf = _cio.CONFIG_YAML_PATH  # resolved at call time (honors a repoint)
            doc = load_yaml_doc(leaf)
            pop_keys_from_yaml(doc, keys)
            save_yaml_doc(doc, leaf)
            messages.append(f"reset {len(keys)} key(s) to inherited")
        except Exception as e:
            log.exception("[config] reset (pop keys) failed")
            return False, [f"reset: {e}"]

    ok, reload_msg = _reload_langgraph_agent()
    messages.append(reload_msg)
    return ok, messages


def _build_settings_callbacks() -> dict[str, Any]:
    """Callbacks consumed by the console Settings (config routes) + the setup wizard."""
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
        # …unless the runtime is ACP (acp:<agent>): the coding agent is the brain and
        # may have no gateway key at all (ADR 0033). Probing a gateway we won't use would
        # wrongly block setup, so skip it — the model block is still persisted for native
        # delegates/fallback if the operator filled it in.
        _runtime = str((config or {}).get("agent_runtime", "native") or "native")
        if not _runtime.startswith("acp:") and config is not None and isinstance(config.get("model"), dict):
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
            from infra.autostart import autostart_status

            name = (STATE.graph_config.identity_name if STATE.graph_config else "") or "protoagent"
            return autostart_status(name)
        except Exception as e:
            return {"supported": False, "installed": False, "reason": str(e)}

    def toggle_autostart(enabled: bool) -> tuple[bool, str]:
        """Install or uninstall the OS autostart artifact, mirroring
        the YAML field. Called from the drawer's checkbox handler so
        toggling takes effect immediately without waiting for Save."""
        try:
            from infra.autostart import install_autostart, uninstall_autostart

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
