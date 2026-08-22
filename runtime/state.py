"""AppState — the process runtime container (ADR 0023).

Replaces server.py's 26 ambient module-global singletons with one named,
injectable object. Same objects, same lifecycle — `server.py` and the extracted
modules read `STATE.knowledge_store` instead of a bare `_knowledge_store`, and
init/reload set `STATE.x` instead of `global _x`. A single process-wide
singleton (`STATE`); `get_state()` is the FastAPI dependency form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppState:
    # Compiled graph + its config.
    graph: Any = None
    graph_config: Any = None
    # Why the graph is absent when a native-OAuth credential is the reason (#2458):
    # ``{"provider", "message", "relogin"}`` after boot/reload hit OAuthCredentialError
    # (signed-out is an intentional state — the server stays up, graphless, so the
    # reconnect routes remain reachable). None whenever a graph is live.
    graph_auth_error: Any = None
    # Conversation checkpointer + prune bookkeeping.
    checkpointer: Any = None
    checkpoint_path: Any = None
    checkpoint_prune_task: Any = None
    monitor_goals_task: Any = None  # ADR 0030 monitor-goal cadence loop
    watch_task: Any = None  # ADR 0067 watch cadence loop
    plugin_autoupdate_task: Any = None  # #1720 opt-in plugin auto-update loop
    secrets_refresh_task: Any = None  # ADR 0080 external secrets-manager refresh loop
    # Stores / registries bound into the active graph.
    knowledge_store: Any = None
    skills_index: Any = None
    workflow_registry: Any = None  # set by the workflows plugin (None = plugin disabled)
    workflow_run: Any = None  # async (name, inputs, on_step) -> result; set by the workflows plugin
    telemetry_store: Any = None
    # Plugin metric timeseries (#1632) — behind sdk.record_metric / metric_history /
    # metric_last. Wired once at boot (path isn't reloadable config); survives reloads.
    metrics_store: Any = None
    # Async engine behind the durable A2A task store — held so the periodic
    # prune loop can re-run the 24h TTL sweep (it used to run only at boot, so
    # an always-on agent accumulated task rows forever between restarts).
    a2a_task_engine: Any = None
    a2a_push_engine: Any = None  # push-config store engine — for the orphan sweep (ADR 0051)
    inbox_store: Any = None
    tasks_store: Any = None
    storm_guard: Any = None
    activity_log: Any = None
    # MCP servers (ADR 0001) + plugin contributions (ADR 0018/0019).
    mcp_clients: list = field(default_factory=list)
    mcp_tools: list = field(default_factory=list)
    mcp_meta: list = field(default_factory=list)
    plugin_tools: list = field(default_factory=list)
    # tool name -> owning plugin display name (Tools tab grouping); mirrors plugin_tools.
    plugin_tool_owner: dict = field(default_factory=dict)
    plugin_skill_dirs: list = field(default_factory=list)
    plugin_middleware: list = field(default_factory=list)  # resolved AgentMiddleware instances (ADR 0032)
    plugin_late_tool_factories: list = field(default_factory=list)  # (all_tools, config) -> tool|list (late seam)
    plugin_workflow_dirs: list = field(default_factory=list)  # *.yaml recipe dirs (ADR 0027)
    plugin_a2a_skills: list = field(default_factory=list)  # A2A card skills from plugins (#570)
    plugin_chat_commands: dict = field(default_factory=dict)  # token -> handler; user-only /<name> control commands
    thread_id_resolver: object = None  # (request_metadata, session_id) -> str (#571)
    plugin_routers: list = field(default_factory=list)
    plugin_public_paths: list = field(default_factory=list)  # manifest auth-exempt prefixes
    plugin_federation_paths: list = field(default_factory=list)  # manifest federation-tier prefixes (#2747)
    # The live FastAPI app + the (plugin_id, prefix) keys already mounted on it —
    # lets a config reload hot-mount a newly-enabled plugin's routes (no restart).
    fastapi_app: object = None
    plugin_router_keys: set = field(default_factory=set)
    # (plugin_id, prefix) -> the Route objects currently serving it — what hot
    # REMOUNT removes when a reload carries fresh router code (ADR 0096 live QA:
    # iterating a scaffolded view served the stale first-mount handler forever).
    plugin_router_routes: dict = field(default_factory=dict)
    # Set by POST /api/restart: after uvicorn drains, _main re-execs a fresh process.
    restart_requested: bool = False
    # The live ``uvicorn.Server``, so a graceful self-shutdown can ask it to exit instead
    # of signalling the process. Self-signalling is not portable: on Windows ``os.kill``
    # delivers only CTRL_C_EVENT / CTRL_BREAK_EVENT and turns every other value into
    # TerminateProcess, which killed the server outright — no drain, no re-exec (#2585).
    uvicorn_server: object = None
    plugin_surfaces: list = field(default_factory=list)
    plugin_surface_handles: list = field(default_factory=list)
    # True once the startup hook has run its surface-start loop. A config reload uses
    # this to tell "surfaces not started yet" (the pending startup will start the
    # updated list — don't hot-start, or they'd double-start) from "startup done"
    # (reconcile: hot-start newly-enabled, stop removed) — ADR 0018 surface hot-reload.
    plugin_surfaces_started: bool = False
    plugin_meta: list = field(default_factory=list)
    # Background subsystems + handles.
    scheduler: Any = None
    background_mgr: Any = None  # ADR 0050 — detached background subagent jobs
    cache_warmer: Any = None
    goal_controller: Any = None
    watch_controller: Any = None  # ADR 0067 — standalone watch primitive
    main_loop: Any = None
    # The port this process actually bound to (populated by _main).
    active_port: int = 7870
    # Epoch of the last chat-turn start, for the ADR 0074 ``agent.active`` idle debounce.
    # None ⇒ no turn yet this process (the first turn emits with previous_state="boot").
    last_activity_ts: float | None = None


STATE = AppState()


def get_state() -> AppState:
    """The process-wide AppState (FastAPI dependency form)."""
    return STATE
