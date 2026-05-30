"""Settings schema — the single source of truth for the operator console's
generic Settings UI.

Each :class:`Field` maps a YAML path (``key``, e.g. ``compaction.enabled``) to
the ``LangGraphConfig`` attribute that holds its live value (``attr``), plus the
metadata the UI needs to render an input and tell the user whether a change
applies on save (hot-reload) or needs a process ``restart``.

The write path reuses ``_apply_settings_changes`` (validate → persist → reload),
so this module only has to: describe fields, read current values, and turn the
flat ``{key: value}`` payload the UI sends back into the nested dict the YAML
writer expects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Field:
    key: str                      # dotted YAML path, e.g. "model.temperature"
    attr: str                     # LangGraphConfig attribute holding the value
    label: str
    type: str                     # string|number|bool|select|string_list|secret
    section: str
    description: str = ""
    restart: bool = False         # True = needs a full process restart (not hot-reload)
    options: list[str] = field(default_factory=list)
    options_source: str = ""      # "models" → filled dynamically by the endpoint
    minimum: float | None = None
    maximum: float | None = None


# Ordered registry. Section order here is the order the UI renders groups in.
FIELDS: list[Field] = [
    # ── Model ────────────────────────────────────────────────────────────────
    Field("model.name", "model_name", "Primary model", "select", "Model",
          "The main reasoning model (gateway alias).", options_source="models"),
    Field("model.provider", "model_provider", "Provider", "string", "Model"),
    Field("model.api_base", "api_base", "API base URL", "string", "Model"),
    Field("model.api_key", "api_key", "API key", "secret", "Model",
          "Stored in secrets.yaml, never echoed back."),
    Field("model.temperature", "temperature", "Temperature", "number", "Model",
          minimum=0, maximum=2),
    Field("model.max_tokens", "max_tokens", "Max output tokens", "number", "Model", minimum=1),
    Field("model.max_iterations", "max_iterations", "Max tool iterations", "number", "Model",
          "Hard cap on the agent loop per turn.", minimum=1),

    # ── Routing ──────────────────────────────────────────────────────────────
    Field("routing.aux_model", "aux_model", "Auxiliary (fast) model", "string", "Routing",
          "Cheap/fast alias for summarization, goal-verification, and subagents. "
          "Blank = use the main model."),
    Field("routing.fallback_models", "routing_fallback_models", "Fallback models", "string_list",
          "Routing", "Retried in order when the primary model errors."),

    # ── Context compaction ───────────────────────────────────────────────────
    Field("compaction.enabled", "compaction_enabled", "Enable compaction", "bool", "Compaction",
          "Summarize old history near the context limit."),
    Field("compaction.trigger", "compaction_trigger", "Trigger", "string", "Compaction",
          'fraction:0.8 | tokens:120000 | messages:80 (fraction/tokens need a model profile).'),
    Field("compaction.keep_messages", "compaction_keep_messages", "Keep last N messages", "number",
          "Compaction", minimum=1),
    Field("compaction.model", "compaction_model", "Summarizer model", "string", "Compaction",
          "Blank = routing.aux_model, then the main model."),

    # ── Goal mode ────────────────────────────────────────────────────────────
    Field("goal.enabled", "goal_enabled", "Enable goal mode", "bool", "Goal mode"),
    Field("goal.max_iterations", "goal_max_iterations", "Max continuations", "number", "Goal mode",
          minimum=1),
    Field("goal.eval_model", "goal_eval_model", "Verifier model", "string", "Goal mode",
          "Blank = routing.aux_model, then the main model."),

    # ── Programmatic tool calling ────────────────────────────────────────────
    Field("execute_code.enabled", "execute_code_enabled", "Enable execute_code", "bool", "Tools",
          "Lets the model run one Python script composing many tools. SECURITY: runs "
          "model-authored code in a sandboxed subprocess — only enable for trusted "
          "models or in a hardened container."),
    Field("execute_code.timeout", "execute_code_timeout", "Script timeout (s)", "number", "Tools",
          minimum=1),

    # ── Prompt caching ───────────────────────────────────────────────────────
    Field("prompt_cache.enabled", "prompt_cache_enabled", "Enable prefix caching", "bool", "Caching",
          "Anthropic prefix caching on the stable prompt; no-op on non-Anthropic models."),
    Field("prompt_cache.ttl", "prompt_cache_ttl", "Cache TTL", "select", "Caching",
          options=["5m", "1h"]),
    Field("prompt_cache.warm.enabled", "cache_warming_enabled", "Cache warming", "bool", "Caching",
          "Reproduce the cached prefix on an interval (only for sporadic, latency-sensitive traffic)."),
    Field("prompt_cache.warm.interval_seconds", "cache_warming_interval_seconds",
          "Warm interval (s)", "number", "Caching", minimum=1),

    # ── Knowledge / memory ───────────────────────────────────────────────────
    Field("knowledge.top_k", "knowledge_top_k", "Knowledge recall top-k", "number", "Knowledge",
          minimum=1),
    Field("knowledge.embed_model", "embed_model", "Embedding model", "string", "Knowledge"),
    Field("skills.top_k", "skills_top_k", "Skill recall top-k", "number", "Knowledge", minimum=1),
    Field("checkpoint.db_path", "checkpoint_db_path", "Conversation history DB", "string", "Knowledge",
          "SQLite path for per-session chat history (survives restarts). Blank = in-memory.",
          restart=True),
    Field("checkpoint.keep_per_thread", "checkpoint_keep_per_thread", "History: keep N per session",
          "number", "Knowledge", "Latest checkpoints retained per chat session.", minimum=1),
    Field("checkpoint.max_age_days", "checkpoint_max_age_days", "History: max age (days)", "number",
          "Knowledge", "Drop whole sessions idle longer than this (0 = never).", minimum=0),
    Field("checkpoint.prune_interval_hours", "checkpoint_prune_interval_hours", "History: prune every (hours)",
          "number", "Knowledge", "How often the prune sweep runs (0 disables it).", minimum=0,
          restart=True),
    Field("checkpoint.harvest_enabled", "checkpoint_harvest_enabled", "History: harvest to knowledge", "bool",
          "Knowledge", "Summarize a session into the searchable knowledge base before pruning/deleting it."),

    # ── Middleware toggles ───────────────────────────────────────────────────
    Field("middleware.knowledge", "knowledge_middleware", "Knowledge middleware", "bool", "Middleware"),
    Field("middleware.memory", "memory_middleware", "Memory middleware", "bool", "Middleware"),
    Field("middleware.audit", "audit_middleware", "Audit middleware", "bool", "Middleware"),
    Field("middleware.scheduler", "scheduler_enabled", "Scheduler", "bool", "Middleware"),
    Field("middleware.enforcement", "enforcement_enabled", "Tool enforcement", "bool", "Middleware"),

    # ── Identity / operator ──────────────────────────────────────────────────
    Field("identity.name", "identity_name", "Agent name", "string", "Identity"),
    Field("identity.operator", "identity_operator", "Operator", "string", "Identity"),
    Field("operator.allowed_dirs", "operator_allowed_dirs", "Allowed project dirs", "string_list",
          "Identity", "Directories the beads/notes APIs may touch."),
    Field("auth.token", "auth_token", "A2A auth token", "secret", "Identity",
          "Bearer token for the A2A endpoint. Stored in secrets.yaml; applies live."),

    # ── Runtime (restart) ────────────────────────────────────────────────────
    Field("runtime.autostart_on_boot", "autostart_on_boot", "Autostart on boot", "bool", "Runtime",
          "Install/remove the boot LaunchAgent.", restart=True),
]

_BY_KEY = {f.key: f for f in FIELDS}
_SECRET_KEYS = {f.key for f in FIELDS if f.type == "secret"}


def build_schema(config, *, model_options: list[str] | None = None) -> list[dict[str, Any]]:
    """Return the settings schema grouped by section, with current values.

    Secrets report ``value: ""`` plus ``is_set`` rather than echoing the secret.
    """
    defaults = type(config)()
    groups: dict[str, dict[str, Any]] = {}
    for f in FIELDS:
        current = getattr(config, f.attr, None)
        entry: dict[str, Any] = {
            "key": f.key,
            "label": f.label,
            "type": f.type,
            "section": f.section,
            "description": f.description,
            "restart": f.restart,
            "options": (model_options or []) if f.options_source == "models" else list(f.options),
            "default": _jsonable(getattr(defaults, f.attr, None)),
        }
        if f.type == "secret":
            entry["value"] = ""
            entry["is_set"] = bool(current)
        else:
            entry["value"] = _jsonable(current)
        if f.minimum is not None:
            entry["minimum"] = f.minimum
        if f.maximum is not None:
            entry["maximum"] = f.maximum
        groups.setdefault(f.section, {"section": f.section, "fields": []})["fields"].append(entry)
    return list(groups.values())


def validate_flat(updates: dict[str, Any]) -> tuple[bool, str | None]:
    """Light per-field validation against the registry before persisting."""
    for key, val in updates.items():
        f = _BY_KEY.get(key)
        if f is None:
            return False, f"unknown setting: {key}"
        if f.type == "bool" and not isinstance(val, bool):
            return False, f"{key} must be a boolean"
        if f.type == "number":
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                return False, f"{key} must be a number"
            if f.minimum is not None and val < f.minimum:
                return False, f"{key} must be ≥ {f.minimum}"
            if f.maximum is not None and val > f.maximum:
                return False, f"{key} must be ≤ {f.maximum}"
        if f.type == "string_list" and not (isinstance(val, list) and all(isinstance(x, str) for x in val)):
            return False, f"{key} must be a list of strings"
        if f.type == "select" and f.options and val not in f.options:
            return False, f"{key} must be one of {f.options}"
    return True, None


def nest_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """Turn a flat ``{"model.temperature": 0.5}`` payload into the nested dict
    the YAML writer expects, dropping unset secrets (empty string)."""
    nested: dict[str, Any] = {}
    for key, val in updates.items():
        if key in _SECRET_KEYS and (val is None or val == ""):
            continue  # leave an existing secret untouched
        cursor = nested
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = val
    return nested


def restart_keys(updates: dict[str, Any]) -> list[str]:
    """Keys in the payload that need a process restart to take effect."""
    return [k for k in updates if (_BY_KEY.get(k) and _BY_KEY[k].restart)]


def _jsonable(val: Any) -> Any:
    if isinstance(val, (list, tuple)):
        return list(val)
    return val
