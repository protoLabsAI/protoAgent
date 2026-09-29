"""Agent initialization, the component builders, and hot-reload.

Extracted from ``server/__init__.py`` (ADR 0023, phase 2). This module owns the
composition of the LangGraph agent from config: ``_init_langgraph_agent`` and the
``_build_*`` builders (knowledge / skills / MCP / plugins / workflow / scheduler;
the store builders live in ``server/stores.py``), and ``_reload_langgraph_agent`` (the
hot-reload path).

The builders read and mutate the shared ``runtime.state.STATE`` container; the few
``server/__init__`` symbols they need (``agent_name``, ``_bundle_root``) are imported
from ``server`` — all defined before the re-export line in ``__init__`` that triggers
this import, so it is not a cycle. ``server/__init__.py`` re-exports every public name so ``server.<symbol>``
keeps resolving for ``_main``'s wiring and for the test suite.

The background maintenance loops (checkpoint prune, watch, A2A reaper, memory
guard, plugin auto-update, secrets refresh), ``_retire_thread`` and the persona
drift/audit passes live in ``server/maintenance_loops.py`` (#3807) and are
re-exported here. Plugin router mounting + HTTP wrapping, the plugin registries and
host, and plugin-surface reconcile live in ``server/plugin_wiring.py`` (#3821), also
re-exported here. The checkpointer, the per-agent / inbox / background / activity /
telemetry / metrics / ledger store builders and the inbox now-recovery worker live in
``server/stores.py`` (#3829), also re-exported here. The settings apply / reset path
(``_apply_settings_changes`` and its snapshot-rollback helpers), the autostart sync, the
``CONFIG_WRITE_LOCK`` decorator and the console Settings + setup-wizard callbacks live in
``server/settings_apply.py`` (#3848), also re-exported here — ``_apply_settings_changes``
is still PATCHED here: every caller (operator_api, the devkit plugin, maintenance_loops,
plugin_wiring, the plugin host, ``save_all``) resolves it through this module at call time.
"""

import logging
import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from infra.paths import instance_paths
from runtime.state import STATE
from server import agent_name

if TYPE_CHECKING:
    from scheduler.interface import SchedulerBackend

log = logging.getLogger("protoagent.server")

from server.maintenance_loops import (  # noqa: E402,F401 — re-export (#3807); patch collaborators THERE
    _HARVEST_FAILURE_CAP,
    _HARVEST_FAILURES,
    _AUTOUPDATE_IDLE_QUIET_S,
    A2A_REAPER_INTERVAL_S,
    MEMORY_GUARD_INTERVAL_S,
    _a2a_reaper_loop,
    _audit_persona_tools,
    _autoupdate_one_plugin,
    _checkpoint_prune_loop,
    _judge_soul_drift,
    _maybe_run_soul_drift_pass,
    _memory_guard_loop,
    _plugin_autoupdate_loop,
    _plugin_autoupdate_sweep,
    _retire_thread,
    _run_soul_drift_pass,
    _secrets_refresh_loop,
    _server_is_idle,
    _watch_loop,
)
from server.plugin_wiring import (  # noqa: E402,F401 — re-export (#3821); patch collaborators THERE
    _SURFACE_CANCEL_GRACE_S,
    _SURFACE_RECONCILE_LOCKS,
    _SURFACE_RESTART_GRACE_S,
    _apply_plugin_registries,
    _exclude_unschemable_routes,
    _install_error_envelope,
    _mount_plugin_routers,
    _plan_surface_reconcile,
    _plugin_agent_invoke,
    _plugin_api_routes,
    _populate_plugin_host,
    _reload_plugin_surfaces,
    _surface_key,
    _surface_reconcile_lock,
    _wrap_plugin_endpoint,
)
from server.stores import (  # noqa: E402,F401 — re-export (#3829); patch collaborators THERE
    _AGENT_DB,
    _INBOX_NOW_RECOVERY_BATCH_LIMIT,
    _INBOX_NOW_RECOVERY_MAX_ATTEMPTS,
    _INBOX_NOW_RECOVERY_RETRY_AFTER_S,
    _agent_store_db,
    _build_activity_log,
    _build_background_manager,
    _build_checkpointer,
    _build_inbox_store,
    _build_ledger_store,
    _build_metrics_store,
    _build_telemetry_store,
    _on_work_terminal,
    _resolve_checkpoint_db,
    _start_inbox_now_recovery_once,
    recover_pending_now_inbox_items,
)
from server.settings_apply import (  # noqa: E402,F401 — re-export (#3848); patch collaborators THERE
    _CONFIG_WRITE_LOCK,
    _ROLLBACK_NOTE,
    _WRITE_ANNOUNCEMENTS,
    _apply_settings_changes,
    _build_settings_callbacks,
    _config_files_to_snapshot,
    _drop_undone_write_messages,
    _filter_nested_to_host_keys,
    _prune_shadowing_agent_keys,
    _reset_settings_keys,
    _restore_config_files,
    _serialized_config_write,
    _snapshot_config_files,
    _sync_autostart_with_config,
)



