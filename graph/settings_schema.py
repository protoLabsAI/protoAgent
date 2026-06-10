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
    # Cascade layer this field's shared default lives at (ADR 0047). "agent" (the
    # leaf) by default; "host" = box-shared default in host-config.yaml. Git-style
    # advisory — a field is always overridable at a lower layer, so this only sets
    # the home/default layer + where the settings UI writes it. No "app" value: the
    # App layer is the dataclass defaults (no writable file).
    scope: str = "agent"


# Ordered registry. Section order here is the order the UI renders groups in.
FIELDS: list[Field] = [
    # ── Agent runtime (ADR 0033) — leads the Agent settings: "who runs the turn?" ──
    Field("agent_runtime", "agent_runtime", "Agent runtime", "select", "Agent runtime",
          "Which brain drives a turn: the built-in LangGraph loop (native), or an external "
          "coding agent over ACP (needs its CLI installed + authenticated on the host).",
          options=["native", "acp:proto", "acp:codex", "acp:claude", "acp:copilot", "acp:opencode"]),
    Field("operator_mcp.tools", "operator_mcp_tools", "Tools exposed to the ACP brain", "string_list",
          "Agent runtime", "Allowlist of operator tools an external (ACP) brain may call via MCP — "
          "one per line, or `*` for all (minus execute_code). Empty = none. Ignored by native."),

    # ── Model ────────────────────────────────────────────────────────────────
    Field("model.name", "model_name", "Primary model", "select", "Model",
          "The main reasoning model (gateway alias).", options_source="models", scope="host"),
    Field("model.provider", "model_provider", "Provider", "string", "Model", scope="host"),
    Field("model.api_base", "api_base", "API base URL", "string", "Model", scope="host"),
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
          "Blank = use the main model.", scope="host"),
    Field("routing.fallback_models", "routing_fallback_models", "Fallback models", "string_list",
          "Routing", "Retried in order when the primary model errors.", scope="host"),

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
          "Anthropic prefix caching on the stable prompt; no-op on non-Anthropic models.", scope="host"),
    Field("prompt_cache.ttl", "prompt_cache_ttl", "Cache TTL", "select", "Caching",
          options=["5m", "1h"], scope="host"),
    Field("prompt_cache.warm.enabled", "cache_warming_enabled", "Cache warming", "bool", "Caching",
          "Reproduce the cached prefix on an interval (only for sporadic, latency-sensitive traffic).", scope="host"),
    Field("prompt_cache.warm.interval_seconds", "cache_warming_interval_seconds",
          "Warm interval (s)", "number", "Caching", minimum=1, scope="host"),

    # ── Knowledge / memory ───────────────────────────────────────────────────
    Field("knowledge.top_k", "knowledge_top_k", "Knowledge recall top-k", "number", "Knowledge",
          minimum=1),
    Field("knowledge.embeddings", "knowledge_embeddings", "Semantic recall (embeddings)", "bool", "Knowledge",
          "Hybrid FTS5 + vector search via the embedding model (RRF-fused). Off = "
          "keyword-only. Needs the gateway to serve the embedding model; falls back "
          "to keyword search on outage.", restart=True),
    Field("knowledge.embed_model", "embed_model", "Embedding model", "string", "Knowledge",
          "Gateway alias used when semantic recall is on."),
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
    Field("knowledge.facts", "knowledge_facts", "Extract semantic facts", "bool", "Knowledge",
          "On session retirement, also distil durable facts (aux model) and "
          "consolidate them into the store. Rides the harvest pass."),

    # ── Middleware toggles ───────────────────────────────────────────────────
    Field("middleware.knowledge", "knowledge_middleware", "Knowledge middleware", "bool", "Middleware"),
    Field("middleware.memory", "memory_middleware", "Memory middleware", "bool", "Middleware"),
    Field("middleware.audit", "audit_middleware", "Audit middleware", "bool", "Middleware"),
    Field("middleware.scheduler", "scheduler_enabled", "Scheduler", "bool", "Middleware"),
    Field("middleware.enforcement", "enforcement_enabled", "Tool enforcement", "bool", "Middleware"),

    # ── Telemetry (local cost/latency store, ADR 0006) ───────────────────────
    Field("telemetry.enabled", "telemetry_enabled", "Store telemetry locally", "bool", "Telemetry",
          "Persist a per-turn cost/latency row to a local SQLite DB (queryable in Settings → "
          "Telemetry). Off = nothing is recorded — no store is opened. Stays on your machine; "
          "it is never sent anywhere.", restart=True, scope="host"),
    Field("telemetry.retention_days", "telemetry_retention_days", "Telemetry retention (days)",
          "number", "Telemetry", "Auto-prune rows older than this (0 = keep forever).",
          minimum=0, restart=True, scope="host"),

    # ── Identity / operator ──────────────────────────────────────────────────
    Field("identity.name", "identity_name", "Agent name", "string", "Identity"),
    Field("identity.operator", "identity_operator", "Operator", "string", "Identity"),
    Field("identity.org", "identity_org", "Organization", "string", "Identity", scope="host"),
    Field("operator.allowed_dirs", "operator_allowed_dirs", "Allowed project dirs", "string_list",
          "Identity", "Directories the beads/notes APIs may touch."),
    Field("auth.token", "auth_token", "A2A auth token", "secret", "Identity",
          "Bearer token for the A2A endpoint. Stored in secrets.yaml; applies live."),

    # Discord's Settings group is now declared by the discord plugin's manifest
    # (ADR 0019) and rendered via the plugin-fields path in build_schema.

    # Google's Settings group is now declared by the google plugin's manifest
    # (ADR 0019) and rendered via the plugin-fields path in build_schema. The
    # "Connect Google" button (consent flow) is a console affordance, not a field.

    # ── Runtime (restart) ────────────────────────────────────────────────────
    Field("runtime.autostart_on_boot", "autostart_on_boot", "Autostart on boot", "bool", "Runtime",
          "Install/remove the boot LaunchAgent.", restart=True),
]

