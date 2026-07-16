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

from runtime.acp_agents import acp_runtime_options


@dataclass
class Field:
    key: str  # dotted YAML path, e.g. "model.temperature"
    attr: str  # LangGraphConfig attribute holding the value
    label: str
    type: str  # string|text|number|bool|select|string_list|secret
    section: str
    description: str = ""
    restart: bool = False  # True = needs a full process restart (not hot-reload)
    options: list[str] = field(default_factory=list)
    options_source: str = ""  # "models" → filled dynamically by the endpoint
    minimum: float | None = None
    maximum: float | None = None
    # Conditional visibility (#963): {"key": "<sibling field key>", "equals": <value>}
    # — or {"key", "in": [...]}, or just {"key"} for "is truthy". The console hides
    # this field until the named sibling's *current form value* satisfies it (reactive
    # to the in-form value, not just the saved one). The sibling key is the full dotted
    # path for core fields; plugin specs may use the short key (resolved at build time).
    depends_on: dict | None = None
    # Cascade layer this field's shared default lives at (ADR 0047). "agent" (the
    # leaf) by default; "host" = box-shared default in host-config.yaml. Git-style
    # advisory — a field is always overridable at a lower layer, so this only sets
    # the home/default layer + where the settings UI writes it. No "app" value: the
    # App layer is the dataclass defaults (no writable file).
    scope: str = "agent"
    # Keep the field in FIELDS (so it round-trips through config_to_dict / the YAML
    # writer) but DON'T render it in the generic Settings UI — for a key a dedicated
    # panel already owns. `build_schema` skips it; `config_to_dict` keeps it. (#1076)
    ui_hidden: bool = False


# ACP coding-agent choices, offered as the main-brain runtime AND as model overrides for the
# auxiliary slots (aux / goal-eval / compaction). Derived from the canonical catalog
# (runtime/acp_agents.py) so the list lives in exactly one place.
ACP_MODEL_OPTIONS = acp_runtime_options()