@contextmanager
def _timed_boot_phase(phase: str, sink: dict[str, float] | None = None):
    """Time one agent-construction phase (#2674) — same ``time.monotonic()`` idiom
    as ``AuditMiddleware``'s tool-call timing (``graph/middleware/audit.py``),
    applied to boot instead of a turn. Emits to the ``*_boot_phase_seconds{phase}``
    histogram unconditionally (no-ops when metrics are disabled); also records
    into ``sink`` when given, so a caller can log one structured summary line
    covering every phase instead of one log line per phase."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        duration_s = time.monotonic() - t0
        from observability import metrics

        metrics.record_boot_phase(phase, duration_s)
        if sink is not None:
            sink[phase] = duration_s


def _log_boot_phase_summary(phases: dict[str, float]) -> None:
    """One structured summary line covering every phase timed so far (#2674), so
    "session felt slow to start" is diagnosable from a boot log without scraping
    /metrics. Called from every ``_init_langgraph_agent`` exit point — including
    the setup-pending early return, which only reaches the checkpointer phase —
    so first-run boot logs aren't silently missing whatever phases DID run."""
    if phases:
        log.info(
            "[boot] phase timings: %s (total %.2fs)",
            ", ".join(f"{phase}={duration:.2f}s" for phase, duration in phases.items()),
            sum(phases.values()),
        )


def apply_egress_allowlist(config) -> None:
    """Point the egress guard at ``config``'s allowlist, the model gateway always included.

    One seam for boot and live-reload (ADR 0008). The deny-by-default allowlist must never
    block the operator's own gateway, and the endpoint comes from the resolver every other
    reader uses (#3128). It exists to be testable: while both call sites inlined this, a site
    that stopped auto-allowing the gateway left the whole suite green.
    """
    from graph.config import resolve_model_route
    from security import egress

    egress.set_allowed_hosts(config.egress_allowed_hosts, also_allow_url=resolve_model_route(config).base_url or "")


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
        apply_seed_merge,
        config_yaml_path,
        ensure_live_config,
        ensure_live_soul,
        is_setup_complete,
        mark_setup_complete,
        validate_for_headless,
    )

    # Boot-phase timing (#2674) — populated as the phases below run, logged as one
    # structured summary line once the graph is ready (or we know it won't be).
    _boot_phases: dict[str, float] = {}

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

    # Make a provisioned Node runtime (`protoagent runtime install-node`) visible to
    # every subprocess we spawn — the npx-based ACP coding agents + MCP servers — when
    # the user has no Node of their own. No-op if Node is already on PATH or none is
    # provisioned. One seam covers ACP, MCP, delegates, and gh (ADR 0085).
    from infra.node_runtime import augment_path_with_managed_node

    _node_dir = augment_path_with_managed_node()
    if _node_dir:
        log.info("[node] managed Node runtime on PATH: %s", _node_dir)

    # Seed the untracked live config from the .example template on first run.
    # config_yaml_path() resolves to <instance_root>/config/langgraph-config.yaml
    # (env-driven via PROTOAGENT_HOME / PROTOAGENT_INSTANCE), so load through it.
    ensure_live_config()
    # Re-apply the baked seed's image-owned declarative keys (#2071). Opt-in via
    # PROTOAGENT_SEED_MERGE; a no-op otherwise, so seed-once stays the default.
    apply_seed_merge()
    # Seed the live SOUL.md the same way (seed-not-force) — and heal a lingering
    # starter placeholder so a persona baked into the bundle actually takes effect
    # instead of being shadowed forever by "Replace this file". No-op once authored.
    ensure_live_soul()
    STATE.graph_config = LangGraphConfig.from_yaml(config_yaml_path())
    # Fork tool denylist (config ``tools.disabled``) — applied before any
    # get_all_tools() call so dropped tools never reach the graph.
    from tools.lg_tools import set_disabled_tools

    # Hidden tools (#2172) are a HARD superset of disabled — deny them here too, so a
    # hidden tool is never bound to the graph regardless of the disabled list. The console
    # inventory then also drops them (console_handlers), so they never render or toggle.
    _denied = list(dict.fromkeys([*STATE.graph_config.tools_disabled, *STATE.graph_config.tools_hidden]))
    set_disabled_tools(_denied)
    # Egress allowlist (ADR 0008): deny-by-default outbound hosts for fetch_url. The model
    # gateway's host is always allowed: the default route's endpoint.
    apply_egress_allowlist(STATE.graph_config)
    # Opt-in CIDR allowlist for outbound A2A destinations — callbacks + delegate_to a2a delegates (#572).
    from security import policy

    policy.set_callback_allowlist(STATE.graph_config.security_callback_allowlist)
    # Instance identity is env-only (ADR 0004 / InstancePaths): PROTOAGENT_HOME /
    # PROTOAGENT_INSTANCE are resolved at boot by infra.paths — never seeded from
    # config-file content — so a correctly-scoped config is read on the first try.
    # Conversation checkpointer: durable SQLite when a path is configured (chat
    # history survives restarts), else in-memory. Bound into the graph at
    # compile time below — a checkpointer in the invoke config is ignored.
    with _timed_boot_phase("checkpointer", _boot_phases):
        STATE.checkpointer = _build_checkpointer(STATE.graph_config)

    # Plugin metric timeseries (#1632) — sdk.record_metric / metric_history / metric_last.
    # Wired BEFORE any plugin load (including the pre-setup routes/surfaces load below)
    # so a plugin's register() can already record. Not config-dependent, so it survives
    # hot-reloads untouched (like tasks_store / background_mgr) — and it is never
    # threaded into the graph build, so the #1630 reload-drop class doesn't apply.
    STATE.metrics_store = _build_metrics_store()
    # Delegation ledger — the durable record of who handed what work to whom
    # (graph/ledger.py is the single writer). Wired here, beside the metric store and for
    # the same reasons: not config-dependent, needed before any plugin's register() runs,
    # and never threaded into the graph build, so a hot-reload leaves it untouched.
    STATE.ledger_store = _build_ledger_store()

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
            STATE.plugin_federation_paths = _pre.federation_paths
            _register_plugin_subagents(_pre.subagents)
            log.info(
                "Setup wizard has not been completed — graph not compiled "
                "(plugin routes/surfaces still mounted). "
                "Open the UI to finish setup (or run headless: --ui none / --setup).",
            )
            _log_boot_phase_summary(_boot_phases)
            return

    from graph.agent import create_agent_graph
    from graph.providers.oauth import OAuthCredentialError
    from tools.lg_tools import get_all_tools

    # Construct the default KnowledgeStore so memory tools (memory_ingest,
    # memory_recall, memory_list, memory_stats) and KnowledgeMiddleware have something to
    # bind to. Forks that don't want a store can set
    # ``middleware.knowledge: false`` and remove the memory tools from
    # the worker subagent — the store is still cheap to construct.
    with _timed_boot_phase("knowledge_store", _boot_phases):
        STATE.knowledge_store = _build_knowledge_store(STATE.graph_config)

    # Scheduler — the bundled local sqlite backend (or None when disabled).
    # Agent-tool surface: schedule_task / list_schedules / cancel_schedule.
    STATE.scheduler = _build_scheduler(STATE.graph_config)

    # Plugins — drop-in packages (tools + bundled skills + surfaces/routes +
    # managed MCP servers). Loaded BEFORE MCP so a plugin's managed MCP server
    # (register_mcp_server, e.g. Google) is injected into the MCP discovery
    # below. Collision check uses core tools only — MCP tools are namespaced
    # (<server>__<tool>) so they can't be shadowed by a plugin tool anyway.
    with _timed_boot_phase("plugins", _boot_phases):
        _plugins = _build_plugins(
            STATE.graph_config,
            existing_tools=get_all_tools(
                STATE.knowledge_store,
                scheduler=STATE.scheduler,
                goal_enabled=getattr(STATE.graph_config, "goal_enabled", True),
                watches_enabled=getattr(STATE.graph_config, "watches_enabled", False),
            ),
        )
    STATE.plugin_tools, STATE.plugin_skill_dirs, STATE.plugin_meta = (
        _plugins.tools,
        _plugins.skill_dirs,
        _plugins.meta,
    )
    STATE.plugin_tool_owner = _plugins.tool_plugins
    STATE.plugin_workflow_dirs = _plugins.workflow_dirs
    STATE.plugin_a2a_skills = _plugins.a2a_skills  # A2A card skills (#570)
    STATE.plugin_chat_commands = _plugins.chat_commands  # user-only /<name> control commands
    STATE.thread_id_resolver = _plugins.thread_id_resolver  # thread_id seam (#571)
    # A plugin may provide the knowledge backend (ADR 0031) — swap it in now (the
    # graph compiles below with STATE.knowledge_store). Default built-in store stays
    # the collision-check binding + the degrade-safe fallback.
    STATE.knowledge_store = _apply_plugin_knowledge_backend(STATE.graph_config, STATE.knowledge_store, _plugins)
    # Register plugin-contributed goal verifiers + goal/watch hooks (ADR 0028/0067) into
    # their live module registries — re-applied on every (re)load via _apply_plugin_registries
    # so a config/plugin change refreshes the available `plugin` verifiers and hooks.
    _apply_plugin_registries(_plugins)
    # Surfaces / routes / subagents (ADR 0018). Both are captured here and consumed
    # by _main (mount) + the startup hook (start). Routers DO hot-reload now — the
    # reload commit re-mounts newly-enabled plugins' routers (#1752/#1890) — but
    # SURFACES do not: the startup hook has already fired, so a surface only (re)starts
    # on a full restart (the reload path just fires each running surface's `reload`
    # callback). Subagents register into SUBAGENT_REGISTRY before the graph build below
    # so the first compile (and every reload) can delegate to them.
    # (`global STATE.plugin_routers, STATE.plugin_surfaces` is declared at the top of the fn.)
    STATE.plugin_routers, STATE.plugin_surfaces = _plugins.routers, _plugins.surfaces
    STATE.plugin_public_paths = _plugins.public_paths
    STATE.plugin_federation_paths = _plugins.federation_paths
    _register_plugin_subagents(_plugins.subagents)
    _apply_config_subagents(STATE.graph_config)  # YAML subagent overrides (tools/max_turns/model)
    STATE.plugin_middleware = _resolve_plugin_middleware(STATE.graph_config, _plugins.middleware)  # ADR 0032
    STATE.plugin_late_tool_factories = _plugins.late_tool_factories  # late-tools seam

    # MCP — external Model Context Protocol servers; their tools become agent
    # tools (namespaced <server>__<tool>). Off unless mcp.enabled OR a plugin
    # contributes a managed server (ADR 0019).
    with _timed_boot_phase("mcp", _boot_phases):
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
        # Bind the emit seam (#2262): in-graph code (middleware warnings the
        # operator should SEE) appends through activity.emit(), which is a no-op
        # until this line runs — graph can't import server, so the binding
        # happens here, where the per-instance feed is built.
        from activity import set_default_feed

        set_default_feed(STATE.activity_log)
    from tasks import TaskStore

    if STATE.tasks_store is None:  # may have been created early (pre-setup) for the routes
        STATE.tasks_store = TaskStore()  # in-process issue tracker (Sprint B), instance-scoped
    if STATE.storm_guard is None:
        from inbox import StormGuard

        STATE.storm_guard = StormGuard()
    # Background subagent manager (ADR 0050) — must exist before the graph build so
    # the `task` tool's run_in_background path can reach it.
    STATE.background_mgr = _build_background_manager(STATE.graph_config)

    try:
        with _timed_boot_phase("graph_compile", _boot_phases):
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
                # Lets the guarded edit_soul tool (ADR 0079/0081) reload the graph so a
                # persona self-edit is live on the next turn — injected, so tools/ never
                # imports server/. The prompt-only variant: a persona edit must not
                # re-import plugins or respawn MCP servers (#3365).
                reload_callback=_reload_for_soul_edit,
            )
    except OAuthCredentialError as exc:
        # Signed-out is an intentional state, not a boot failure (#2458): the user
        # disconnected a native OAuth provider and the marker survived a restart.
        # Crashing here is a recovery dead end — the reconnect routes live on THIS
        # server. Boot graphless instead (routes/surfaces above are already wired,
        # chat degrades on ``STATE.graph is None``) and record why, so status APIs
        # can offer reconnect instead of a dead port. The graph-independent
        # machinery below (goal/watch controllers) still builds: the reconnect
        # reload rebuilds only the graph, so anything skipped here would stay
        # dead until a full restart.
        STATE.graph = None
        STATE.graph_auth_error = {
            "provider": exc.provider,
            "message": str(exc),
            "relogin": exc.relogin,
        }
        log.warning(
            "[oauth] %s — starting without a compiled graph; reconnect %s from the console to restore chat.",
            exc,
            exc.provider,
        )
    else:
        STATE.graph_auth_error = None
        # Untooled-action audit (#2276) — now that the persona AND the bound tool set
        # both exist, warn about commitments no tool backs (the model narrates those
        # as done).
        _audit_persona_tools(STATE.graph, trigger="boot")

    # Fires even on a graphless boot (OAuthCredentialError) — the phases that DID
    # run (checkpointer/knowledge_store/plugins/mcp) are still useful signal.
    _log_boot_phase_summary(_boot_phases)

    # Cache-warming heartbeat — off by default; start() no-ops unless enabled
    # for an Anthropic-family model (see graph/cache_warmer.py). Not built while
    # signed out (#2458): its pings are provider requests, exactly what a
    # disconnected instance must not send.
    if STATE.graph is not None:
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

        STATE.goal_controller = GoalController(STATE.graph_config, GoalStore(), scheduler=STATE.scheduler)
    else:
        STATE.goal_controller = None
    # Watch primitive (ADR 0067) — many concurrent, out-of-band watches. Machinery only; no
    # watch runs until one is created, so it's always on (cheap when idle).
    from graph.watches import WatchController, WatchStore

    STATE.watch_controller = WatchController(STATE.graph_config, WatchStore())
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

                store = HybridKnowledgeStore(
                    db_path=db_path,
                    scoped=scoped,
                    embed_fn=embed_fn,
                    embed_batch_fn=embed_batch_fn,
                    vector_k=config.knowledge_vector_k,
                    rrf_k=config.knowledge_rrf_k,
                    min_score=config.knowledge_min_score,
                    breaker_threshold=config.knowledge_embed_breaker_threshold,
                    breaker_cooldown_s=config.knowledge_embed_breaker_cooldown_s,
                    preview_chars=config.knowledge_recall_preview_chars,
                    chunk_max_chars=config.knowledge_chunk_max_chars,
                    chunk_overlap_chars=config.knowledge_chunk_overlap_chars,
                    chunk_min_chars=config.knowledge_chunk_min_chars,
                    context_fn=context_fn,
                )
                # Off-thread route probe (#1681): a dead embedding route opens the
                # breaker BEFORE the first chat turn instead of freezing it.
                store.warm_probe()
                return store
            return KnowledgeStore(
                db_path=db_path,
                scoped=scoped,
                preview_chars=config.knowledge_recall_preview_chars,
                chunk_max_chars=config.knowledge_chunk_max_chars,
                chunk_overlap_chars=config.knowledge_chunk_overlap_chars,
                chunk_min_chars=config.knowledge_chunk_min_chars,
                context_fn=context_fn,
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
                    commons_path,
                    stamped,
                    want,
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

        from infra.paths import instance_paths

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

        _ip = instance_paths()
        live_root = Path(config.skills_dir).expanduser() if config.skills_dir else (_ip.config_dir / "skills")
        roots = [_ip.bundle_dir / "skills", live_root]  # bundle first, live overrides
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

    ``clients`` may include a persistent-session pool holding live subprocesses
    — whenever a build's clients are discarded (reload swap, failed rebuild),
    release them via ``_close_mcp_clients``.
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


def _close_mcp_clients(clients) -> None:
    """Release MCP connection handles (persistent session pools). Never raises."""
    try:
        from tools.mcp_tools import close_mcp_clients

        close_mcp_clients(clients)
    except Exception:  # noqa: BLE001 — teardown must never break a reload
        log.warning("[mcp] client teardown failed", exc_info=True)


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
        from graph.plugins.host import HOST

        # The LAZY host fields must be wired BEFORE register() runs: they're
        # deferred reads (a lambda over STATE / the settings-apply seam), so
        # assigning them early is safe — and a plugin that captures
        # `registry.host.config` at register time otherwise silently gets None
        # on a COLD boot while working on a hot-enable (the promptlab incident:
        # broke on the first desktop-app restart after install). The eager
        # fields (invoke / event bus) genuinely need the built server and stay
        # in _populate_plugin_host.
        if HOST.config is None:
            HOST.config = lambda: STATE.graph_config
        if HOST.apply_settings is None:
            HOST.apply_settings = lambda patch: _apply_settings_changes(config=patch)

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


# The checkpointer, the per-agent stores (inbox / background / activity), the inbox
# now-recovery worker, and the telemetry / metrics / ledger stores moved to
# server/stores.py (#3829); re-exported above for existing callers. Monkeypatch
# collaborators on THAT module.


# Background maintenance loops, _retire_thread, persona drift/audit, plugin
# auto-update and secrets refresh moved to server/maintenance_loops.py (#3807);
# re-exported above for existing callers. Monkeypatch collaborators on THAT module.


def _commons_dir(config):
    """The shared commons base (ADR 0041) — read by every agent on the host, never
    per-instance scoped. Configurable via ``commons.path``; defaults to
    ``~/.protoagent/commons``."""
    from pathlib import Path

    raw = (getattr(config, "commons_path", "") or "").strip()
    return Path(raw).expanduser() if raw else (Path.home() / ".protoagent" / "commons")


def _resolve_skills_db(configured: str, *, shared: bool = False, commons=None) -> str:
    """Pick the skills DB path.

    When ``shared`` (ADR 0041, tiered stores), the skills library is the COMMONS:
    box-level + un-scoped so every agent on the host shares one DB. Otherwise it's
    the per-instance ``instance_root/skills.db`` (``configured`` is no longer a
    location knob — the instance root IS the scope)."""
    from pathlib import Path

    if shared:
        path = Path(commons or instance_paths().commons_dir) / "skills.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    db = instance_paths().store("skills.db")
    db.parent.mkdir(parents=True, exist_ok=True)
    return str(db)


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
        # Ask the guard what it enforces instead of re-deriving it here (#2620); this
        # also drops the old "env-derived name, NOT identity.name" footgun, since the
        # resolution now happens in exactly one place.
        from a2a_impl.auth import inbound_credentials

        bearer, api_key = inbound_credentials()
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


# Plugin router mounting + HTTP wrapping and plugin registries moved to
# server/plugin_wiring.py (#3821); re-exported above. Patch collaborators on THAT module.


def _reload_for_soul_edit() -> tuple[bool, str]:
    """The reload ``edit_soul`` gets: re-render the prompt, keep the plugins (#3365).

    A persona edit rewrites SOUL.md — the system prompt — and changes nothing about
    the plugin set or the MCP roster. Rebuilding those anyway cost a full re-import
    of every plugin (which leaked a whole generation per rebuild until the loader
    learned to skip unchanged sources) plus a teardown-and-respawn of every MCP
    server subprocess — on the agent's own turn, every time it edited its persona.

    Kept as a named function rather than a lambda so the graph can re-thread it
    into the next generation of ``edit_soul`` by name. Deliberately NOT decorated
    with ``@_serialized_config_write``: it takes that lock through the call below,
    which is where the write actually happens.
    """
    return _reload_langgraph_agent(reload_plugins=False)


@_serialized_config_write
def _reload_langgraph_agent(*, reload_plugins: bool = True) -> tuple[bool, str]:
    """Rebuild the compiled graph from the latest config YAML.

    ``reload_plugins=False`` reuses the live plugin bundle and MCP clients instead
    of rebuilding them (#3365) — for a caller that only changed the prompt. Plugin
    middleware and late-tool factories are still re-resolved from the cached
    bundle, so the rebuilt graph gets fresh instances either way.

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
    from graph.config_io import config_yaml_path, ensure_live_config, is_setup_complete
    from tools.lg_tools import get_all_tools

    ensure_live_config()
    try:
        new_config = LangGraphConfig.from_yaml(config_yaml_path())
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
    new_plugin_tool_owner: dict = {}  # tool name -> owning plugin display name (Tools tab)
    new_plugin_chat_commands: dict = {}  # user-only /<name> control commands
    # Pre-seeded like its siblings because the commit below publishes it UNCONDITIONALLY (the
    # reconcile has to see a disabled plugin's surface leave the wanted set). Reading it off
    # `new_plugins` there raised UnboundLocalError on the setup-pending branch, which never
    # builds plugins — the same trap the `new_middleware = []` below already guards against.
    new_plugin_surfaces: list = []
    # Pre-seeded for the same reason as its siblings: the setup-pending branch never
    # builds plugins, and the commit below publishes unconditionally.
    new_plugin_bundle = None
    if is_setup_complete():
        try:
            new_store = _build_knowledge_store(new_config)
            # The workflows plugin re-sets these in its register() if still enabled;
            # reset first so disabling it on reload leaves them cleared.
            STATE.workflow_registry = STATE.workflow_run = None
            # Plugins before MCP — a plugin's managed MCP server (e.g. Google)
            # is injected into the MCP discovery below (matches _main ordering).
            # Prompt-only rebuild (#3365): reuse the live bundle + MCP clients. Only
            # when we actually have one — a reuse request before the first successful
            # build (or after a failed one) falls through to a full build rather than
            # committing an empty plugin surface.
            reused_bundle = None if reload_plugins else STATE.plugin_bundle
            if reused_bundle is not None:
                new_plugins = reused_bundle
                # These clients are LIVE and stay live: the commit below skips the
                # close because the list is identical, and the failure path below
                # must not close them either.
                new_mcp_clients, new_mcp_tools, new_mcp_meta = (
                    STATE.mcp_clients,
                    STATE.mcp_tools,
                    STATE.mcp_meta,
                )
            else:
                new_plugins = _build_plugins(
                    new_config,
                    existing_tools=get_all_tools(
                        new_store,
                        scheduler=next_scheduler,
                        goal_enabled=getattr(new_config, "goal_enabled", True),
                        watches_enabled=getattr(new_config, "watches_enabled", False),
                    ),
                )
                new_mcp_clients, new_mcp_tools, new_mcp_meta = _build_mcp(
                    new_config, plugin_servers=[s["factory"] for s in new_plugins.mcp_servers]
                )
            new_plugin_bundle = new_plugins
            new_plugin_tools = new_plugins.tools
            new_plugin_tool_owner = new_plugins.tool_plugins
            new_plugin_skill_dirs = new_plugins.skill_dirs
            new_plugin_meta = new_plugins.meta
            new_plugin_chat_commands = new_plugins.chat_commands  # user-only /<name> control commands
            new_plugin_surfaces = new_plugins.surfaces
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
                # The tasks store survives reloads like the checkpointer (its path isn't
                # reloadable config). It was missing here, so ANY settings hot-reload
                # silently dropped task_create/task_list/task_update/task_close from the
                # rebuilt graph until the next full restart.
                tasks_store=STATE.tasks_store,
                # The background manager (ADR 0050) survives reloads unchanged — its
                # store path + self-invoke URL/auth don't depend on reloadable config.
                background_mgr=STATE.background_mgr,
                # Re-thread the reload hook so the rebuilt graph's edit_soul can reload again
                # (self-referential: this IS the reload path). Missing it would make the
                # persona editor a one-shot after the first hot-reload. Threads the
                # prompt-only variant, so the scope reduction survives a reload too (#3365).
                reload_callback=_reload_for_soul_edit,
            )
        except Exception as e:
            log.exception("[reload] graph rebuild failed")
            # The freshly-built MCP clients were never committed — close them or
            # their persistent sessions (subprocesses) leak on every failed reload.
            # A REUSED set is the live one still serving the current graph, though:
            # closing it here would kill working MCP servers because an unrelated
            # prompt rebuild failed (#3365).
            if new_mcp_clients is not STATE.mcp_clients:
                _close_mcp_clients(new_mcp_clients)
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
    # A fleet member's own ``workspace.yaml`` is what the HUB's fleet list — and so the
    # console's agent switcher / header label — displays. Settings ▸ Agent ▸ Identity is
    # agent-scoped, so on a member console it writes the MEMBER's identity.name and never
    # touched that record: the tab title and A2A card renamed, the switcher didn't. Restamp
    # it here, at the single choke point where the live identity changes, so every path
    # (settings save, /api/config, an out-of-band YAML edit + reload) converges. No-op on a
    # host/standalone instance and when it's already in step, so this is also a cheap
    # boot-time reconcile after a hub-side rename.
    # A label the record can't hold verbatim is normalized, not refused, so the note below is
    # usually about a name that got slugified — not a failure.
    fleet_label_note = ""
    try:
        from graph.workspaces import manager as workspaces_manager

        note = workspaces_manager.sync_self_display_name(new_config.identity_name)
        if note:
            log.info("[fleet] %s", note)
            fleet_label_note = f" • {note}"
    except Exception:  # noqa: BLE001 — a fleet label must never fail a reload
        log.exception("[fleet] workspace display-name sync failed")
    # HOST-side mirror (#2528), same choke-point rationale as the fleet-label sync
    # above: the host's model group IS what members inherit, so keep the Host layer
    # (host-config.yaml) in step on every successful (re)build — boot included. A
    # member's reload no-ops here (it must never write box state).
    try:
        from graph.config_io import sync_host_model_layer

        sync_host_model_layer(new_config)
    except Exception:  # noqa: BLE001 — the mirror must never fail a reload
        log.exception("[config] host model-layer sync failed")
    STATE.knowledge_store = new_store
    STATE.skills_index = new_skills
    # Swap in the new MCP clients, then release the old ones — persistent session
    # pools hold live subprocesses that would otherwise leak on every reload. A
    # turn still mid-flight on the OLD graph sees its next MCP call degrade to a
    # recoverable tool-error string (never a hang) — same shape as a server crash.
    old_mcp_clients = STATE.mcp_clients
    STATE.mcp_clients, STATE.mcp_tools, STATE.mcp_meta = new_mcp_clients, new_mcp_tools, new_mcp_meta
    if old_mcp_clients and old_mcp_clients is not new_mcp_clients:
        _close_mcp_clients(old_mcp_clients)
    STATE.plugin_tools, STATE.plugin_skill_dirs, STATE.plugin_meta = (
        new_plugin_tools,
        new_plugin_skill_dirs,
        new_plugin_meta,
    )
    STATE.plugin_tool_owner = new_plugin_tool_owner
    STATE.plugin_bundle = new_plugin_bundle  # what a prompt-only rebuild reuses (#3365)
    try:
        from security import policy

        apply_egress_allowlist(new_config)  # live-reload (ADR 0008)
        policy.set_callback_allowlist(new_config.security_callback_allowlist)  # live-reload (#572)
    except Exception:  # noqa: BLE001 — never block a reload on the egress update
        pass
    try:
        from a2a_impl import auth

        # None (field absent from config) falls back to env — a deployment configured
        # purely via A2A_AUTH_TOKEN/A2A_FEDERATION_TOKEN must not have its credential
        # silently cleared by the first Settings save of anything (#1504 review).
        # "" (explicitly set to empty) means bearer off: do NOT fall back to env,
        # otherwise auth: {token: ""} silently re-enables auth via the env var (#2691).
        auth.set_bearer_token(
            new_config.auth_token if new_config.auth_token is not None
            else (os.environ.get("A2A_AUTH_TOKEN") or None)
        )
        auth.set_federation_token(
            new_config.federation_token if new_config.federation_token is not None
            else (os.environ.get("A2A_FEDERATION_TOKEN") or None)
        )
    except ImportError:
        # a2a_impl.auth not yet imported (e.g. during early-boot reload before
        # _main wires routes) — harmless.
        pass
    STATE.graph = new_graph
    if new_graph is not None:
        # A committed graph proves the credential resolved — clear the signed-out
        # marker state (#2458) so status APIs stop offering reconnect.
        STATE.graph_auth_error = None
    # Untooled-action audit (#2276) — a reload is exactly when the persona/tool set
    # changes (SOUL edit, plugin enable/disable, tools.disabled), so re-check here.
    _audit_persona_tools(new_graph, trigger="reload")
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
    if STATE.goal_controller is not None:
        STATE.goal_controller.reconfigure(new_config, scheduler=next_scheduler)
    if pending_stop is not None:
        _stop_scheduler_async(pending_stop)
    if pending_start is not None:
        _start_scheduler_async(pending_start)

    # Publish the reloaded surface spec set so the reconcile below (and any
    # introspection) sees the CURRENT wanted surfaces — this is what lets a reload
    # hot-start a newly-enabled plugin's surface and stop a disabled one. Was
    # boot-only before, so surface enable/disable needed a restart.
    STATE.plugin_surfaces = new_plugin_surfaces
    # Reconcile running surfaces against that set (ADR 0018/0019): stop surfaces whose
    # plugin was disabled/removed, hot-start newly-enabled ones, and fire each survivor's
    # reload hook so a Discord/Google-style gateway live-reconnects on a token/admin change.
    _reload_plugin_surfaces(new_config)

    # Hot-mount routes from newly-enabled plugins (e.g. delegates) — already-mounted
    # routers are skipped, so repeat reloads are no-ops. Keep STATE.plugin_routers
    # current for anything introspecting the live route set.
    if is_setup_complete():
        _mount_plugin_routers(new_plugins.routers)
        STATE.plugin_routers = new_plugins.routers
        # Refresh the live plugin verifier/hook registries too (#1752) — same as full init.
        # Without this a plugin update/enable that ships a new verifier leaves the watch/goal
        # controllers resolving it as "unknown" until a full restart.
        _apply_plugin_registries(new_plugins)
        # Re-push the auth-exempt public prefixes too (#1890 — same rule as #1752: any
        # init-time registry wiring must re-apply on reload). Without this a hot-enabled
        # plugin's view page stays 401 under a token gate until a full restart.
        STATE.plugin_public_paths = new_plugins.public_paths
        from a2a_impl import auth as _a2a_auth

        _a2a_auth.set_public_prefixes(new_plugins.public_paths)
        # Federation-tier prefixes ride the same re-push (#2747): replacing the set is
        # what makes a disabled plugin's route fall back to operator-only at once.
        STATE.plugin_federation_paths = new_plugins.federation_paths
        _a2a_auth.set_federation_prefixes(new_plugins.federation_paths)
        # Re-publish the remaining boot-time plugin wiring that consumers read fresh from
        # STATE (same #1752/#1890 rule). Each was assigned only at init, so a hot
        # enable/update left it stale until a full restart: the thread_id resolver
        # (server/chat.py memory scoping), the A2A card skills (server/a2a.py card build),
        # and the workflow recipe dirs (the workflows plugin's lazy _reg() rescans on a
        # dir-set change — its own docstring promises "hot install, config reload", which
        # only holds once this list actually updates).
        STATE.thread_id_resolver = new_plugins.thread_id_resolver
        STATE.plugin_a2a_skills = new_plugins.a2a_skills
        STATE.plugin_workflow_dirs = new_plugins.workflow_dirs
        # Swapping STATE alone refreshes only the structured finalizer's view — the
        # SERVED card (route + SDK handler) was built once at boot and closed over.
        # Rebuild it so the card can't advertise a skill set the runtime no longer
        # has, or hide one it just gained (#2754). No-op before first card build.
        from server.a2a import refresh_served_card

        refresh_served_card()

    if new_graph is None:
        log.info("[reload] setup not complete — config reloaded, graph not compiled")
        return True, f"config reloaded • setup not complete{fleet_label_note}"

    log.info("LangGraph agent reloaded (model: %s)", STATE.graph_config.model_name)
    return True, f"reloaded • model={STATE.graph_config.model_name}{fleet_label_note}"


# The plugin host wiring and plugin-surface reconcile moved to server/plugin_wiring.py
# (#3821); re-exported above.


# The settings apply / reset / snapshot-rollback path, the autostart sync and the
# console Settings + setup-wizard callbacks moved to server/settings_apply.py (#3848);
# re-exported above.