_BY_KEY = {f.key: f for f in FIELDS}
_SECRET_KEYS = {f.key for f in FIELDS if f.type == "secret"}
_HOST_KEYS = {f.key for f in FIELDS if getattr(f, "scope", "agent") == "host"}


def host_keys() -> set[str]:
    """Dotted keys whose home/default cascade layer is the Host file (ADR 0047
    ``scope=="host"``). The write path filters host-layer saves to these so the
    host file can't accumulate agent-only settings (D1/D4)."""
    return set(_HOST_KEYS)


def is_secret_key(key: str) -> bool:
    """True for a secret-typed FIELD (ADR 0047 D5 — secrets are agent-leaf only,
    never written to the non-secret Host file)."""
    return key in _SECRET_KEYS


def is_known_key(key: str) -> bool:
    """True iff ``key`` is a known core or plugin-declared settings key. The
    reset path uses this as an existence-only gate (a reset has no value, so the
    per-type ``validate_flat`` checks don't apply)."""
    if key in _BY_KEY:
        return True
    return any(full == key for _, full, _, _ in _plugin_field_specs())


def _plugin_field_specs():
    """Plugin-declared settings fields (ADR 0019) as (schema, full_key, key, spec)
    — ``full_key`` is the dotted YAML path ``<section>.<key>`` the save writes to.
    Best-effort; empty when no plugin declares settings."""
    try:
        from graph.plugins.pconfig import live_plugin_config_schemas

        out = []
        for sch in live_plugin_config_schemas():
            for spec in sch.settings:
                key = spec.get("key")
                if key:
                    out.append((sch, f"{sch.section}.{key}", key, spec))
        return out
    except Exception:  # noqa: BLE001 — plugin discovery is best-effort
        return []


def _plugin_group(sch, spec) -> str:
    return spec.get("group") or sch.section.replace("_", " ").title()


# Settings categories (ADR 0020) — fold the flat sections into a small,
# navigable taxonomy so the surface isn't one long scroll. Order here is the
# category the console routes each section to. Settings are decentralized: Agent +
# Memory render in their home views (Agent → Settings, Knowledge → Settings), while
# Plugins + System stay in the central Settings surface. Unknown sections (notably
# plugin-contributed ones, ADR 0019) default to "Plugins".
_CATEGORY_ORDER = ["Agent", "Memory", "Plugins", "System"]
_SECTION_CATEGORY = {
    # Agent — who it is + how it behaves (rendered in the Agent view's Settings tab).
    "Identity": "Agent",
    "Model": "Agent",
    "Routing": "Agent",
    "Agent runtime": "Agent",
    "Goal mode": "Agent",
    "Tools": "Agent",
    # Memory — knowledge/recall config (rendered in the Knowledge view's Settings tab).
    "Knowledge": "Memory",
    # System — runtime + performance knobs (central Settings → System).
    "Compaction": "System",
    "Caching": "System",
    "Middleware": "System",
    "Runtime": "System",
    "Telemetry": "System",
    # Discord / Google / other plugin sections → "Plugins" (the default).
}