# Ordered registry. Section order here is the order the UI renders groups in.
FIELDS: list[Field] = [
    # ── Agent runtime (ADR 0033) — leads the Agent settings: "who runs the turn?" ──
    Field(
        "agent_runtime",
        "agent_runtime",
        "Agent runtime",
        "select",
        "Agent runtime",
        "Which brain drives a turn: the built-in LangGraph loop (native), or an external "
        "coding agent over ACP (needs its CLI installed + authenticated on the host).",
        # "runtime" = native + every ACP agent, resolved per-request from config.acp_agents so
        # user-registered custom agents show up too (build_schema). Dynamic source ⇒ no static
        # enum, so validate_flat accepts a custom acp:<id> — same as the model.name select.
        options_source="runtime",
    ),
    Field(
        "operator_mcp.tools",
        "operator_mcp_tools",
        "Restrict tools for the ACP brain",
        "string_list",
        "Agent runtime",
        "Optional restriction on which operator tools an external (ACP) brain may call via "
        "MCP — one per line, or `*` for all. Empty = the full toolset (parity with the native "
        "runtime, minus execute_code the coding agent already has). Ignored by native.",
    ),
    # ── Model ────────────────────────────────────────────────────────────────
    Field(
        "model.name",
        "model_name",
        "Primary model",
        "select",
        "Model",
        "The main reasoning model (gateway alias).",
        options_source="models",
        scope="host",
    ),
    Field("model.provider", "model_provider", "Provider", "string", "Model", scope="host"),
    Field("model.api_base", "api_base", "API base URL", "string", "Model", scope="host"),
    Field("model.api_key", "api_key", "API key", "secret", "Model", "Stored in secrets.yaml, never echoed back."),
    Field("model.temperature", "temperature", "Temperature", "number", "Model", minimum=0, maximum=2),
    Field("model.max_tokens", "max_tokens", "Max output tokens", "number", "Model", minimum=1),
    Field(
        "model.thinking",
        "thinking",
        "Thinking mode",
        "select",
        "Model",
        "Reasoning models on an OpenAI-compatible gateway (e.g. DeepSeek) can toggle their "
        "thinking/reasoning step. Blank = inherit the provider default. Note: with thinking on, "
        "DeepSeek ignores temperature / top-p / penalties.",
        options=["", "enabled", "disabled"],
    ),
    Field(
        "model.reasoning_effort",
        "reasoning_effort",
        "Reasoning effort",
        "select",
        "Model",
        "How hard a reasoning model thinks. Blank = inherit the provider default. DeepSeek maps "
        "low/medium to high and treats max as the ceiling; the OpenAI o-series uses these directly.",
        options=["", "low", "medium", "high", "max"],
    ),
    Field(
        "model.vision",
        "model_vision",
        "Vision (native images)",
        "bool",
        "Model",
        "Turn on when the primary model accepts images (e.g. protolabs/fast, protolabs/smart). "
        "Chat then sends attached images straight to the model as native multimodal parts "
        "instead of through the extraction pipeline.",
        restart=True,
    ),
    Field(
        "model.max_iterations",
        "max_iterations",
        "Max tool iterations",
        "number",
        "Model",
        "Hard cap on the agent loop per turn.",
        minimum=1,
    ),
    # ── Favorite models (#1957) ──────────────────────────────────────────────
    Field(
        "model.favorites",
        "model_favorites",
        "Favorites",
        "string_list",
        "Favorite models",
        "Pinned go-to models for the chat `/model` quick-switch — the inline picker offers "
        "these, in this order, instead of the gateway's full list. Add, remove, and reorder "
        "here; empty = /model shows every gateway model.",
        options_source="models",
    ),
    # ── Routing ──────────────────────────────────────────────────────────────
    Field(
        "routing.aux_model",
        "aux_model",
        "Auxiliary (fast) model",
        "string",
        "Routing",
        "Cheap/fast alias for summarization, goal-verification, and subagents. Blank = use the "
        "main model. Or pick an `acp:<agent>` to route these aux calls through a coding agent "
        "(e.g. Opus via acp:claude) — needs that agent's CLI on the host.",
        options_source="models+acp",
        scope="host",
    ),
    Field(
        "routing.fallback_models",
        "routing_fallback_models",
        "Fallback models",
        "string_list",
        "Routing",
        "Retried in order when the primary model errors.",
        options_source="models",
        scope="host",
    ),
    # ── Context compaction ───────────────────────────────────────────────────
    Field(
        "compaction.enabled",
        "compaction_enabled",
        "Enable compaction",
        "bool",
        "Compaction",
        "Summarize old history near the context limit.",
    ),
    Field(
        "compaction.trigger",
        "compaction_trigger",
        "Trigger",
        "string",
        "Compaction",
        "fraction:0.8 | tokens:120000 | messages:80 (fraction/tokens need a model profile).",
    ),
    Field(
        "compaction.keep_messages",
        "compaction_keep_messages",
        "Keep last N messages",
        "number",
        "Compaction",
        minimum=1,
    ),
    Field(
        "compaction.model",
        "compaction_model",
        "Summarizer model",
        "string",
        "Compaction",
        "Blank = routing.aux_model, then the main model. Accepts an `acp:<agent>` to summarize "
        "with a coding agent.",
        options_source="models+acp",
    ),
    # ── Goal mode ────────────────────────────────────────────────────────────
    # Goal mode is always on (config default True). The on/off toggle is hidden from
    # the Settings UI; the field stays in FIELDS so existing configs round-trip and the
    # YAML value (if any) is still honored. Tuning knobs below remain user-editable.
    Field("goal.enabled", "goal_enabled", "Enable goal mode", "bool", "Goal mode", ui_hidden=True),
    Field("goal.max_iterations", "goal_max_iterations", "Max continuations", "number", "Goal mode", minimum=1),
    Field(
        "goal.eval_model",
        "goal_eval_model",
        "Verifier model",
        "string",
        "Goal mode",
        "Blank = routing.aux_model, then the main model. Accepts an `acp:<agent>` to verify goals "
        "with a coding agent.",
        options_source="models+acp",
    ),
    # ── Persona (self-authored SOUL) ─────────────────────────────────────────
    # Guarded, default OFF. When on, the lead agent gets the `edit_soul` tool and can
    # rewrite sections of its own SOUL.md (persona only — ADR 0079; every edit snapshotted
    # + reversible via #1691). Crossing the operator-trust boundary (ADR 0066/0081) is an
    # opt-in: settable via YAML/API, but hidden from the generic Settings UI for now — the
    # dedicated Identity panel already owns SOUL, and this reload-live toggle can join it
    # later. ui_hidden keeps it in FIELDS so it round-trips.
    Field(
        "soul.self_edit_enabled",
        "soul_self_edit_enabled",
        "Let the agent edit its own persona (SOUL.md)",
        "bool",
        "Persona",
        "When on, the lead agent gets the `edit_soul` tool to rewrite sections of its own "
        "SOUL.md. Persona only, never operating doctrine (ADR 0079); each edit is snapshotted "
        "and reversible, and applies on the next turn.",
        ui_hidden=True,
    ),
    # ── Prompt caching ───────────────────────────────────────────────────────
    Field(
        "prompt_cache.enabled",
        "prompt_cache_enabled",
        "Enable prefix caching",
        "bool",
        "Caching",
        "Anthropic prefix caching on the stable prompt; no-op on non-Anthropic models.",
        scope="host",
    ),
    Field("prompt_cache.ttl", "prompt_cache_ttl", "Cache TTL", "select", "Caching", options=["5m", "1h"], scope="host"),
    Field(
        "prompt_cache.warm.enabled",
        "cache_warming_enabled",
        "Cache warming",
        "bool",
        "Caching",
        "Reproduce the cached prefix on an interval (only for sporadic, latency-sensitive traffic).",
        scope="host",
    ),
    Field(
        "prompt_cache.warm.interval_seconds",
        "cache_warming_interval_seconds",
        "Warm interval (s)",
        "number",
        "Caching",
        minimum=1,
        scope="host",
    ),
    # ── Knowledge / memory ───────────────────────────────────────────────────
    Field("knowledge.top_k", "knowledge_top_k", "Knowledge recall top-k", "number", "Knowledge", minimum=1),
    # Scope filter for the auto-inject RAG search (ADR 0069 D3a).
    Field(
        "knowledge.inject_namespaces",
        "knowledge_inject_namespaces",
        "Auto-inject namespaces",
        "string_list",
        "Knowledge",
        "Restrict per-turn auto-injected knowledge (RAG) to chunks in these namespaces — one "
        "per line; an empty line matches un-namespaced chunks. Empty = no filter (everything "
        "is eligible, today's behavior). Tool-driven recall (memory_recall) is not affected.",
    ),
    # Trust floor for the auto-inject RAG hits (ADR 0069 D8).
    Field(
        "knowledge.inject_min_trust",
        "knowledge_inject_min_trust",
        "Auto-inject trust floor",
        "number",
        "Knowledge",
        "Minimum trust tier a knowledge chunk needs to be auto-injected into the prompt. "
        "1 = everything (low-trust hits are only ranked below higher tiers); 2 = exclude "
        "ingested/external content (web, YouTube, PDFs, media); 3 = operator-authored rows "
        "only. Tool-driven recall (memory_recall) is never gated — excluded content stays "
        "reachable on demand, tier visible.",
        minimum=1,
        maximum=3,
    ),
    # Hot-memory write confirm gate (ADR 0069 D8).
    Field(
        "knowledge.hot_write_confirm",
        "knowledge_hot_write_confirm",
        "Confirm agent hot-memory writes",
        "bool",
        "Knowledge",
        "When on, the agent's memory_ingest tool refuses to write always-on hot memory "
        "(domain \"hot\") and instructs the model to ask you instead — only operator "
        "surfaces (the knowledge browser / memory inspector) can put facts in front of the "
        "model every turn. Every hot write emits a memory.hot_written event either way.",
    ),
    # Tier (ADR 0041 / bd-2wu) — mirrors `skills.scope`; the commons lives at `commons.path`.
    Field(
        "knowledge.scope",
        "knowledge_scope",
        "Knowledge sharing",
        "select",
        "Knowledge",
        "How this agent uses the knowledge base: scoped (private only) · shared (the one "
        "box commons) · layered (read the commons ∪ private, write private, promote proven "
        "facts up). Blank = scoped. A shared/layered fleet must share one embedding model.",
        options=["scoped", "shared", "layered"],
        restart=True,
    ),
    Field(
        "knowledge.embeddings",
        "knowledge_embeddings",
        "Semantic recall (embeddings)",
        "bool",
        "Knowledge",
        "Hybrid FTS5 + vector search via the embedding model (RRF-fused). Off = "
        "keyword-only. Needs the gateway to serve the embedding model; falls back "
        "to keyword search on outage.",
        restart=True,
    ),
    Field(
        "knowledge.embed_model",
        "embed_model",
        "Embedding model",
        "select",
        "Knowledge",
        "Gateway model used to embed for semantic recall (NOT the chat model). Picked from "
        "the models your gateway serves — a wrong alias silently degrades recall to "
        "keyword-only. Falls back to a free-text field if the gateway can't be listed.",
        options_source="models",
    ),
    Field(
        "knowledge.transcribe_model",
        "transcribe_model",
        "Transcription model",
        "string",
        "Knowledge",
        "Gateway speech-to-text model for audio/video ingestion (e.g. whisper-1), via the "
        "OpenAI-compatible /audio/transcriptions endpoint. Blank disables audio/video import. "
        "Video needs ffmpeg on the host to extract the audio track.",
        options_source="models",
        restart=True,
    ),
    Field(
        "knowledge.image_describe_model",
        "image_describe_model",
        "Image description model",
        "string",
        "Knowledge",
        "Vision-capable gateway model used to DESCRIBE attached images when the chat model "
        "can't see them (text-only). The screenshot is sent to this model; its description + "
        "any transcribed text is inlined as context. Blank disables image attachments on "
        "non-vision models (they error with a clear message). Needs a vision model (e.g. "
        "protolabs/smart); the chat model can stay text-only.",
        options_source="models",
        restart=True,
    ),
    Field(
        "knowledge.recall_preview_chars",
        "knowledge_recall_preview_chars",
        "Recall preview length",
        "number",
        "Knowledge",
        "How many characters of each recalled chunk the model sees. Bigger carries more "
        "answer-bearing context at no retrieval cost.",
        minimum=1,
        restart=True,
    ),
    Field(
        "knowledge.vector_k",
        "knowledge_vector_k",
        "Hybrid candidate pool",
        "number",
        "Knowledge",
        "How many FTS5 + vector candidates are fused (RRF) per query before the top-k cut. "
        "Bigger = wider recall, slightly slower.",
        minimum=1,
        restart=True,
    ),
    Field(
        "knowledge.rrf_k",
        "knowledge_rrf_k",
        "RRF constant (k)",
        "number",
        "Knowledge",
        "Reciprocal-Rank-Fusion constant. Higher flattens the rank weighting (60 is the standard default).",
        minimum=1,
        restart=True,
    ),
    Field(
        "knowledge.min_score",
        "knowledge_min_score",
        "Recall relevance floor",
        "number",
        "Knowledge",
        "Drop fused hits below this score; 0 keeps all. A floor stops off-topic turns from "
        "injecting weak chunks — RRF scores aren't normalized, so tune empirically.",
        minimum=0,
        restart=True,
    ),
    Field(
        "knowledge.chunk_max_chars",
        "knowledge_chunk_max_chars",
        "Ingest chunk size",
        "number",
        "Knowledge",
        "Large ingests (conversation summaries, pasted docs) are split into pieces at most "
        "this many characters before embedding, so each passage gets its own vector. Smaller = "
        "more precise recall, more rows. Content under this size isn't split.",
        minimum=1,
        restart=True,
    ),
    Field(
        "knowledge.chunk_overlap_chars",
        "knowledge_chunk_overlap_chars",
        "Ingest chunk overlap",
        "number",
        "Knowledge",
        "Characters shared between adjacent chunks so a span straddling a split is still wholly "
        "present in one chunk. 0 = no overlap.",
        minimum=0,
        restart=True,
    ),
    Field(
        "knowledge.contextual_enrichment",
        "knowledge_contextual_enrichment",
        "Contextual enrichment",
        "bool",
        "Knowledge",
        "When a document splits, prepend a one-line aux-LLM context situating each chunk in the "
        "whole document before embedding — lifts both semantic and keyword recall (Anthropic's "
        "Contextual Retrieval). Costs one aux call per chunk at ingest (not on the query path). "
        "Off by default.",
        restart=True,
    ),
    Field(
        "knowledge.attach_inline_budget",
        "knowledge_attach_inline_budget",
        "Chat attachment inline budget",
        "number",
        "Knowledge",
        "A file dropped in chat is inlined whole if its text is at or under this many "
        "characters; a larger doc is indexed for retrieval instead, with only a lede of this "
        "length inlined — so a big document never gets dumped into the turn.",
        minimum=1,
    ),
    # minimum=0 (not 1): 0 = "index off, but /slash + load_skill still work" — a
    # coherent middle ground vs skills.enabled:false (which kills everything). The
    # runtime (KnowledgeMiddleware + runtime/context.py) honors 0, so the schema must
    # let it through validation (ADR 0060).
    Field("skills.top_k", "skills_top_k", "Skills listed in context", "number", "Knowledge", minimum=0),
    Field(
        "checkpoint.db_path",
        "checkpoint_db_path",
        "Conversation history DB",
        "string",
        "Knowledge",
        "SQLite path for per-session chat history (survives restarts). Blank = in-memory.",
        restart=True,
    ),
    Field(
        "checkpoint.keep_per_thread",
        "checkpoint_keep_per_thread",
        "History: keep N per session",
        "number",
        "Knowledge",
        "Latest checkpoints retained per chat session.",
        minimum=1,
    ),
    Field(
        "checkpoint.max_age_days",
        "checkpoint_max_age_days",
        "History: max age (days)",
        "number",
        "Knowledge",
        "Drop whole sessions idle longer than this (0 = never).",
        minimum=0,
    ),
    Field(
        "checkpoint.prune_interval_hours",
        "checkpoint_prune_interval_hours",
        "History: prune every (hours)",
        "number",
        "Knowledge",
        "How often the prune sweep runs (0 disables it).",
        minimum=0,
        restart=True,
    ),
    Field(
        "checkpoint.harvest_enabled",
        "checkpoint_harvest_enabled",
        "History: harvest to knowledge",
        "bool",
        "Knowledge",
        "Summarize a session into the searchable knowledge base before pruning/deleting it.",
    ),
    Field(
        "checkpoint.vacuum",
        "checkpoint_vacuum",
        "History: reclaim disk after prune",
        "bool",
        "Knowledge",
        "After a prune frees rows, VACUUM + truncate the WAL so the DB file shrinks instead of holding the freed space.",
    ),
    Field(
        "knowledge.facts",
        "knowledge_facts",
        "Extract semantic facts",
        "bool",
        "Knowledge",
        "On session retirement, also distil durable facts (aux model) and "
        "consolidate them into the store. Rides the harvest pass.",
    ),
    # ── Skills (ADR 0041 tiered stores) ──────────────────────────────────────
    # `skills.scope` is the agent's own choice of how it participates in the
    # fleet's skill library; `commons.path` is the box-shared location of that
    # library (host-scoped — every agent on the box reads the same commons).
    Field(
        "skills.scope",
        "skills_scope",
        "Skill sharing",
        "select",
        "Skills",
        "How this agent uses the skill library: scoped (private only) · shared "
        "(the one box commons) · layered (read the commons ∪ private, write "
        "private, promote proven skills up). Blank = derived (scoped).",
        options=["scoped", "shared", "layered"],
    ),
    Field(
        "commons.path",
        "commons_path",
        "Shared skills location",
        "string",
        "Skills",
        "Box-shared skill library read by every agent on this machine. Blank = ~/.protoagent/commons.",
        scope="host",
    ),
    # MCP server sharing tier (ADR 0041) — mirrors skills.scope. The commons of shared
    # servers lives at `commons.path`/mcp-servers.json; share/unshare moves a server
    # between this agent's config and that commons (Settings ▸ MCP).
    Field(
        "mcp.scope",
        "mcp_scope",
        "MCP server sharing",
        "select",
        "MCP",
        "How this agent uses MCP servers: scoped (only this agent's servers) · layered "
        "(also run the box commons — every agent on this machine shares those — ∪ your "
        "own). Blank = scoped. A shared server runs as a subprocess with its configured "
        "secrets on every agent, so share only servers you trust box-wide.",
        options=["scoped", "layered"],
    ),
    # ── Filesystem (ADR 0007 operator primitives) — the fenced project fs toolset,
    # incl. the dual-use ``run_command``. Per-agent (leaf scope): an operator can
    # fully remove run_command for one agent while siblings keep it. Hot-reloads:
    # a save rebuilds the graph, so binding changes apply without a restart. ──────
    Field(
        "filesystem.enabled",
        "filesystem_enabled",
        "Filesystem tools",
        "bool",
        "Filesystem",
        "The fenced multi-project filesystem toolset (list/read/search/write/edit + "
        "run_command). Off = none of them are bound. With no explicit projects "
        "configured, a default read-write `workspace` project is provided.",
    ),
    Field(
        "filesystem.allow_run",
        "filesystem_allow_run",
        "Allow run_command",
        "bool",
        "Filesystem",
        "Bind the run_command shell tool (runs arbitrary commands in a project dir as "
        "the server user). Off = the tool is never built — the model can't see or call "
        "it; the read/write file tools stay. The full kill switch for shell access.",
        depends_on={"key": "filesystem.enabled"},
    ),
    Field(
        "filesystem.run_requires_approval",
        "filesystem_run_requires_approval",
        "Require approval per command",
        "bool",
        "Filesystem",
        "Pause every run_command invocation for operator approval (HITL) before it "
        "executes. Turn off only inside a hardened container or for a trusted "
        "autonomous deploy.",
        depends_on={"key": "filesystem.allow_run"},
    ),
    Field(
        "filesystem.bypass_allowed",
        "filesystem_bypass_allowed",
        "Allow /bypass",
        "bool",
        "Filesystem",
        "Permit the per-tab /bypass chat toggle to skip the approval gate. Off = "
        "approvals are enforced regardless of any caller-supplied bypass flag.",
        depends_on={"key": "filesystem.run_requires_approval"},
    ),
    # ── Tools — the operator denylist over the assembled toolset ────────────────
    Field(
        "tools.disabled",
        "tools_disabled",
        "Disabled tools",
        "string_list",
        "Tools",
        "Tool names removed from this agent's toolset at graph build — one per line. "
        "Covers every contributor: core, plugin, MCP, filesystem (e.g. run_command), "
        "and delegation tools. Applies on save (the graph rebuilds).",
    ),
    # ── Middleware toggles ───────────────────────────────────────────────────
    Field("middleware.knowledge", "knowledge_middleware", "Knowledge middleware", "bool", "Middleware"),
    Field("middleware.memory", "memory_middleware", "Memory middleware", "bool", "Middleware"),
    Field("middleware.audit", "audit_middleware", "Audit middleware", "bool", "Middleware"),
    Field("middleware.scheduler", "scheduler_enabled", "Scheduler", "bool", "Middleware"),
    # Enforcement is a code/YAML fork seam (deny-list + rate-limits + a pluggable
    # predicate), not a console feature: the bare on/off toggle is a no-op until a
    # policy is configured in YAML, so it's ui_hidden (kept in FIELDS for config
    # round-trip). See docs/reference/configuration.md `## enforcement`.
    Field("middleware.enforcement", "enforcement_enabled", "Tool enforcement", "bool", "Middleware", ui_hidden=True),
    # ── Telemetry (local cost/latency store, ADR 0006) ───────────────────────
    Field(
        "telemetry.fleet_trace_export",
        "fleet_trace_export_enabled",
        "Fleet trace export",
        "bool",
        "Telemetry",
        "Write one per-turn trajectory row (OpenAI chat format) to <instance>/fleet-traces/ "
        "for the agent-fleet flywheel. Off by default; dumps stay on this machine until you "
        "ship them (scripts/setup_fleet_tracing.sh). Env PROTOAGENT_FLEET_TRACE_EXPORT overrides.",
        restart=True,
    ),
    Field(
        "telemetry.enabled",
        "telemetry_enabled",
        "Store telemetry locally",
        "bool",
        "Telemetry",
        "Persist a per-turn cost/latency row to a local SQLite DB (queryable in Settings → "
        "Telemetry). Off = nothing is recorded — no store is opened. Stays on your machine; "
        "it is never sent anywhere.",
        restart=True,
        scope="host",
    ),
    Field(
        "telemetry.retention_days",
        "telemetry_retention_days",
        "Telemetry retention (days)",
        "number",
        "Telemetry",
        "Auto-prune rows older than this (0 = keep forever).",
        minimum=0,
        restart=True,
        scope="host",
    ),
    # ── Media output store (#1929) ────────────────────────────────────────────
    Field(
        "media.public",
        "media_public",
        "Serve media without auth",
        "bool",
        "Media",
        "Expose /media/<file> (tool-generated images/audio/video) without any credential. "
        "Off (default) = each file is reachable only via its signed URL or a bearer — the "
        "default-deny gate holds. Turn on only when the whole store should be public.",
    ),
    Field(
        "media.retention_days",
        "media_retention_days",
        "Media retention (days)",
        "number",
        "Media",
        "Auto-prune generated media files older than this on each new save (0 = keep forever).",
        minimum=0,
    ),
    # ── Identity / operator ──────────────────────────────────────────────────
    # `identity.name` stays in FIELDS so it round-trips through the YAML writer AND so it
    # validates/cascades on save, but it's ui_hidden: the dedicated Identity panel (Agent ▸
    # Identity) is its single editor, alongside the persona (SOUL.md). Surfacing it here too
    # would make the same field editable from two places (#1076). The panel saves the name
    # through the canonical /api/settings cascade (a ui_hidden key still saves fine — only
    # build_schema rendering is gated); only SOUL goes via /api/config.
    Field("identity.name", "identity_name", "Agent name", "string", "Identity", ui_hidden=True),
    Field("identity.operator", "identity_operator", "Operator", "string", "Identity"),
    Field("identity.org", "identity_org", "Organization", "string", "Identity", scope="host"),
    Field(
        "operator.project_dir",
        "operator_project_dir",
        "Project directory",
        "string",
        "Identity",
        "Working directory for the console's tasks/notes (and the agent's "
        "default project). Always allowed. Blank = the protoAgent directory.",
    ),
    Field(
        "operator.allowed_dirs",
        "operator_allowed_dirs",
        "Allowed project dirs",
        "string_list",
        "Identity",
        "Extra directories the tasks/notes APIs may touch, beyond the project "
        "directory and protoAgent (which are always allowed).",
    ),
    Field(
        "auth.token",
        "auth_token",
        "A2A auth token",
        "secret",
        "Identity",
        "Bearer token for the A2A endpoint. Stored in secrets.yaml; applies live.",
    ),
    # A plugin's Settings group is declared by its manifest (ADR 0019) and rendered
    # via the plugin-fields path in build_schema — same for bundled or external
    # plugins; the generic Test button + guide link come from the manifest (ADR 0059).
    # ── Background jobs (ADR 0050/0070) ──────────────────────────────────────
    Field(
        "background.auto_resume",
        "background_auto_resume",
        "Push-resume on completion",
        "bool",
        "Background",
        "When a background job finishes, immediately run a turn in the session that "
        "spawned it so the agent reviews the report and briefs the operator (ADR 0070). "
        "Off = the report waits for that session's next manual turn.",
    ),
    # ── Runtime (restart) ────────────────────────────────────────────────────
    Field(
        "runtime.autostart_on_boot",
        "autostart_on_boot",
        "Autostart on boot",
        "bool",
        "Runtime",
        "Install/remove the boot LaunchAgent.",
        restart=True,
    ),
    # ── Host box-runtime knobs (Host layer, ADR 0047 D8) ─────────────────────
    # Box-wide runtime knobs promoted out of env/CLI into the Host cascade layer.
    # All scope="host": the box default every co-located agent inherits (file > env
    # > default; the matching PROTOAGENT_* env var stays the fallback). Consumed by
    # the host process — a workspace leaf override is a silent no-op (ADR §5).
    # Regrouped into Network / Discovery / Keep-warm sections (bd-2zb) so Host config
    # reads as coherent groups instead of one "Fleet" lump; all map to System below.
    Field(
        "network.bind",
        "bind_host",
        "Bind interface",
        "string",
        "Network",
        "Network interface the server listens on. 127.0.0.1 = loopback only (safe "
        "default); 0.0.0.0 = all interfaces (token-gate the A2A endpoint first). An "
        "explicit --host flag still wins. Env fallback: PROTOAGENT_HOST.",
        restart=True,
        scope="host",
    ),
    # Outbound counterpart to the inbound bind interface (ADR 0008). Host-scoped +
    # hot-reloaded (egress.set_allowed_hosts runs on save). string_list → the generic
    # one-per-line editor; no bespoke console code.
    Field(
        "egress.allowed_hosts",
        "egress_allowed_hosts",
        "Outbound host allowlist",
        "string_list",
        "Network",
        "Hosts the agent's fetch_url tool may reach — one per line; a leading `*.` matches "
        "subdomains (e.g. `*.github.com`). Empty = off: any public host is reachable, with a "
        "built-in SSRF guard still blocking private / loopback / cloud-metadata addresses. When "
        "set it's deny-by-default (only these hosts) — your configured model gateway (Model ▸ API "
        "base URL) is always permitted automatically, so you needn't list it. Also the source of "
        "truth for the OpenShell sandbox network policy.",
        scope="host",
    ),
    Field(
        "fleet.port_base",
        "fleet_port_base",
        "Workspace port base",
        "number",
        "Network",
        "Base TCP port for fleet workspace agents — each gets port_base+1, +2, … unless given an explicit port.",
        minimum=1,
        maximum=65535,
        restart=True,
        scope="host",
    ),
    Field(
        "fleet.discovery.port_min",
        "discovery_port_min",
        "Discovery scan: min port",
        "number",
        "Discovery",
        "Low end (inclusive) of the port window fleet discovery probes on the LAN / tailnet.",
        minimum=1,
        maximum=65535,
        scope="host",
    ),
    Field(
        "fleet.discovery.port_max",
        "discovery_port_max",
        "Discovery scan: max port",
        "number",
        "Discovery",
        "High end (inclusive) of the discovery port window.",
        minimum=1,
        maximum=65535,
        scope="host",
    ),
    Field(
        "fleet.discovery.mdns",
        "discovery_mdns",
        "mDNS discovery",
        "bool",
        "Discovery",
        "Advertise + browse the _protoagent._tcp mDNS/Bonjour channel so LAN siblings "
        "find each other automatically. Off by default (#1802) — an agent stays quiet on "
        "the network unless you opt in (privacy/security). On = this agent announces itself "
        "and discovers siblings on the LAN; tailnet + manual register work either way, and "
        "the local fleet console is unaffected (it enumerates from disk, not mDNS).",
        scope="host",
    ),
    Field(
        "fleet.warm.max",
        "fleet_max_warm",
        "Warm-agent cap",
        "number",
        "Keep-warm",
        "Max fleet agents kept running at once; the least-recently-active beyond this "
        "are spun down (LRU). 0 = unlimited. Env fallback: PROTOAGENT_FLEET_MAX_WARM.",
        minimum=0,
        scope="host",
    ),
    Field(
        "fleet.warm.grace_seconds",
        "fleet_warm_grace_seconds",
        "Warm eviction grace (s)",
        "number",
        "Keep-warm",
        "Spare an agent touched within this many seconds from LRU eviction (it may be "
        "mid-turn). 0 = pure LRU. Env fallback: PROTOAGENT_FLEET_WARM_GRACE.",
        minimum=0,
        scope="host",
    ),
    Field(
        "fleet.autostart",
        "fleet_autostart",
        "Autostart members",
        "string_list",
        "Keep-warm",
        "Fleet members (by id or display name) the hub (re)starts on boot — so a container "
        "recreate or host restart brings your declared crew back up automatically instead of "
        "leaving them down until re-activated by hand. One per line. Env fallback: "
        "PROTOAGENT_FLEET_AUTOSTART (comma-separated).",
        restart=True,
        scope="host",
    ),
    Field(
        "developer.channel",
        "developer_channel",
        "Developer channel",
        "select",
        "Developer",
        "Which pre-release features this instance exposes (ADR 0068). `prod` = released "
        "features only; `beta` = opt-in previews; `dev` = everything, incl. in-progress "
        "work. The dev sandbox instance defaults to `dev`. Env fallback: PROTOAGENT_CHANNEL.",
        options=["prod", "beta", "dev"],
    ),
    # ── Secrets manager (ADR 0080) — pull env vars from an external manager ──
    Field(
        "secrets_manager.enabled",
        "secrets_manager_enabled",
        "Pull secrets from a manager",
        "bool",
        "Secrets manager",
        "Fetch secrets from an external manager (Infisical) at boot, on config reload, and "
        "on the refresh interval, and export them as environment variables. Values fill the "
        "documented env fallback tier — an env var you set yourself still wins, and "
        "secrets.yaml still beats everything.",
    ),
    Field(
        "secrets_manager.provider",
        "secrets_manager_provider",
        "Provider",
        "select",
        "Secrets manager",
        options=["infisical"],
        depends_on={"key": "secrets_manager.enabled"},
    ),
    Field(
        "secrets_manager.host",
        "secrets_manager_host",
        "Server URL",
        "string",
        "Secrets manager",
        "Infisical Cloud (https://us.infisical.com / https://eu.infisical.com) or your "
        "self-hosted instance URL.",
        depends_on={"key": "secrets_manager.enabled"},
    ),
    Field(
        "secrets_manager.project_id",
        "secrets_manager_project_id",
        "Project ID",
        "string",
        "Secrets manager",
        depends_on={"key": "secrets_manager.enabled"},
    ),
    Field(
        "secrets_manager.environment",
        "secrets_manager_environment",
        "Environment",
        "string",
        "Secrets manager",
        "Environment slug to pull from (e.g. dev / staging / prod). The dev sandbox "
        "instance can point at a different environment than your default instance.",
        depends_on={"key": "secrets_manager.enabled"},
    ),
    Field(
        "secrets_manager.path",
        "secrets_manager_path",
        "Secret path",
        "string",
        "Secrets manager",
        "Folder to pull (subfolders included when recursive is on).",
        depends_on={"key": "secrets_manager.enabled"},
    ),
    Field(
        "secrets_manager.recursive",
        "secrets_manager_recursive",
        "Include subfolders",
        "bool",
        "Secrets manager",
        depends_on={"key": "secrets_manager.enabled"},
    ),
    Field(
        "secrets_manager.client_id",
        "secrets_manager_client_id",
        "Machine identity client ID",
        "secret",
        "Secrets manager",
        "Universal-auth machine identity. Stored in secrets.yaml, never echoed back — or "
        "set INFISICAL_CLIENT_ID in the environment instead.",
        depends_on={"key": "secrets_manager.enabled"},
    ),
    Field(
        "secrets_manager.client_secret",
        "secrets_manager_client_secret",
        "Machine identity client secret",
        "secret",
        "Secrets manager",
        "Stored in secrets.yaml, never echoed back — or set INFISICAL_CLIENT_SECRET in the "
        "environment instead. This bootstrap pair is the only secret that stays local; "
        "everything else can live in the manager.",
        depends_on={"key": "secrets_manager.enabled"},
    ),
    Field(
        "secrets_manager.refresh_seconds",
        "secrets_manager_refresh_seconds",
        "Refresh interval (seconds)",
        "number",
        "Secrets manager",
        "Re-pull on this interval so rotation lands without a restart. 0 = fetch only at "
        "boot and on config reload.",
        minimum=0,
        depends_on={"key": "secrets_manager.enabled"},
    ),
    Field(
        "secrets_manager.required",
        "secrets_manager_required",
        "Required at boot",
        "bool",
        "Secrets manager",
        "Refuse to boot when the manager is unreachable, instead of the default "
        "warn-and-continue with whatever the environment already has.",
        depends_on={"key": "secrets_manager.enabled"},
    ),
    Field(
        "secrets_manager.override_env",
        "secrets_manager_override_env",
        "Manager beats existing env",
        "bool",
        "Secrets manager",
        "By default a pre-existing environment variable shadows the manager's value. "
        "Turn this on to prefer the manager (rotation-wins).",
        depends_on={"key": "secrets_manager.enabled"},
    ),
    Field(
        "secrets_manager.timeout_seconds",
        "secrets_manager_timeout_seconds",
        "Fetch timeout (seconds)",
        "number",
        "Secrets manager",
        minimum=1,
        depends_on={"key": "secrets_manager.enabled"},
    ),
]

