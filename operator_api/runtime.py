"""Runtime status contract for the React operator console."""

from __future__ import annotations

from typing import Any


def build_runtime_status(
    *,
    config: Any,
    setup_complete: bool,
    graph_loaded: bool,
    project_path: str = "",
    allowed_dirs: list[str] | None = None,
    knowledge_store: Any = None,
    scheduler: Any = None,
    cache_warmer: Any = None,
    goal_controller: Any = None,
    skills_index: Any = None,
) -> dict[str, Any]:
    """Return UI-safe runtime status.

    Secrets are represented as booleans only. The React setup/runtime screens
    need to know whether auth/model credentials exist, not what they are.

    ``allowed_dirs`` is the operator-console sandbox the client uses to
    populate the project-path picker; the server still enforces it.
    """
    project = {"path": project_path, "allowed_dirs": list(allowed_dirs or [])}

    skill_count = 0
    if skills_index is not None:
        try:
            skill_count = len(skills_index.all_skills())
        except Exception:  # noqa: BLE001 — status must never raise
            skill_count = 0

    if config is None:
        return {
            "setup_complete": bool(setup_complete),
            "graph_loaded": False,
            "project": project,
            "model": None,
            "identity": None,
            "middleware": {},
            "knowledge": {"enabled": False, "configured_path": None, "resolved_path": None},
            "skills": {"enabled": False, "count": skill_count, "configured_path": None},
            "scheduler": {"enabled": False, "backend": "disabled"},
            "goal": {"enabled": False, "controller_loaded": False},
            "cache_warmer": {"enabled": False, "loaded": False},
        }

    return {
        "setup_complete": bool(setup_complete),
        "graph_loaded": bool(graph_loaded),
        "project": project,
        "model": {
            "provider": getattr(config, "model_provider", ""),
            "name": getattr(config, "model_name", ""),
            "api_base": getattr(config, "api_base", ""),
            "api_key_configured": bool(getattr(config, "api_key", "")),
            "temperature": getattr(config, "temperature", None),
            "max_tokens": getattr(config, "max_tokens", None),
            "max_iterations": getattr(config, "max_iterations", None),
        },
        "identity": {
            "name": getattr(config, "identity_name", ""),
            "operator": getattr(config, "identity_operator", ""),
        },
        "middleware": {
            "knowledge": bool(getattr(config, "knowledge_middleware", False)),
            "audit": bool(getattr(config, "audit_middleware", False)),
            "memory": bool(getattr(config, "memory_middleware", False)),
            "scheduler": bool(getattr(config, "scheduler_enabled", False)),
            "enforcement": bool(getattr(config, "enforcement_enabled", False)),
            "ingest": bool(getattr(config, "ingest_enabled", False)),
            "prompt_cache": bool(getattr(config, "prompt_cache_enabled", False)),
            "compaction": bool(getattr(config, "compaction_enabled", False)),
            "execute_code": bool(getattr(config, "execute_code_enabled", False)),
        },
        "knowledge": {
            "enabled": bool(getattr(config, "knowledge_middleware", False)),
            "configured_path": getattr(config, "knowledge_db_path", None),
            "resolved_path": str(getattr(knowledge_store, "path", "") or "") or None,
            "top_k": getattr(config, "knowledge_top_k", None),
        },
        "skills": {
            "enabled": bool(getattr(config, "skills_enabled", False)),
            "count": skill_count,
            "configured_path": getattr(config, "skills_db_path", None),
            "top_k": getattr(config, "skills_top_k", None),
        },
        "scheduler": {
            "enabled": bool(getattr(config, "scheduler_enabled", False)),
            "backend": getattr(scheduler, "name", "disabled") if scheduler else "disabled",
        },
        "goal": {
            "enabled": bool(getattr(config, "goal_enabled", False)),
            "controller_loaded": goal_controller is not None,
            "max_iterations": getattr(config, "goal_max_iterations", None),
            "no_progress_limit": getattr(config, "goal_no_progress_limit", None),
        },
        "cache_warmer": {
            "enabled": bool(getattr(config, "cache_warming_enabled", False)),
            "loaded": cache_warmer is not None,
            "interval_seconds": getattr(config, "cache_warming_interval_seconds", None),
        },
    }
