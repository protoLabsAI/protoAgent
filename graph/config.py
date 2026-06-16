"""LangGraph configuration loader for protoAgent.

Loads from ``config/langgraph-config.yaml`` when present, falls back
to hardcoded defaults otherwise. Fork this file to add agent-specific
config surface (extra subagents, domain flags, custom knowledge
store paths, etc.).

The defaults here point at the protoLabs LiteLLM gateway via the
``protolabs/<agent>`` alias pattern — retarget ``model.name`` in the
YAML (or swap the gateway alias) per agent without code changes.
"""

import copy
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("protoagent.config")

# The built-in researcher's runtime defaults are the single source of truth; the
# config-side SubagentDef mirrors them so the YAML override layer can't drift
# (graph/subagents/config has no graph.config dep — no import cycle).
from graph.subagents.config import RESEARCHER_CONFIG

# Secrets (model API key, A2A bearer) live in an untracked ``secrets.yaml``
# sibling of the main config, never in the tracked YAML. See graph/config_io
# for the write side. ``from_yaml`` overlays them below; both still fall back
# to env (OPENAI_API_KEY / A2A_AUTH_TOKEN) when the file is absent, so
# infisical/env-injected deployments are unaffected.
SECRETS_FILENAME = "secrets.yaml"


def _load_secrets_doc(config_dir: Path) -> dict:
    """Load the untracked secrets overlay sitting next to the config YAML."""
    secrets_path = config_dir / SECRETS_FILENAME
    if not secrets_path.exists():
        return {}
    try:
        with open(secrets_path) as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _resolve_plugin_config(data: dict, secrets: dict, config_dir: Path) -> dict:
    """Resolve each enabled plugin's declared config section (ADR 0019).

    For every plugin that claims a top-level section, merge: manifest defaults ⊕
    the (secret-stripped) YAML section ⊕ the secrets overlay for its secret keys.
    Best-effort — never breaks config load. Returns ``{section: resolved_dict}``.
    """
    try:
        from graph.plugins.pconfig import discover_plugin_config, plugin_roots_from

        plugins = data.get("plugins") or {}
        roots = plugin_roots_from(config_dir, str(plugins.get("dir") or ""))
        schemas = discover_plugin_config(
            roots, set(plugins.get("enabled") or []), set(plugins.get("disabled") or []),
        )
    except Exception as e:  # noqa: BLE001 — plugin config is best-effort, but say so
        log.warning(
            "[plugins] config resolution failed — plugin config unavailable this load "
            "(plugins behave as if unconfigured): %s",
            e,
        )
        return {}

    out: dict = {}
    for sch in schemas:
        section_yaml = data.get(sch.section) or {}
        sec_overlay = secrets.get(sch.section) or {}
        resolved = dict(sch.defaults)
        resolved.update({k: v for k, v in section_yaml.items() if k not in sch.secrets})
        for k in sch.secrets:
            v = sec_overlay.get(k)
            if v is None:
                v = section_yaml.get(k)  # belt-and-suspenders if not yet stripped
            resolved[k] = v if v is not None else resolved.get(k, "")
        out[sch.section] = resolved
    return out


# ── App→Host→Agent settings cascade (ADR 0047) ───────────────────────────────

def _deep_merge_dicts(base: dict, overlay: dict) -> dict:
    """Deep-merge ``overlay`` onto ``base`` in place — overlay wins on leaf
    conflicts; nested dicts merge recursively; lists REPLACE (no union)."""
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge_dicts(base[k], v)
        else:
            base[k] = v
    return base