# Knowledge domain sub-sections (console grouping). The Knowledge fields are declared with
# section "Knowledge" above for locality; here we split that one 22-field wall into three
# scannable accordion groups — Recall (retrieval), Ingestion (import/chunking), History
# (checkpoints) — all still under the Knowledge domain (see _SECTION_CATEGORY). One map, so
# the split is reviewable in one place instead of scattered across 22 field definitions.
_KNOWLEDGE_SUBSECTION = {
    # Recall — what the agent retrieves into context, and how.
    "knowledge.top_k": "Recall",
    "knowledge.inject_namespaces": "Recall",
    "knowledge.inject_min_trust": "Recall",
    "knowledge.hot_write_confirm": "Recall",
    "knowledge.scope": "Recall",
    "knowledge.embeddings": "Recall",
    "knowledge.embed_model": "Recall",
    "knowledge.recall_preview_chars": "Recall",
    "knowledge.vector_k": "Recall",
    "knowledge.rrf_k": "Recall",
    "knowledge.min_score": "Recall",
    "skills.top_k": "Recall",  # skills surfaced into context — a recall-count sibling
    # Ingestion — bringing documents in (extraction, chunking, enrichment).
    "knowledge.transcribe_model": "Ingestion",
    "knowledge.image_describe_model": "Ingestion",
    "knowledge.chunk_max_chars": "Ingestion",
    "knowledge.chunk_overlap_chars": "Ingestion",
    "knowledge.contextual_enrichment": "Ingestion",
    "knowledge.attach_inline_budget": "Ingestion",
    "knowledge.facts": "Ingestion",
    # History — conversation checkpoints + retention/harvest.
    "checkpoint.db_path": "History",
    "checkpoint.keep_per_thread": "History",
    "checkpoint.max_age_days": "History",
    "checkpoint.prune_interval_hours": "History",
    "checkpoint.harvest_enabled": "History",
    "checkpoint.vacuum": "History",
}
for _f in FIELDS:
    if _f.key in _KNOWLEDGE_SUBSECTION:
        _f.section = _KNOWLEDGE_SUBSECTION[_f.key]

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