def _category_for(section: str) -> str:
    return _SECTION_CATEGORY.get(section, "Plugins")


def _resolve_dotted(doc: dict | None, dotted: str) -> bool:
    """True iff ``dotted`` (e.g. ``"prompt_cache.warm.enabled"``) resolves in ``doc``."""
    cur: Any = doc
    if not isinstance(cur, dict):
        return False
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _source_for(key: str, agent_doc: dict | None, host_doc: dict | None) -> str:
    """Which cascade layer the live value came from (ADR 0047): the agent leaf if it
    sets the key, else the Host layer, else the App default. Drives the UI's
    inherited-vs-overridden badge."""
    if _resolve_dotted(agent_doc, key):
        return "agent"
    if _resolve_dotted(host_doc, key):
        return "host"
    return "default"


def build_schema(
    config,
    *,
    model_options: list[str] | None = None,
    agent_doc: dict | None = None,
    host_doc: dict | None = None,
) -> list[dict[str, Any]]:
    """Return the settings schema grouped by section, with current values.

    Each group carries a ``category`` (ADR 0020) so the console can present a
    category sub-nav instead of a flat scroll. Groups are ordered by category
    (``_CATEGORY_ORDER``), then by their first appearance in ``FIELDS``.

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
            "scope": f.scope,  # ADR 0047: "agent" | "host"
            "source": _source_for(f.key, agent_doc, host_doc),  # which layer set the live value
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

    # Plugin-declared settings fields (ADR 0019) — value from config.plugin_config,
    # rendered + saved through the same generic Settings surface (key = dotted
    # YAML path, so apply_updates_to_yaml + secret routing handle it for free).
    plugin_cfg = getattr(config, "plugin_config", {}) or {}
    for sch, full_key, key, spec in _plugin_field_specs():
        section_cfg = plugin_cfg.get(sch.section) or sch.defaults
        current = section_cfg.get(key)
        ftype = spec.get("type", "string")
        group = _plugin_group(sch, spec)
        entry = {
            "key": full_key,
            "label": spec.get("label", key),
            "type": ftype,
            "section": group,
            "description": spec.get("description", ""),
            "restart": bool(spec.get("restart", False)),
            "options": list(spec.get("options", []) or []),
            "default": _jsonable(sch.defaults.get(key)),
            "scope": "agent",  # plugin config is agent-local (ADR 0047 D6)
            "source": "agent" if current is not None else "default",
        }
        if ftype == "secret":
            entry["value"] = ""
            entry["is_set"] = bool(current)
        else:
            entry["value"] = _jsonable(current)
        if spec.get("minimum") is not None:
            entry["minimum"] = spec["minimum"]
        if spec.get("maximum") is not None:
            entry["maximum"] = spec["maximum"]
        groups.setdefault(group, {"section": group, "fields": []})["fields"].append(entry)
        # A plugin that declares `test: true` (ADR 0029) gets a generic console
        # "Test connection" button posting the group's fields to its test route.
        if getattr(sch, "test", False):
            groups[group]["test"] = {"endpoint": f"/api/config/test-{sch.section}"}

    out = list(groups.values())
    # Insertion order = first appearance in FIELDS (core), then plugins.
    section_pos = {g["section"]: i for i, g in enumerate(out)}
    for g in out:
        g["category"] = _category_for(g["section"])

    def _sort_key(g: dict) -> tuple[int, int]:
        cat = g["category"]
        cat_rank = _CATEGORY_ORDER.index(cat) if cat in _CATEGORY_ORDER else len(_CATEGORY_ORDER)
        return (cat_rank, section_pos[g["section"]])

    out.sort(key=_sort_key)
    return out


def validate_flat(updates: dict[str, Any]) -> tuple[bool, str | None]:
    """Light per-field validation against the registry before persisting."""
    plugin_keys = {full: spec for _, full, _, spec in _plugin_field_specs()}
    for key, val in updates.items():
        f = _BY_KEY.get(key)
        if f is None:
            spec = plugin_keys.get(key)
            if spec is None:
                return False, f"unknown setting: {key}"
            t = spec.get("type", "string")
            if t == "bool" and not isinstance(val, bool):
                return False, f"{key} must be a boolean"
            if t == "number" and (not isinstance(val, (int, float)) or isinstance(val, bool)):
                return False, f"{key} must be a number"
            continue
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