def _get_dotted(d: dict, dotted: str):
    """Walk ``dotted`` (``"prompt_cache.warm.enabled"``) through ``d`` →
    ``(found, value)``; ``found`` is False at the first missing/non-dict segment."""
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _set_dotted(out: dict, dotted: str, value) -> None:
    cur = out
    parts = dotted.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _env_default(name: str, default, cast=str):
    """Env-fallback for a promoted host knob (ADR 0047 D8). Returns the env var's
    value (cast) when set+non-empty, else ``default``. Used as the ``.get(key, …)``
    fallback in ``from_dict`` so resolution is **file > env > app-default**: env is
    consulted only when the merged (host⊕leaf) dict omits the key, keeping
    promotion of an env-configured box zero-migration."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def _filter_to_host_keys(raw: dict) -> dict:
    """Keep only the host-scoped FIELDS keys present in a raw host-config doc.

    The Host file can set box-shared defaults but **cannot inject agent-only
    settings** (ADR 0047 D1/D4) — anything outside the ``scope=="host"`` set is
    dropped here before the merge."""
    from graph.settings_schema import FIELDS

    out: dict = {}
    for f in FIELDS:
        if getattr(f, "scope", "agent") != "host":
            continue
        found, val = _get_dotted(raw, f.key)
        if found:
            _set_dotted(out, f.key, val)
    return out


def _load_host_layer() -> dict:
    """The Host layer (ADR 0047): ``host-config.yaml`` filtered to host-scoped keys.

    Returns ``{}`` when the file is absent, unreadable, or malformed — the cascade
    then collapses to App defaults + the agent leaf. Best-effort: a corrupt host
    file must never crash boot."""
    try:
        from infra.paths import host_config_path

        hp = host_config_path()
    except Exception:  # noqa: BLE001 — never let host-path resolution break config load
        return {}
    if not hp.exists():
        return {}
    try:
        with open(hp) as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("host-config.yaml at %s is unreadable (%s); ignoring the Host layer", hp, exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("host-config.yaml at %s is not a mapping; ignoring the Host layer", hp)
        return {}
    return _filter_to_host_keys(raw)


@dataclass
class SubagentDef:
    enabled: bool = True
    tools: list[str] = field(default_factory=list)
    max_turns: int = 30
    # Per-subagent model override (ADR 0001) — blank = routing.aux_model → main model.
    # Applied onto the registry's SubagentConfig at build (see _apply_config_subagents).
    model: str = ""


@dataclass
class LangGraphConfig:
    # Model settings — route through the LiteLLM gateway by default
    model_provider: str = "openai"
    model_name: str = "protolabs/reasoning"  # override in YAML per agent
    api_base: str = "http://gateway:4000/v1"
    api_key: str = ""  # set via OPENAI_API_KEY env (gateway master key)
    temperature: float = 0.2
    max_tokens: int = 32768  # 32k — required headroom for the Qwen models we run
    max_iterations: int = 50
    # Native vision (ADR 0021): set true when `model_name` is image-capable (e.g.
    # protolabs/fast, protolabs/smart). The chat composer then sends attached
    # images as native multimodal parts straight to the model instead of routing
    # them through the extraction pipeline. Off → images go through the pipeline.
    model_vision: bool = False

    # Per-call timeout (seconds) on the model client + transient-retry cap. Bounds
    # a hung/slow gateway so a turn surfaces a clean error instead of blocking the
    # A2A task / SSE stream indefinitely (prod-readiness). 0/None ⇒ SDK default.
    request_timeout: float = 120.0
    llm_max_retries: int = 2

    # Advanced sampling — all opt-in. ``None`` (or a negative top_k) means
    # "let the gateway / model card decide". top_p and presence_penalty are
    # standard OpenAI params; top_k and repetition_penalty aren't, so they
    # ride ``extra_body`` for vLLM-compatible gateways. ``chat_template_kwargs``
    # also rides extra_body — e.g. vLLM's ``preserve_thinking=True`` to keep
    # historical <think>/<scratch_pad> blocks across turns.
    top_p: float | None = None
    top_k: int = -1
    presence_penalty: float | None = None
    repetition_penalty: float | None = None
    chat_template_kwargs: dict | None = None

    # Subagents — template ships one example, `researcher` (see
    # graph/subagents/config.py). Add fields here as you add entries to
    # SUBAGENT_REGISTRY. Tool/max_turns here mirror the registry default and
    # are the YAML-overridable layer.
    # Defaults derived from the registry entry (SSOT) so the YAML-overridable layer
    # always matches the runtime default — an un-overridden config is a true no-op.
    researcher: SubagentDef = field(default_factory=lambda: SubagentDef(
        tools=list(RESEARCHER_CONFIG.tools),
        max_turns=RESEARCHER_CONFIG.max_turns,
        model=RESEARCHER_CONFIG.model,
    ))

    # Sub-agent fan-out — the `task_batch` tool runs delegations concurrently.
    # ``subagent_max_concurrency`` caps in-flight subagents (protects the
    # gateway / context budget); ``subagent_output_truncate`` bounds each
    # subagent's returned text (chars) so a fan-out can't blow the parent
    # context. Both apply to `task_batch`; single `task` is unbounded.
    subagent_max_concurrency: int = 4
    subagent_output_truncate: int = 6000

    # Middleware / subsystem toggles. All default-on so a fresh fork has
    # a working memory loop + scheduler on day one. Forks that want a
    # purely stateless agent (no KB, no scheduled tasks) can flip these
    # via the drawer or by editing the YAML directly.
    knowledge_middleware: bool = True
    audit_middleware: bool = True
    memory_middleware: bool = True
    scheduler_enabled: bool = True

    # The Discord surface (ADR 0015/0016) is now the first-party `discord` plugin
    # (ADR 0018/0019, plugins/discord/) — its config lives in plugin_config["discord"],
    # not a typed field here.

    # The Google surface (ADR 0017) is now the first-party `google` plugin (ADR
    # 0019, plugins/google/) — a managed MCP server it injects via
    # register_mcp_server. Its config lives in plugin_config["google"], not typed
    # fields here.

    # Enforcement gate — opt-in safety middleware that blocks tool calls
    # before they execute (deny list + per-tool rate limits). Off by default;
    # forks enable it and supply a deny list / rate limits (and can attach a
    # custom predicate in code). See graph/middleware/enforcement.py.
    enforcement_enabled: bool = False
    enforcement_disallowed_tools: list[str] = field(default_factory=list)
    enforcement_rate_limits: dict = field(default_factory=dict)

    # Knowledge-ingest gate — opt-in middleware that captures tool output into
    # the KB after execution. Off by default; ``ingest_tools`` (empty = all)
    # narrows which tools are captured. Forks attach a structured extractor in
    # code. See graph/middleware/knowledge_ingest.py.
    ingest_enabled: bool = False
    ingest_tools: list[str] = field(default_factory=list)

    # Prompt caching — Anthropic prefix caching on the stable system prompt.
    # Safe no-op on non-Anthropic models (gated on model name unless forced).
    # NOTE: this middleware also DELIVERS KnowledgeMiddleware's context to the
    # model (create_agent doesn't read the `context` state key), so it's wired
    # unconditionally; the flags below only control the caching half.
    prompt_cache_enabled: bool = True
    prompt_cache_ttl: str = "5m"          # "5m" (ephemeral) or "1h" (persistent)
    prompt_cache_force: bool = False      # bypass the Anthropic-name heuristic

    # Cache-warming heartbeat — optional background ping that reproduces the
    # agent's cached system+tools prefix on an interval so the FIRST real
    # request after an idle gap hits a warm cache instead of a full miss.
    # OFF by default; only worth enabling for sporadic-but-latency-sensitive
    # workloads on the "1h" persistent tier (interval just under the TTL).
    # For steady traffic the cache stays warm on its own and this is pure cost.
    cache_warming_enabled: bool = False
    cache_warming_interval_seconds: int = 3300  # 55m — just under the 1h tier

    # Context compaction — wires langchain's SummarizationMiddleware to
    # summarize old history near the context limit. ON by default (a long
    # session would otherwise overflow the window). trigger is
    # "fraction:0.8" | "tokens:120000" | "messages:80"; keep = last N messages.
    # NOTE: "fraction:"/"tokens:" triggers need the model's context-window
    # profile; for a custom gateway alias that lacks one, the wiring falls back
    # to a message-count trigger (see graph/agent.py) instead of crashing.
    compaction_enabled: bool = True
    compaction_trigger: str = "fraction:0.8"
    compaction_keep_messages: int = 20
    compaction_model: str = ""            # blank = summarize with the main model

    # Programmatic tool calling — the `execute_code` tool. Lets the model write
    # one Python script that calls several tools, loops/filters/composes their
    # results, and returns only stdout — collapsing a long tool-call chain into
    # a single turn. The script runs in a subprocess with a scrubbed env (no
    # secrets) and a hard timeout; tools are invoked back in the parent over an
    # fd-based RPC bridge. OFF by default (run only trusted-model output, or in
    # a hardened container). ``execute_code_tools`` empty = expose all tools
    # except execute_code itself.
    execute_code_enabled: bool = False
    execute_code_timeout: float = 30.0
    execute_code_tools: list[str] = field(default_factory=list)
    execute_code_output_truncate: int = 6000

    # Deferred tools (ADR 0005 #3) — progressive tool disclosure for high tool
    # counts. When enabled, only a small base set + a ``search_tools`` meta-tool
    # are exposed to the model each turn; the rest are bound (callable) but their
    # schemas are withheld until the agent searches for and "loads" them. Cuts
    # the per-turn tool-schema footprint and improves selection accuracy past
    # ~15 tools. OFF by default — the full tool set is exposed (unchanged).
    # ``tools_deferred_keep`` overrides the always-on base (empty → built-in
    # base: keyless core + delegation/workflow tools + search_tools).
    tools_deferred_enabled: bool = False
    tools_deferred_keep: list[str] = field(default_factory=list)

    # Tool denylist — drop named core tools from the agent without editing
    # ``tools/lg_tools.py::get_all_tools``. A fork keeps what it wants by listing
    # the rest here (config ``tools.disabled``); plugins still ADD tools. So
    # "keep what you want, drop the rest, add your own" is fully config + plugin
    # driven — no core edit that conflicts on upstream re-sync.
    tools_disabled: list[str] = field(default_factory=list)

    # Model routing / failover — wires langchain's ModelFallbackMiddleware.
    # On primary error, retry on each fallback model (same gateway) in order.
    routing_fallback_models: list[str] = field(default_factory=list)

    # Auxiliary model — a single cheap/fast alias for the non-reasoning calls
    # (context summarization, goal verification, subagent delegation). Each of
    # those paths uses its own specific override if set, else falls back to
    # this, else the main model. Blank = everything on the main model.
    aux_model: str = ""

    # Goal mode — testable-outcome goals the agent self-drives toward. The
    # machinery is available when enabled, but no goal is active until one is
    # set via `/goal` (a control message) or the /goal HTTP endpoints. After
    # each terminal turn the goal's verifier (command/test/ci/data/llm) decides
    # completion; on "not met" the agent is re-invoked with a continuation
    # prompt until met, the iteration budget runs out (exhausted), or it's
    # flagged unachievable (no-progress streak, or the model gives up). See
    # graph/goals/ and docs/guides/goal-mode.
    goal_enabled: bool = True
    goal_max_iterations: int = 8          # continuation budget per goal
    goal_no_progress_limit: int = 3       # identical verifier evidence N times -> unachievable
    goal_monitor_interval: int = 60       # seconds between out-of-band monitor-goal checks (ADR 0030)
    goal_eval_model: str = ""             # blank = main model (llm verifier / fuzzy goals)
    goal_verify_timeout: float = 120.0    # seconds for command/test/ci verifiers

    # Knowledge store — sqlite + FTS5, see ``knowledge/store.py``.
    # The default path lives under ``/sandbox/`` to play well with the
    # bundled Docker volume; the store falls back to
    # ``~/.protoagent/knowledge/agent.db`` automatically when /sandbox
    # is read-only or absent (e.g. local ``python -m server``).
    knowledge_db_path: str = "/sandbox/knowledge/agent.db"
    # Knowledge backend selector (ADR 0031) — "" = the built-in SQLite/FTS5 store;
    # otherwise the name of a plugin-registered backend (register_knowledge_store).
    # An unregistered name / a factory error degrades to the built-in store.
    knowledge_backend: str = ""
    # In-process embedder selector (ADR 0031 follow-up) — "" = the gateway embedder
    # (create_embed_fn); otherwise a plugin-registered embedder (register_embedder),
    # used by the built-in hybrid store. Unregistered/error → gateway embedder.
    knowledge_embedder: str = ""
    # The gateway's embedding model (NOT the chat model). Default is what the
    # protoLabs gateway serves; forks on a different gateway set this to a model
    # their gateway has (check GET /v1/models). A wrong/absent model degrades to
    # keyword search via the store's circuit breaker — never KB-less.
    embed_model: str = "qwen3-embedding"
    # Speech-to-text model for audio/video ingestion (ADR 0021) — the gateway's
    # OpenAI-compatible /audio/transcriptions alias (e.g. whisper-1). Audio is sent
    # as-is; video has its audio track pulled by ffmpeg first. Blank disables
    # audio/video ingestion (they error with a clear message).
    transcribe_model: str = "whisper-1"
    # Semantic recall (ADR 0021): when True, the knowledge store is the
    # HybridKnowledgeStore (FTS5 + vector embeddings via `embed_model`, fused
    # with RRF). On by default — semantic recall finds paraphrases keyword search
    # misses; the circuit breaker falls back to FTS5 on an embedding outage.
    knowledge_embeddings: bool = True
    # How many recalled chunks are injected per turn. Bumped 5 → 10 (RAG bake-off:
    # more candidates in-context lifted answer quality at sub-million-chunk scale).
    knowledge_top_k: int = 10
    # Hybrid-retrieval tuning (HybridKnowledgeStore knobs, ADR 0021) — surfaced so
    # they're tunable without editing the store / via the retrieval eval:
    #   vector_k  — FTS5 + vector candidates fused per query (the RRF pool).
    #   rrf_k     — Reciprocal-Rank-Fusion constant (higher = flatter weighting).
    #   min_score — drop fused hits below this score; 0 = keep all (a relevance
    #               floor for off-topic turns; RRF scores aren't normalized, so
    #               tune empirically).
    #   recall_preview_chars — how much of each hit the model sees (was 240).
    #   embed_breaker_* — the embed circuit breaker (consecutive failures to open
    #               it; seconds it stays open before retrying the gateway).
    knowledge_vector_k: int = 20
    knowledge_rrf_k: int = 60
    knowledge_min_score: float = 0.0
    knowledge_recall_preview_chars: int = 1000
    knowledge_embed_breaker_threshold: int = 2
    knowledge_embed_breaker_cooldown_s: float = 300.0
    # Document chunking on ingest (ADR 0021) — large bodies (conversation
    # summaries, pasted docs) are split before embedding so each passage gets
    # its own vector instead of one diluted whole-doc embedding. Applied by
    # add_document (harvest + operator paste); facts/notes stay atomic.
    #   chunk_max_chars     — target ceiling per chunk (content ≤ this isn't split).
    #   chunk_overlap_chars — shared tail between adjacent chunks (boundary safety).
    #   chunk_min_chars     — a trailing fragment below this folds into the prior chunk.
    knowledge_chunk_max_chars: int = 1200
    knowledge_chunk_overlap_chars: int = 150
    knowledge_chunk_min_chars: int = 200
    # Contextual Retrieval (ADR 0021, Anthropic) — when a doc splits, prepend a
    # one-line aux-LLM context situating each chunk in the whole document before
    # embedding/indexing, so the chunk carries doc-level context (lifts both
    # semantic + keyword recall). Costs one aux call per chunk at INGEST (not on
    # the query path), so OFF by default. context_max_doc_chars caps the document
    # text sent in the context prompt.
    knowledge_contextual_enrichment: bool = False
    knowledge_context_max_doc_chars: int = 12000
    # Chat attachments (ADR 0021) — a file dropped in chat is extracted, then TIERED
    # so a big doc never gets dumped into the turn: text at or under this many chars
    # is inlined whole; a larger doc is ingested (chunked/embedded, session-scoped)
    # for retrieval and only a lede of this many chars is inlined as an anchor.
    knowledge_attach_inline_budget: int = 8000

    # Conversation checkpointer — persists each chat session's history per
    # thread_id so multi-turn chats survive a server restart. A path → durable
    # SQLite (same /sandbox→~/.protoagent writable fallback as the stores);
    # blank → in-memory (history cleared on restart). Bound at graph-compile
    # time (see graph/checkpointer.py); changing the path needs a restart.
    checkpoint_db_path: str = "/sandbox/checkpoints.db"
    # Local telemetry store (ADR 0006 Slice 2) — one per-turn cost/latency row
    # per terminal A2A turn, queryable via /api/telemetry/*. ON by default
    # (cheap, one write per turn); path follows /sandbox→~/.protoagent fallback
    # and is instance-scoped (ADR 0004).
    telemetry_enabled: bool = True
    telemetry_db_path: str = "/sandbox/telemetry.db"
    # Retention guardrail (ADR 0006) — turns older than this are pruned by the
    # periodic maintenance loop so the store can't grow unbounded. 0 = keep forever.
    telemetry_retention_days: int = 90
    # Inbox/Activity retention — delivered inbox items and activity log entries
    # are pruned by the same maintenance loop. 0 = keep forever.
    inbox_retention_days: int = 90
    activity_retention_days: int = 90
    # Checkpoint pruning — keeps the SQLite DB from growing unbounded. Keep the
    # latest N checkpoints per session, and TTL whole sessions idle past
    # max_age_days. Runs every prune_interval_hours (0 disables the sweep).
    checkpoint_keep_per_thread: int = 5
    checkpoint_max_age_days: int = 30
    checkpoint_prune_interval_hours: int = 6
    # When a session is retired (aged out or deleted), summarize it into the
    # knowledge base before dropping the raw checkpoints — so past conversations
    # stay searchable via memory_recall. Needs the knowledge store enabled.
    checkpoint_harvest_enabled: bool = True
    # Semantic facts (ADR 0021): on retirement, also extract durable facts from
    # the conversation (aux model) and consolidate them into the store as
    # finding_type="fact". Rides the harvest pass; needs harvest enabled.
    knowledge_facts: bool = True

    # Skills — human-authored ``SKILL.md`` folders (AgentSkills open standard)
    # loaded from disk into the FTS5 skill index and retrieved at inference by
    # KnowledgeMiddleware. ``db_path`` follows the same /sandbox→~/.protoagent
    # writable fallback as the knowledge store (resolved in server.py).
    # ``dir`` optionally overrides the writable skills root (default:
    # ``<config_dir>/skills``); shipped example skills live in ``config/skills``.
    skills_enabled: bool = True
    skills_db_path: str = "/sandbox/skills.db"
    skills_top_k: int = 5
    skills_dir: str = ""
    # Tiered stores (ADR 0041) — `shared` lifts a store out of per-instance scoping
    # into the COMMONS (read by every agent on the host); `scoped` keeps it private.
    # Slice 1: skills can be shared (a fleet's compounding skill library). Default
    # False = legacy per-instance behavior (no surprise migration).
    skills_shared: bool = False
    # Tier (ADR 0041 slice 3): "scoped" (private), "shared" (one commons for all), or
    # "layered" (read commons ∪ private, write private, promote to commons). Blank →
    # derived from skills_shared (back-compat): shared→"shared", else "scoped".
    skills_scope: str = ""
    commons_path: str = ""  # commons base dir; blank → ~/.protoagent/commons

    # Workflows — declarative multi-step subagent recipes (see ADR 0002),
    # exposed via the run_workflow tool. Bundled examples ship in the repo
    # ``dir`` is the writable root for user/agent-emitted recipes (same
    # /sandbox→~/.protoagent fallback, resolved in server.py). Read by the
    # workflows plugin (lean core — the engine/tools live there now).
    workflow_dir: str = "/sandbox/workflows"

    # MCP — Model Context Protocol client. Connect to external MCP servers
    # (stdio or streamable-HTTP); their tools become agent tools, namespaced
    # ``<server>__<tool>`` so they can't shadow core tools. OFF by default —
    # configuring a server is the opt-in. ``servers`` entries are
    # ``{name, transport, command/args/env | url/headers}`` plus two optional
    # context-control keys: ``enabled: false`` skips connecting that server
    # entirely (lazy), and ``tools: {include: [...], exclude: [...]}`` filters
    # which of its tools are bound — ``include`` is an allowlist (only those
    # survive), the surgical defense against a large catalog flooding context.
    # ``denylist`` is a cross-server hard block. See tools/mcp_tools.py.
    mcp_enabled: bool = False
    mcp_servers: list[dict] = field(default_factory=list)
    mcp_timeout_seconds: float = 20.0
    mcp_denylist: list[str] = field(default_factory=list)

    # Operator MCP server (ADR 0033 slice 1) — expose THIS agent's tools as an MCP
    # server so any MCP client (Claude Desktop, Cursor) or an ACP coding-agent runtime
    # can operate the instance. Opt-in + allowlist-gated: only the named tools are
    # exposed (empty = none). Served standalone via ``python -m server.operator_mcp``.
    operator_mcp_enabled: bool = False
    operator_mcp_tools: list[str] = field(default_factory=list)

    # Agent runtime (ADR 0033) — which brain executes a turn. "native" = the built-in
    # LangGraph loop (default). "acp:<agent>" (e.g. "acp:codex", "acp:claude") = an
    # external coding agent drives the turn over ACP, mounting the operator MCP bus.
    agent_runtime: str = "native"

    # Plugins — drop-in packages (manifest + register()) that contribute tools
    # and bundled skills. Run IN-PROCESS with the agent's privileges, so a
    # plugin loads only when enabled: listed here, or ``enabled: true`` in its
    # own manifest. ``dir`` overrides the live plugins root (default
    # ``<config_dir>/plugins``); shipped examples live in ``plugins/``.
    # See graph/plugins/ and docs/guides/plugins.md.
    plugins_enabled: list[str] = field(default_factory=list)
    # Denylist — turn OFF a plugin even if its manifest says ``enabled: true``.
    # Lets a fork disable a bundled first-party plugin (e.g. the Discord surface)
    # without deleting its directory or editing core.
    plugins_disabled: list[str] = field(default_factory=list)
    plugins_dir: str = ""
    # Optional source allowlist for git-URL installs (ADR 0027 D3) — host/org globs
    # (e.g. ``github.com/protoLabsAI/*``); empty = any URL allowed (gated install).
    plugins_sources_allow: list[str] = field(default_factory=list)
    # Plugin-declared config sections (ADR 0019), keyed by the claimed top-level
    # section. Each value is the section's resolved config (manifest defaults ⊕
    # YAML ⊕ secrets overlay). A plugin reads its own via plugin_config["<section>"].
    plugin_config: dict = field(default_factory=dict)

    # Identity — captured by the setup wizard, editable via the drawer.
    # ``identity_name`` falls back to the AGENT_NAME env var at runtime;
    # the YAML value wins when both are set so per-fork customization
    # survives image rebuilds. ``operator`` is the human the agent thinks
    # it's talking to — injected into the system prompt when non-empty.
    identity_name: str = "protoagent"
    identity_operator: str = ""
    identity_org: str = ""  # white-label org label (settings_schema FIELDS + runtime status + Header)

    # A2A card identity (#570). Forks declare their advertised skills + card
    # description here (or a plugin contributes skills via register_a2a_skill)
    # instead of editing server/a2a.py. ``a2a_skills`` is a list of skill specs
    # (id/name/description/tags/examples, + optional output_schema/result_mime);
    # empty falls back to the template placeholder so a fresh clone stays
    # callable. ``a2a_description`` overrides the card description; blank uses the
    # template default. The card ``name`` already resolves from identity (see
    # agent_name()).
    a2a_skills: list[dict] = field(default_factory=list)
    a2a_description: str = ""
    # When true, refuse to start if the card would advertise a loopback URL
    # (e.g. A2A_PUBLIC_URL unset on a deployed agent → http://127.0.0.1:.../a2a,
    # silently unreachable to remote consumers). Off by default — local/desktop
    # runs SHOULD advertise loopback (the client is same-host). Enforced by
    # server/a2a.py::assert_routable_card_url() at startup.
    a2a_require_routable_url: bool = False

    # Instance id for multi-instance data scoping (ADR 0004). When set, every
    # store nests under <base>/<id>/ so several instances can share one
    # filesystem without clobbering each other. Empty = single-instance (legacy)
    # paths, unchanged. Seeded into the PROTOAGENT_INSTANCE env at startup so the
    # env-reading stores (knowledge/scheduler/memory) honor it too.
    instance_id: str = ""

    # A2A bearer token — blank = open mode (local dev). Writing a token
    # here makes the A2A handler require ``Authorization: Bearer <token>``
    # on every request and advertises the bearer scheme on the agent card.
    # Kept in YAML rather than env so the drawer can manage it.
    auth_token: str = ""

    # OS-level autostart — ``True`` means the server launches on user
    # login (macOS LaunchAgent today; Linux/Windows TBD). Managed by
    # ``autostart.py``; the field here is the source of truth for
    # whether the plist should exist.
    autostart_on_boot: bool = False

    # Box runtime (Host layer, ADR 0047 D8) — box-wide knobs promoted out of
    # scattered env/CLI reads into the Host cascade layer (scope="host" in FIELDS).
    # Each pairs with an env-var fallback in ``from_dict`` (file > env > default), so
    # existing PROTOAGENT_* boxes keep working with zero migration. Consumed by the
    # host process (uvicorn bind / workspace port picker / fleet discovery + warm
    # supervisor) — a workspace leaf override is a silent no-op (the host process
    # reads its own config), see ADR §5.
    bind_host: str = "127.0.0.1"          # uvicorn bind interface; env: PROTOAGENT_HOST
    fleet_port_base: int = 7870           # workspace agents get port_base+1, +2, …
    discovery_port_min: int = 7860        # fleet discovery scan window (inclusive)
    discovery_port_max: int = 7910
    discovery_mdns: bool = True           # advertise + browse the _protoagent._tcp mDNS channel
    fleet_max_warm: int = 0               # warm-agent cap (0 = unlimited); env: PROTOAGENT_FLEET_MAX_WARM
    fleet_warm_grace_seconds: int = 0     # spare agents touched within N s from LRU eviction; env: PROTOAGENT_FLEET_WARM_GRACE

    # Operator-console directory allowlist — the extra directories the
    # React console's beads/notes APIs may read and write. The protoAgent
    # repo root is always allowed implicitly (it's the default project);
    # add other project roots here to operate on them. Empty = repo root
    # only. The client sends a free-text project path, so this server-side
    # list — not the UI — is the security boundary. See operator_api/paths.
    operator_allowed_dirs: list[str] = field(default_factory=list)

    # The operator console's working directory — where its beads/notes live, and
    # the agent's default project. Set in the setup wizard / Settings. Blank =
    # the resolver's default (PROTOAGENT_PROJECT_DIR env, else the protoAgent
    # dir). Read by ``server._resolve_operator_project_root``; always allowed.
    operator_project_dir: str = ""

    # Fenced filesystem toolset (ADR 0007 — operator primitives). ON by default,
    # fenced to a default **workspace** dir (paths.workspace_dir) when no explicit
    # ``projects`` are configured — read/write/list/search, every path contained
    # under the workspace root (``..``/symlink escapes refused). A capable, safe
    # first run: the agent can actually work with files, but only inside the fence.
    # ``projects`` entries: ``{name, path, write: true|false}`` register extra dirs.
    # ``allow_run`` adds the dual-use ``run_command`` power tool. ON by default
    # now that it's gated: run_command (like execute_code) is fenced cwd but
    # arbitrary argv (not a real sandbox), so each call pauses for HITL approval
    # (``run_requires_approval``) — the operator sees the command + approves. A
    # fork can drop the gate inside a hardened container / trusted autonomous run.
    filesystem_enabled: bool = True
    filesystem_allow_run: bool = True
    filesystem_run_requires_approval: bool = True
    filesystem_projects: list[dict] = field(default_factory=list)

    # Egress allowlist (ADR 0008) — deny-by-default outbound-host allowlist
    # enforced in ``fetch_url`` (the model-chosen-host exfil/SSRF vector). Empty
    # = permissive (off). ``*.host`` matches subdomains. Single source of truth
    # for the generated OpenShell network policy (scripts/gen_openshell_policy).
    egress_allowed_hosts: list[str] = field(default_factory=list)

    # Opt-in CIDR allowlist for outbound A2A destinations — push callbacks +
    # delegate_to a2a delegates (#572). Empty/unset = today's behavior (callbacks keep their
    # default private-IP denylist; delegate_to unrestricted). When set, an
    # outbound destination is allowed iff every resolved IP is inside a listed
    # CIDR. Enforced via ``policy.set_callback_allowlist``.
    security_callback_allowlist: list[str] = field(default_factory=list)

    def __post_init__(self):
        # PROTOAGENT_MODEL wins over the YAML/default model so an eval sweep can
        # boot the same agent against different models without editing config
        # (evals/sweep.py). Applied here so it holds on *every* construction
        # path — including the defaults fallback when no YAML is present (CI,
        # fresh forks), not just the from_yaml parse branch.
        env_model = os.environ.get("PROTOAGENT_MODEL")
        if env_model:
            self.model_name = env_model

    def effective_filesystem_projects(self, *, create: bool = False) -> list[dict]:
        """The fs project registry the agent actually gets. Explicit
        ``filesystem_projects`` win; otherwise (when filesystem is enabled) a
        single default ``workspace`` project so the on-by-default fs toolset has a
        fenced place to work. ``create=True`` mkdirs the workspace dir."""
        if self.filesystem_projects:
            return self.filesystem_projects
        if not self.filesystem_enabled:
            return []
        from infra.paths import workspace_dir
        return [{"name": "workspace", "path": str(workspace_dir(create=create)), "write": True}]

    @classmethod
    def from_yaml(cls, path: str | Path) -> "LangGraphConfig":
        """Load config via the App→Host→Agent cascade (ADR 0047).

        Reads the **agent** (leaf) YAML at ``path`` and overlays it on the box-shared
        **Host** layer (``host-config.yaml``, filtered to host-scoped FIELDS keys).
        Agent values win (git-style per-field override); the **App** layer is the
        dataclass defaults filled in by :meth:`from_dict`. With NO host file the merge
        collapses to the agent doc alone — byte-identical to the pre-cascade parse
        (zero-migration). Secrets stay leaf-only (sibling ``secrets.yaml``).
        """
        p = Path(path)
        host_layer = _load_host_layer()  # {} when absent/unreadable — never crashes boot
        if not p.exists() and not host_layer:
            return cls()

        agent_data: dict = {}
        if p.exists():
            with open(p) as f:
                agent_data = yaml.safe_load(f) or {}

        # Host is the base; the agent leaf overlays it (agent wins). No host layer ⇒
        # merged is exactly the agent doc — the pre-cascade input, unchanged.
        merged = _deep_merge_dicts(copy.deepcopy(host_layer), agent_data) if host_layer else agent_data

        secrets = _load_secrets_doc(p.parent)
        return cls.from_dict(merged, secrets=secrets, config_dir=p.parent)

    @classmethod
    def from_dict(
        cls,
        data: dict | None,
        *,
        secrets: dict | None = None,
        config_dir: Path | str | None = None,
    ) -> "LangGraphConfig":
        """Build a config from an already-loaded YAML dict — the parse seam.

        ``secrets`` overlays secret keys (model api_key, auth token); ``config_dir``
        locates plugin config (ADR 0019). Both optional so callers/tests can parse a
        bare dict. Behavior is identical to the old inline ``from_yaml`` parse.
        """
        data = data or {}
        secrets = secrets or {}
        config_dir = Path(config_dir) if config_dir is not None else Path(".")

        model = data.get("model", {})
        subagents = data.get("subagents", {})
        middleware = data.get("middleware", {})
        knowledge = data.get("knowledge", {})
        skills = data.get("skills", {})
        mcp = data.get("mcp", {})
        operator_mcp = data.get("operator_mcp", {})
        plugins = data.get("plugins", {})
        identity = data.get("identity", {})
        # `or {}` (not a default arg): a section present but empty/commented in
        # YAML parses to None, and `.get(...)` on the default arg wouldn't catch
        # that — the example ships an all-commented `a2a:` block.
        a2a = data.get("a2a") or {}
        auth = data.get("auth", {})
        runtime = data.get("runtime", {})
        operator = data.get("operator", {})
        # Box runtime (Host layer, ADR 0047 D8) — `or {}` because a present-but-empty
        # section parses to None.
        network = data.get("network", {}) or {}
        fleet = data.get("fleet", {}) or {}
        discovery = fleet.get("discovery", {}) or {}
        warm = fleet.get("warm", {}) or {}

        # Secret overlay wins when present; otherwise the (now secret-free)
        # main YAML value, otherwise the dataclass default — and a blank
        # value still lets create_llm / set_a2a_token fall back to env.
        secret_api_key = secrets.get("model", {}).get("api_key")
        secret_auth_token = secrets.get("auth", {}).get("token")

        config = cls(
            model_provider=model.get("provider", cls.model_provider),
            model_name=model.get("name", cls.model_name),
            api_base=model.get("api_base", cls.api_base),
            api_key=secret_api_key or model.get("api_key", cls.api_key),
            temperature=model.get("temperature", cls.temperature),
            max_tokens=model.get("max_tokens", cls.max_tokens),
            model_vision=model.get("vision", cls.model_vision),
            max_iterations=model.get("max_iterations", cls.max_iterations),
            request_timeout=model.get("request_timeout", cls.request_timeout),
            llm_max_retries=model.get("max_retries", cls.llm_max_retries),
            top_p=model.get("top_p", cls.top_p),
            top_k=model.get("top_k", cls.top_k),
            presence_penalty=model.get("presence_penalty", cls.presence_penalty),
            repetition_penalty=model.get("repetition_penalty", cls.repetition_penalty),
            chat_template_kwargs=model.get("chat_template_kwargs", cls.chat_template_kwargs),
            knowledge_middleware=middleware.get("knowledge", cls.knowledge_middleware),
            audit_middleware=middleware.get("audit", cls.audit_middleware),
            memory_middleware=middleware.get("memory", cls.memory_middleware),
            scheduler_enabled=middleware.get("scheduler", cls.scheduler_enabled),
            enforcement_enabled=middleware.get("enforcement", cls.enforcement_enabled),
            enforcement_disallowed_tools=(
                data.get("enforcement", {}).get("disallowed_tools", [])
            ),
            enforcement_rate_limits=(
                data.get("enforcement", {}).get("rate_limits", {})
            ),
            ingest_enabled=middleware.get("ingest", cls.ingest_enabled),
            ingest_tools=data.get("ingest", {}).get("tools", []),
            prompt_cache_enabled=data.get("prompt_cache", {}).get("enabled", cls.prompt_cache_enabled),
            prompt_cache_ttl=data.get("prompt_cache", {}).get("ttl", cls.prompt_cache_ttl),
            prompt_cache_force=data.get("prompt_cache", {}).get("force", cls.prompt_cache_force),
            cache_warming_enabled=data.get("prompt_cache", {}).get("warm", {}).get("enabled", cls.cache_warming_enabled),
            cache_warming_interval_seconds=data.get("prompt_cache", {}).get("warm", {}).get("interval_seconds", cls.cache_warming_interval_seconds),
            compaction_enabled=data.get("compaction", {}).get("enabled", cls.compaction_enabled),
            compaction_trigger=data.get("compaction", {}).get("trigger", cls.compaction_trigger),
            compaction_keep_messages=data.get("compaction", {}).get("keep_messages", cls.compaction_keep_messages),
            compaction_model=data.get("compaction", {}).get("model", cls.compaction_model),
            execute_code_enabled=data.get("execute_code", {}).get("enabled", cls.execute_code_enabled),
            execute_code_timeout=data.get("execute_code", {}).get("timeout", cls.execute_code_timeout),
            execute_code_tools=data.get("execute_code", {}).get("tools", []),
            execute_code_output_truncate=data.get("execute_code", {}).get("output_truncate", cls.execute_code_output_truncate),
            tools_deferred_enabled=data.get("tools", {}).get("deferred", {}).get("enabled", cls.tools_deferred_enabled),
            tools_deferred_keep=list(data.get("tools", {}).get("deferred", {}).get("keep", []) or []),
            tools_disabled=list(data.get("tools", {}).get("disabled", []) or []),
            routing_fallback_models=data.get("routing", {}).get("fallback_models", []),
            aux_model=data.get("routing", {}).get("aux_model", cls.aux_model),
            goal_enabled=data.get("goal", {}).get("enabled", cls.goal_enabled),
            goal_max_iterations=data.get("goal", {}).get("max_iterations", cls.goal_max_iterations),
            goal_no_progress_limit=data.get("goal", {}).get("no_progress_limit", cls.goal_no_progress_limit),
            goal_monitor_interval=data.get("goal", {}).get("monitor_interval", cls.goal_monitor_interval),
            goal_eval_model=data.get("goal", {}).get("eval_model", cls.goal_eval_model),
            goal_verify_timeout=data.get("goal", {}).get("verify_timeout", cls.goal_verify_timeout),
            subagent_max_concurrency=subagents.get("max_concurrency", cls.subagent_max_concurrency),
            subagent_output_truncate=subagents.get("output_truncate", cls.subagent_output_truncate),
            knowledge_db_path=knowledge.get("db_path", cls.knowledge_db_path),
            knowledge_backend=knowledge.get("backend", cls.knowledge_backend),
            knowledge_embedder=knowledge.get("embedder", cls.knowledge_embedder),
            checkpoint_db_path=data.get("checkpoint", {}).get("db_path", cls.checkpoint_db_path),
            telemetry_enabled=data.get("telemetry", {}).get("enabled", cls.telemetry_enabled),
            telemetry_db_path=data.get("telemetry", {}).get("db_path", cls.telemetry_db_path),
            telemetry_retention_days=data.get("telemetry", {}).get("retention_days", cls.telemetry_retention_days),
            inbox_retention_days=data.get("inbox", {}).get("retention_days", cls.inbox_retention_days),
            activity_retention_days=data.get("activity", {}).get("retention_days", cls.activity_retention_days),
            checkpoint_keep_per_thread=data.get("checkpoint", {}).get("keep_per_thread", cls.checkpoint_keep_per_thread),
            checkpoint_max_age_days=data.get("checkpoint", {}).get("max_age_days", cls.checkpoint_max_age_days),
            checkpoint_prune_interval_hours=data.get("checkpoint", {}).get("prune_interval_hours", cls.checkpoint_prune_interval_hours),
            checkpoint_harvest_enabled=data.get("checkpoint", {}).get("harvest_enabled", cls.checkpoint_harvest_enabled),
            knowledge_facts=data.get("knowledge", {}).get("facts", cls.knowledge_facts),
            workflow_dir=data.get("workflows", {}).get("dir", cls.workflow_dir),
            embed_model=knowledge.get("embed_model", cls.embed_model),
            transcribe_model=knowledge.get("transcribe_model", cls.transcribe_model),
            knowledge_embeddings=knowledge.get("embeddings", cls.knowledge_embeddings),
            knowledge_top_k=knowledge.get("top_k", cls.knowledge_top_k),
            knowledge_vector_k=knowledge.get("vector_k", cls.knowledge_vector_k),
            knowledge_rrf_k=knowledge.get("rrf_k", cls.knowledge_rrf_k),
            knowledge_min_score=knowledge.get("min_score", cls.knowledge_min_score),
            knowledge_recall_preview_chars=knowledge.get(
                "recall_preview_chars", cls.knowledge_recall_preview_chars),
            knowledge_embed_breaker_threshold=knowledge.get(
                "embed_breaker_threshold", cls.knowledge_embed_breaker_threshold),
            knowledge_embed_breaker_cooldown_s=knowledge.get(
                "embed_breaker_cooldown_s", cls.knowledge_embed_breaker_cooldown_s),
            knowledge_chunk_max_chars=knowledge.get(
                "chunk_max_chars", cls.knowledge_chunk_max_chars),
            knowledge_chunk_overlap_chars=knowledge.get(
                "chunk_overlap_chars", cls.knowledge_chunk_overlap_chars),
            knowledge_chunk_min_chars=knowledge.get(
                "chunk_min_chars", cls.knowledge_chunk_min_chars),
            knowledge_contextual_enrichment=knowledge.get(
                "contextual_enrichment", cls.knowledge_contextual_enrichment),
            knowledge_context_max_doc_chars=knowledge.get(
                "context_max_doc_chars", cls.knowledge_context_max_doc_chars),
            knowledge_attach_inline_budget=knowledge.get(
                "attach_inline_budget", cls.knowledge_attach_inline_budget),
            skills_enabled=skills.get("enabled", cls.skills_enabled),
            skills_db_path=skills.get("db_path", cls.skills_db_path),
            skills_top_k=skills.get("top_k", cls.skills_top_k),
            skills_dir=skills.get("dir", cls.skills_dir),
            skills_shared=skills.get("shared", cls.skills_shared),
            skills_scope=skills.get("scope", cls.skills_scope),
            commons_path=(data.get("commons", {}) or {}).get("path", cls.commons_path),
            mcp_enabled=mcp.get("enabled", cls.mcp_enabled),
            mcp_servers=list(mcp.get("servers", []) or []),
            mcp_timeout_seconds=mcp.get("timeout_seconds", cls.mcp_timeout_seconds),
            mcp_denylist=list(mcp.get("denylist", []) or []),
            operator_mcp_enabled=operator_mcp.get("enabled", cls.operator_mcp_enabled),
            operator_mcp_tools=list(operator_mcp.get("tools", []) or []),
            agent_runtime=str(data.get("agent_runtime", cls.agent_runtime) or "native"),
            plugins_enabled=list(plugins.get("enabled", []) or []),
            plugins_disabled=list(plugins.get("disabled", []) or []),
            plugins_dir=plugins.get("dir", cls.plugins_dir),
            plugins_sources_allow=list((plugins.get("sources", {}) or {}).get("allow", []) or []),
            identity_name=identity.get("name", cls.identity_name),
            identity_operator=identity.get("operator", cls.identity_operator),
            identity_org=identity.get("org", cls.identity_org),
            a2a_skills=list(a2a.get("skills", []) or []),
            a2a_description=a2a.get("description", "") or "",
            a2a_require_routable_url=bool(a2a.get("require_routable_url", False)),
            instance_id=data.get("instance", {}).get("id", "") or data.get("instance_id", cls.instance_id),
            auth_token=secret_auth_token or auth.get("token", cls.auth_token),
            autostart_on_boot=runtime.get("autostart_on_boot", cls.autostart_on_boot),
            # Box runtime (Host layer, ADR 0047 D8) — file > env > default. The env
            # fallback only fires when the merged dict omits the key (zero-migration).
            bind_host=network.get("bind", _env_default("PROTOAGENT_HOST", cls.bind_host)),
            fleet_port_base=fleet.get("port_base", cls.fleet_port_base),
            discovery_port_min=discovery.get("port_min", cls.discovery_port_min),
            discovery_port_max=discovery.get("port_max", cls.discovery_port_max),
            discovery_mdns=discovery.get("mdns", cls.discovery_mdns),
            fleet_max_warm=warm.get("max", _env_default("PROTOAGENT_FLEET_MAX_WARM", cls.fleet_max_warm, int)),
            fleet_warm_grace_seconds=warm.get(
                "grace_seconds", _env_default("PROTOAGENT_FLEET_WARM_GRACE", cls.fleet_warm_grace_seconds, int)
            ),
            operator_allowed_dirs=list(operator.get("allowed_dirs", []) or []),
            operator_project_dir=str(operator.get("project_dir", "") or ""),
            filesystem_enabled=data.get("filesystem", {}).get("enabled", cls.filesystem_enabled),
            filesystem_allow_run=data.get("filesystem", {}).get("allow_run", cls.filesystem_allow_run),
            filesystem_run_requires_approval=data.get("filesystem", {}).get(
                "run_requires_approval", cls.filesystem_run_requires_approval
            ),
            filesystem_projects=list(data.get("filesystem", {}).get("projects", []) or []),
            egress_allowed_hosts=list(data.get("egress", {}).get("allowed_hosts", []) or []),
            security_callback_allowlist=list((data.get("security") or {}).get("callback_allowlist", []) or []),
            plugin_config=_resolve_plugin_config(data, secrets, config_dir),
        )

        for name in ("researcher",):
            if name in subagents:
                sub = subagents[name]
                setattr(config, name, SubagentDef(
                    enabled=sub.get("enabled", True),
                    tools=sub.get("tools", getattr(config, name).tools),
                    max_turns=sub.get("max_turns", getattr(config, name).max_turns),
                    model=sub.get("model", getattr(config, name).model),
                ))

        return config
