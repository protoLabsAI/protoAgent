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
    skills_index: Any = None,
    mcp: dict[str, Any] | None = None,
    plugins: list[dict[str, Any]] | None = None,
    telemetry_store: Any = None,
    checkpoint_path: str = "",
    warnings: list[str] | None = None,
    instance_uid: str = "",
    version: str = "",
) -> dict[str, Any]:
    """Return UI-safe runtime status.

    Secrets are represented as booleans only. The React setup/runtime screens
    need to know whether auth/model credentials exist, not what they are.

    ``allowed_dirs`` is the operator-console sandbox the client uses to
    populate the project-path picker; the server still enforces it.

    ``warnings`` are user-facing operational alerts (e.g. a live co-located
    instance sharing this data root, #706) — the shell banners them.

    ``version`` is this instance's app version (pyproject ``[project].version``) —
    the console↔server ``/api/*`` surface has no other versioning, and with remote
    fleet members (ADR 0042 §I) a hub console can drive a DIFFERENT release by
    proxy, so skew must at least be visible.
    """
    warnings_block = [w for w in (warnings or []) if w]
    project = {"path": project_path, "allowed_dirs": list(allowed_dirs or [])}

    skill_count = 0
    if skills_index is not None:
        try:
            skill_count = len(skills_index.all_skills())
        except Exception:  # noqa: BLE001 — status must never raise
            skill_count = 0

    mcp_block = mcp or {"enabled": False, "servers": [], "tool_count": 0}
    plugins_block = list(plugins or [])

    if config is None:
        return {
            "setup_complete": bool(setup_complete),
            "graph_loaded": False,
            "project": project,
            "agent_runtime": "native",
            "model": None,
            "identity": None,
            "middleware": {},
            "knowledge": {"enabled": False, "status": "initializing", "configured_path": None, "resolved_path": None},
            "skills": {"enabled": False, "count": skill_count, "configured_path": None},
            "mcp": mcp_block,
            "plugins": plugins_block,
            "scheduler": {"enabled": False, "backend": "disabled"},
            "cache_warmer": {"enabled": False, "loaded": False},
            "warnings": warnings_block,
            "instance_uid": instance_uid,
            "version": version,
        }

    # Knowledge can be ON (the config flag) but still building its store during the
    # boot/recompile window — surface that as "initializing" so the UI doesn't read a
    # warming store as "disabled" (the flag is the source of truth for on/off).
    _kn_on = bool(getattr(config, "knowledge_middleware", False))
    _kn_resolved = str(getattr(knowledge_store, "path", "") or "") or None
    _kn_status = "disabled" if not _kn_on else ("ready" if _kn_resolved else "initializing")

    return {
        "setup_complete": bool(setup_complete),
        "graph_loaded": bool(graph_loaded),
        "project": project,
        # Which brain drives a turn (ADR 0033): "native" = the LangGraph loop, or
        # "acp:<agent>" = an external coding agent. The console reads this to stop
        # misrepresenting an ACP turn as the gateway model + to flag that protoAgent
        # skills/commands don't apply in coding-agent mode.
        "agent_runtime": str(getattr(config, "agent_runtime", "native") or "native"),
        "model": {
            "provider": getattr(config, "model_provider", ""),
            "name": getattr(config, "model_name", ""),
            "api_base": getattr(config, "api_base", ""),
            "api_key_configured": bool(getattr(config, "api_key", "")),
            "temperature": getattr(config, "temperature", None),
            "max_tokens": getattr(config, "max_tokens", None),
            "max_iterations": getattr(config, "max_iterations", None),
            "vision": bool(getattr(config, "model_vision", False)),
        },
        "identity": {
            "name": getattr(config, "identity_name", ""),
            "operator": getattr(config, "identity_operator", ""),
            "org": getattr(config, "identity_org", ""),
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
            "enabled": _kn_on,
            "status": _kn_status,
            "configured_path": getattr(config, "knowledge_db_path", None),
            "resolved_path": _kn_resolved,
            "top_k": getattr(config, "knowledge_top_k", None),
        },
        "skills": {
            "enabled": bool(getattr(config, "skills_enabled", False)),
            "count": skill_count,
            "configured_path": getattr(config, "skills_db_path", None),
            "top_k": getattr(config, "skills_top_k", None),
        },
        "mcp": mcp_block,
        "plugins": plugins_block,
        "scheduler": {
            "enabled": bool(getattr(config, "scheduler_enabled", False)),
            "backend": getattr(scheduler, "name", "disabled") if scheduler else "disabled",
        },
        "cache_warmer": {
            "enabled": bool(getattr(config, "cache_warming_enabled", False)),
            "loaded": cache_warmer is not None,
            "interval_seconds": getattr(config, "cache_warming_interval_seconds", None),
        },
        # On-disk store sizes (bytes) so growth is visible from the console.
        "storage": {
            "knowledge_bytes": _file_size(getattr(knowledge_store, "path", None)),
            "telemetry_bytes": _file_size(getattr(telemetry_store, "path", None)),
            "checkpoint_bytes": _file_size(checkpoint_path),
            "skills_bytes": _file_size(getattr(skills_index, "path", None)),
            "telemetry_retention_days": getattr(config, "telemetry_retention_days", None),
        },
        "warnings": warnings_block,
        # Stable per-data-root uid — the console keys per-origin client state on it
        # (a different backend on the same address must not render this one's chats).
        "instance_uid": instance_uid,
        "version": version,
    }


def _file_size(path: Any) -> int | None:
    """File size in bytes, or None if the path is unset/missing (best-effort)."""
    if not path:
        return None
    try:
        import os

        return os.path.getsize(str(path))
    except OSError:
        return None