# Settings categories — the DOMAIN each flat section routes to (ADR 0048, ratified
# 2026-06-28). The category IS the domain, so the data model and the console sidenav
# speak the same axis (the earlier scope-vs-category disagreement is gone). Scope
# (host vs agent) is a per-field badge (ADR 0047), NOT a category. Order here is the
# domain order the console renders. Unknown sections (notably plugin-contributed ones,
# ADR 0019) default to "Plugins" (the Integrations surface).
_CATEGORY_ORDER = ["Identity", "Model", "Behavior", "Capabilities", "Knowledge", "Secrets", "Plugins", "Box"]
_SECTION_CATEGORY = {
    # Identity — who the agent is (name + persona live in the dedicated Identity panel;
    # these are the operator/org/access fields rendered beneath it).
    "Identity": "Identity",
    # Model — the LLM connection, sampling, and cache (the real "Model & Routing").
    "Model": "Model",
    "Favorite models": "Model",  # /model quick-switch pins (#1957)
    "Routing": "Model",
    "Caching": "Model",
    # Behavior — how the agent thinks, loops, and decides.
    "Agent runtime": "Behavior",
    "Goal mode": "Behavior",
    "Compaction": "Behavior",
    "Middleware": "Behavior",
    "Background": "Behavior",
    "Runtime": "Behavior",
    # Capabilities — the sharing/tier knobs for what the agent is wired to (the rich
    # Tools/MCP/Skills/Subagents/Delegates managers are bespoke console panels).
    "Skills": "Capabilities",
    "MCP": "Capabilities",
    "Filesystem": "Capabilities",
    "Tools": "Capabilities",
    # Knowledge — recall / RAG config, split into sub-sections (see _KNOWLEDGE_SUBSECTION).
    "Recall": "Knowledge",
    "Ingestion": "Knowledge",
    "History": "Knowledge",
    # Secrets — the external secrets manager (ADR 0080); the console renders this
    # category as its own sidenav section with a status/test/sync card.
    "Secrets manager": "Secrets",
    # Box — box-wide operational config (host console only): the telemetry store + the
    # host box-runtime knobs (network / discovery / keep-warm, ADR 0047 D8). Host-scoped;
    # a workspace-leaf override of these is a silent no-op (consumed by the host process).
    "Telemetry": "Box",
    "Network": "Box",
    "Discovery": "Box",
    "Keep-warm": "Box",
    # Developer — pre-release feature gating (ADR 0068). The channel this instance runs on;
    # the flags themselves live in a device-local Developer panel, not the schema.
    "Developer": "Behavior",
    # Persona — the SOUL/self-edit toggle (ADR 0081). Belongs with Identity (name + persona).
    # soul.self_edit_enabled is ui_hidden today, so this doesn't render yet — but mapping it
    # explicitly keeps it OUT of the Plugins default (it's core, not a plugin section).
    "Persona": "Identity",
    # Media — the core media store (registry.save_media, #1929): tool-generated image/audio/
    # video exposure + retention. INTERIM: mapped to Plugins so it stays reachable, because
    # Capabilities has no schema panel yet. It moves to Capabilities under the settings-IA
    # rework's Decision A (docs/dev/settings-ia-target.md) once that panel lands. Explicit so
    # a CORE section is never silently swept into Integrations by the default below (D5).
    "Media": "Plugins",
    # Discord / Google / GitHub / other PLUGIN-contributed sections → "Plugins" (the default),
    # the Integrations surface. Only genuinely plugin sections may rely on the default —
    # every CORE section (declared by a FIELDS entry) must be mapped explicitly above; a test
    # enforces this so a new core section can't strand itself under Integrations by accident.
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
    # ACP options are config-aware (ADR 0033): built-ins + any user-registered
    # `acp.agents.<id>`, so a custom coding agent shows in the runtime + aux-model
    # dropdowns. Empty config ⇒ exactly ACP_MODEL_OPTIONS (the built-in list).
    acp_opts = acp_runtime_options(getattr(config, "acp_agents", None))
    groups: dict[str, dict[str, Any]] = {}
    for f in FIELDS:
        if f.ui_hidden:
            continue  # in FIELDS for config round-trip, but a dedicated panel owns the UI (#1076)
        current = getattr(config, f.attr, None)
        entry: dict[str, Any] = {
            "key": f.key,
            "label": f.label,
            "type": f.type,
            "section": f.section,
            "description": f.description,
            "restart": f.restart,
            "options": (
                (model_options or [])
                if f.options_source == "models"
                else (model_options or []) + acp_opts
                if f.options_source == "models+acp"
                else ["native", *acp_opts]
                if f.options_source == "runtime"
                else list(f.options)
            ),
            "default": _jsonable(getattr(defaults, f.attr, None)),
            "scope": f.scope,  # ADR 0047: "agent" | "host"
            "source": _source_for(f.key, agent_doc, host_doc),  # which layer set the live value
            # Lets the console refresh a model-backed dropdown from a DIFFERENT gateway than the
            # saved one (#1386): a "Get models" action probes the form's api_base/key and merges
            # the result into every field whose options come from "models".
            "options_source": f.options_source,
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
        if f.depends_on:
            entry["depends_on"] = f.depends_on  # #963 — full dotted sibling key
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
        # #963 — a plugin spec uses the SHORT sibling key (e.g. depends_on.key
        # "ask_enabled"); resolve it to the full dotted path the UI sees so the
        # console can match it against the rendered sibling field.
        dep = spec.get("depends_on")
        if isinstance(dep, dict) and dep.get("key"):
            dk = str(dep["key"])
            if not dk.startswith(f"{sch.section}."):
                dk = f"{sch.section}.{dk}"
            entry["depends_on"] = {**dep, "key": dk}
        # `plugin_id` tags the group with its owning plugin so the console can fold
        # the config into that plugin's row in the Plugins surface (ADR 0059, bd-23a.3).
        groups.setdefault(group, {"section": group, "fields": [], "plugin_id": getattr(sch, "plugin_id", None)})["fields"].append(entry)
        # A plugin that declares `test: true` (ADR 0029) gets a generic console
        # "Test connection" button posting the group's fields to its test route.
        if getattr(sch, "test", False):
            groups[group]["test"] = {"endpoint": f"/api/config/test-{sch.section}"}
        # Optional setup-guide link (ADR 0059) — rendered generically next to the
        # group, so a plugin needs no bespoke console frontend.
        if getattr(sch, "guide_url", ""):
            groups[group]["guide_url"] = sch.guide_url

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
