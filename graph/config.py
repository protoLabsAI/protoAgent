"""LangGraph configuration loader for protoAgent.

Loads from ``config/langgraph-config.yaml`` when present, falls back
to hardcoded defaults otherwise. Fork this file to add agent-specific
config surface (extra subagents, domain flags, custom knowledge
store paths, etc.).

The defaults here point at the protoLabs LiteLLM gateway via the
``protolabs/<agent>`` alias pattern — retarget ``model.name`` in the
YAML (or swap the gateway alias) per agent without code changes.
"""

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


@dataclass
class SubagentDef:
    enabled: bool = True
    tools: list[str] = field(default_factory=list)
    max_turns: int = 30


@dataclass
class LangGraphConfig:
    # Model settings — route through the LiteLLM gateway by default
    model_provider: str = "openai"
    model_name: str = "protolabs/agent"  # override in YAML per agent
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
    embed_model: str = "nomic-embed-text"
    knowledge_top_k: int = 5

    # Conversation checkpointer — persists each chat session's history per
    # thread_id so multi-turn chats survive a server restart. A path → durable
    # SQLite (same /sandbox→~/.protoagent writable fallback as the stores);
    # blank → in-memory (history cleared on restart). Bound at graph-compile
    # time (see graph/checkpointer.py); changing the path needs a restart.
    checkpoint_db_path: str = "/sandbox/checkpoints.db"
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

    # MCP — Model Context Protocol client. Connect to external MCP servers
    # (stdio or streamable-HTTP); their tools become agent tools, namespaced
    # ``<server>__<tool>`` so they can't shadow core tools. OFF by default —
    # configuring a server is the opt-in. ``servers`` entries are
    # ``{name, transport, command/args/env | url/headers}``. See tools/mcp_tools.py.
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
    plugins_dir: str = ""

    # Identity — captured by the setup wizard, editable via the drawer.
    # ``identity_name`` falls back to the AGENT_NAME env var at runtime;
    # the YAML value wins when both are set so per-fork customization
    # survives image rebuilds. ``operator`` is the human the agent thinks
    # it's talking to — injected into the system prompt when non-empty.
    identity_name: str = "protoagent"
    identity_operator: str = ""

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
            checkpoint_keep_per_thread=data.get("checkpoint", {}).get("keep_per_thread", cls.checkpoint_keep_per_thread),
            checkpoint_max_age_days=data.get("checkpoint", {}).get("max_age_days", cls.checkpoint_max_age_days),
            checkpoint_prune_interval_hours=data.get("checkpoint", {}).get("prune_interval_hours", cls.checkpoint_prune_interval_hours),
            checkpoint_harvest_enabled=data.get("checkpoint", {}).get("harvest_enabled", cls.checkpoint_harvest_enabled),
            embed_model=knowledge.get("embed_model", cls.embed_model),
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
            plugins_dir=plugins.get("dir", cls.plugins_dir),
            identity_name=identity.get("name", cls.identity_name),
            identity_operator=identity.get("operator", cls.identity_operator),
            auth_token=secret_auth_token or auth.get("token", cls.auth_token),
            autostart_on_boot=runtime.get("autostart_on_boot", cls.autostart_on_boot),
            operator_allowed_dirs=list(operator.get("allowed_dirs", []) or []),
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
