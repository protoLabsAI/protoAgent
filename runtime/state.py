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
    # Conversation checkpointer + prune bookkeeping.
    checkpointer: Any = None
    checkpoint_path: Any = None
    checkpoint_prune_task: Any = None
    monitor_goals_task: Any = None  # ADR 0030 monitor-goal cadence loop
    # Stores / registries bound into the active graph.
    knowledge_store: Any = None
    skills_index: Any = None
    workflow_registry: Any = None  # set by the workflows plugin (None = plugin disabled)
    workflow_run: Any = None  # async (name, inputs, on_step) -> result; set by the workflows plugin
    telemetry_store: Any = None
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
    plugin_skill_dirs: list = field(default_factory=list)
    plugin_middleware: list = field(default_factory=list)  # resolved AgentMiddleware instances (ADR 0032)
    plugin_late_tool_factories: list = field(default_factory=list)  # (all_tools, config) -> tool|list (late seam)
    plugin_workflow_dirs: list = field(default_factory=list)  # *.yaml recipe dirs (ADR 0027)
    plugin_a2a_skills: list = field(default_factory=list)  # A2A card skills from plugins (#570)
    plugin_chat_commands: dict = field(default_factory=dict)  # token -> handler; user-only /<name> control commands
    thread_id_resolver: object = None  # (request_metadata, session_id) -> str (#571)
    plugin_routers: list = field(default_factory=list)
    plugin_public_paths: list = field(default_factory=list)  # manifest auth-exempt prefixes
    # The live FastAPI app + the (plugin_id, prefix) keys already mounted on it —
    # lets a config reload hot-mount a newly-enabled plugin's routes (no restart).
    fastapi_app: object = None
    plugin_router_keys: set = field(default_factory=set)
    # Set by POST /api/restart: after uvicorn drains, _main re-execs a fresh process.
    restart_requested: bool = False
    plugin_surfaces: list = field(default_factory=list)
    plugin_surface_handles: list = field(default_factory=list)
    plugin_meta: list = field(default_factory=list)
    # Background subsystems + handles.
    scheduler: Any = None
    background_mgr: Any = None  # ADR 0050 — detached background subagent jobs
    cache_warmer: Any = None
    goal_controller: Any = None
    main_loop: Any = None
    # The port this process actually bound to (populated by _main).
    active_port: int = 7870


STATE = AppState()


def get_state() -> AppState:
    """The process-wide AppState (FastAPI dependency form)."""
    return STATE
