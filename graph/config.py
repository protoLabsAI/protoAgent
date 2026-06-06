"""LangGraph configuration loader for protoAgent.

Loads from ``config/langgraph-config.yaml`` when present, falls back
to hardcoded defaults otherwise. Fork this file to add agent-specific
config surface (extra subagents, domain flags, custom knowledge
store paths, etc.).

The defaults here point at the protoLabs LiteLLM gateway via the
``protolabs/<agent>`` alias pattern — retarget ``model.name`` in the
YAML (or swap the gateway alias) per agent without code changes.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

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
    except Exception:  # noqa: BLE001 — plugin config is best-effort
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


@dataclass
class SubagentDef:
    enabled: bool = True
    tools: list[str] = field(default_factory=list)
    max_turns: int = 30


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
    researcher: SubagentDef = field(default_factory=lambda: SubagentDef(
        tools=[
            "current_time",
            "web_search", "fetch_url",
            "memory_recall", "memory_list",
        ],
        max_turns=40,
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
    goal_eval_model: str = ""             # blank = main model (llm verifier / fuzzy goals)
    goal_verify_timeout: float = 120.0    # seconds for command/test/ci verifiers

    # Knowledge store — sqlite + FTS5, see ``knowledge/store.py``.
    # The default path lives under ``/sandbox/`` to play well with the
    # bundled Docker volume; the store falls back to
    # ``~/.protoagent/knowledge/agent.db`` automatically when /sandbox
    # is read-only or absent (e.g. local ``python server.py``).
    knowledge_db_path: str = "/sandbox/knowledge/agent.db"
    # The gateway's embedding model (NOT the chat model). Default is what the
    # protoLabs gateway serves; forks on a different gateway set this to a model
    # their gateway has (check GET /v1/models). A wrong/absent model degrades to
    # keyword search via the store's circuit breaker — never KB-less.
    embed_model: str = "qwen3-embedding"
    # Semantic recall (ADR 0021): when True, the knowledge store is the
    # HybridKnowledgeStore (FTS5 + vector embeddings via `embed_model`, fused
    # with RRF). On by default — semantic recall finds paraphrases keyword search
    # misses; the circuit breaker falls back to FTS5 on an embedding outage.
    knowledge_embeddings: bool = True
    knowledge_top_k: int = 5

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

    # Workflows — declarative multi-step subagent recipes (see ADR 0002),
    # exposed via the run_workflow tool. Bundled examples ship in the repo
    # ``workflows/`` dir; ``dir`` is the writable root for user/agent-emitted
    # recipes (same /sandbox→~/.protoagent fallback, resolved in server.py).
    workflows_enabled: bool = True
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

    # Operator-console directory allowlist — the extra directories the
    # React console's beads/notes APIs may read and write. The protoAgent
    # repo root is always allowed implicitly (it's the default project);
    # add other project roots here to operate on them. Empty = repo root
    # only. The client sends a free-text project path, so this server-side
    # list — not the UI — is the security boundary. See operator_api/paths.
    operator_allowed_dirs: list[str] = field(default_factory=list)

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
    # peer_consult (#572). Empty/unset = today's behavior (callbacks keep their
    # default private-IP denylist; peer_consult unrestricted). When set, an
    # outbound destination is allowed iff every resolved IP is inside a listed
    # CIDR. Enforced via ``security.set_callback_allowlist``.
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
        from paths import workspace_dir
        return [{"name": "workspace", "path": str(workspace_dir(create=create)), "write": True}]

    @classmethod
    def from_yaml(cls, path: str | Path) -> "LangGraphConfig":
        """Load config from YAML file. Falls back to defaults if absent."""
        p = Path(path)
        if not p.exists():
            return cls()

        with open(p) as f:
            data = yaml.safe_load(f) or {}

        secrets = _load_secrets_doc(p.parent)

        model = data.get("model", {})
        subagents = data.get("subagents", {})
        middleware = data.get("middleware", {})
        knowledge = data.get("knowledge", {})
        skills = data.get("skills", {})
        mcp = data.get("mcp", {})
        plugins = data.get("plugins", {})
        identity = data.get("identity", {})
        # `or {}` (not a default arg): a section present but empty/commented in
        # YAML parses to None, and `.get(...)` on the default arg wouldn't catch
        # that — the example ships an all-commented `a2a:` block.
        a2a = data.get("a2a") or {}
        auth = data.get("auth", {})
        runtime = data.get("runtime", {})
        operator = data.get("operator", {})

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
            max_iterations=model.get("max_iterations", cls.max_iterations),
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
            goal_eval_model=data.get("goal", {}).get("eval_model", cls.goal_eval_model),
            goal_verify_timeout=data.get("goal", {}).get("verify_timeout", cls.goal_verify_timeout),
            subagent_max_concurrency=subagents.get("max_concurrency", cls.subagent_max_concurrency),
            subagent_output_truncate=subagents.get("output_truncate", cls.subagent_output_truncate),
            knowledge_db_path=knowledge.get("db_path", cls.knowledge_db_path),
            checkpoint_db_path=data.get("checkpoint", {}).get("db_path", cls.checkpoint_db_path),
            telemetry_enabled=data.get("telemetry", {}).get("enabled", cls.telemetry_enabled),
            telemetry_db_path=data.get("telemetry", {}).get("db_path", cls.telemetry_db_path),
            checkpoint_keep_per_thread=data.get("checkpoint", {}).get("keep_per_thread", cls.checkpoint_keep_per_thread),
            checkpoint_max_age_days=data.get("checkpoint", {}).get("max_age_days", cls.checkpoint_max_age_days),
            checkpoint_prune_interval_hours=data.get("checkpoint", {}).get("prune_interval_hours", cls.checkpoint_prune_interval_hours),
            checkpoint_harvest_enabled=data.get("checkpoint", {}).get("harvest_enabled", cls.checkpoint_harvest_enabled),
            knowledge_facts=data.get("knowledge", {}).get("facts", cls.knowledge_facts),
            workflows_enabled=data.get("workflows", {}).get("enabled", cls.workflows_enabled),
            workflow_dir=data.get("workflows", {}).get("dir", cls.workflow_dir),
            embed_model=knowledge.get("embed_model", cls.embed_model),
            knowledge_embeddings=knowledge.get("embeddings", cls.knowledge_embeddings),
            knowledge_top_k=knowledge.get("top_k", cls.knowledge_top_k),
            skills_enabled=skills.get("enabled", cls.skills_enabled),
            skills_db_path=skills.get("db_path", cls.skills_db_path),
            skills_top_k=skills.get("top_k", cls.skills_top_k),
            skills_dir=skills.get("dir", cls.skills_dir),
            mcp_enabled=mcp.get("enabled", cls.mcp_enabled),
            mcp_servers=list(mcp.get("servers", []) or []),
            mcp_timeout_seconds=mcp.get("timeout_seconds", cls.mcp_timeout_seconds),
            mcp_denylist=list(mcp.get("denylist", []) or []),
            plugins_enabled=list(plugins.get("enabled", []) or []),
            plugins_disabled=list(plugins.get("disabled", []) or []),
            plugins_dir=plugins.get("dir", cls.plugins_dir),
            identity_name=identity.get("name", cls.identity_name),
            identity_operator=identity.get("operator", cls.identity_operator),
            a2a_skills=list(a2a.get("skills", []) or []),
            a2a_description=a2a.get("description", "") or "",
            instance_id=data.get("instance", {}).get("id", "") or data.get("instance_id", cls.instance_id),
            auth_token=secret_auth_token or auth.get("token", cls.auth_token),
            autostart_on_boot=runtime.get("autostart_on_boot", cls.autostart_on_boot),
            operator_allowed_dirs=list(operator.get("allowed_dirs", []) or []),
            filesystem_enabled=data.get("filesystem", {}).get("enabled", cls.filesystem_enabled),
            filesystem_allow_run=data.get("filesystem", {}).get("allow_run", cls.filesystem_allow_run),
            filesystem_run_requires_approval=data.get("filesystem", {}).get(
                "run_requires_approval", cls.filesystem_run_requires_approval
            ),
            filesystem_projects=list(data.get("filesystem", {}).get("projects", []) or []),
            egress_allowed_hosts=list(data.get("egress", {}).get("allowed_hosts", []) or []),
            security_callback_allowlist=list((data.get("security") or {}).get("callback_allowlist", []) or []),
            plugin_config=_resolve_plugin_config(data, secrets, p.parent),
        )

        for name in ("researcher",):
            if name in subagents:
                sub = subagents[name]
                setattr(config, name, SubagentDef(
                    enabled=sub.get("enabled", True),
                    tools=sub.get("tools", getattr(config, name).tools),
                    max_turns=sub.get("max_turns", getattr(config, name).max_turns),
                ))

        return config
