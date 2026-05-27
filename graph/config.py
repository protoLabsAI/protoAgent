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

    # Context compaction — wires langchain's SummarizationMiddleware to
    # summarize old history near the context limit. Opt-in. trigger is
    # "fraction:0.8" | "tokens:120000" | "messages:80"; keep = last N messages.
    compaction_enabled: bool = False
    compaction_trigger: str = "fraction:0.8"
    compaction_keep_messages: int = 20
    compaction_model: str = ""            # blank = summarize with the main model

    # Model routing / failover — wires langchain's ModelFallbackMiddleware.
    # On primary error, retry on each fallback model (same gateway) in order.
    routing_fallback_models: list[str] = field(default_factory=list)

    # Knowledge store — sqlite + FTS5, see ``knowledge/store.py``.
    # The default path lives under ``/sandbox/`` to play well with the
    # bundled Docker volume; the store falls back to
    # ``~/.protoagent/knowledge/agent.db`` automatically when /sandbox
    # is read-only or absent (e.g. local ``python server.py``).
    knowledge_db_path: str = "/sandbox/knowledge/agent.db"
    embed_model: str = "nomic-embed-text"
    knowledge_top_k: int = 5

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

    @classmethod
    def from_yaml(cls, path: str | Path) -> "LangGraphConfig":
        """Load config from YAML file. Falls back to defaults if absent."""
        p = Path(path)
        if not p.exists():
            return cls()

        with open(p) as f:
            data = yaml.safe_load(f) or {}

        model = data.get("model", {})
        subagents = data.get("subagents", {})
        middleware = data.get("middleware", {})
        knowledge = data.get("knowledge", {})
        identity = data.get("identity", {})
        auth = data.get("auth", {})
        runtime = data.get("runtime", {})

        config = cls(
            model_provider=model.get("provider", cls.model_provider),
            model_name=model.get("name", cls.model_name),
            api_base=model.get("api_base", cls.api_base),
            api_key=model.get("api_key", cls.api_key),
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
            compaction_enabled=data.get("compaction", {}).get("enabled", cls.compaction_enabled),
            compaction_trigger=data.get("compaction", {}).get("trigger", cls.compaction_trigger),
            compaction_keep_messages=data.get("compaction", {}).get("keep_messages", cls.compaction_keep_messages),
            compaction_model=data.get("compaction", {}).get("model", cls.compaction_model),
            routing_fallback_models=data.get("routing", {}).get("fallback_models", []),
            subagent_max_concurrency=subagents.get("max_concurrency", cls.subagent_max_concurrency),
            subagent_output_truncate=subagents.get("output_truncate", cls.subagent_output_truncate),
            knowledge_db_path=knowledge.get("db_path", cls.knowledge_db_path),
            embed_model=knowledge.get("embed_model", cls.embed_model),
            knowledge_top_k=knowledge.get("top_k", cls.knowledge_top_k),
            identity_name=identity.get("name", cls.identity_name),
            identity_operator=identity.get("operator", cls.identity_operator),
            auth_token=auth.get("token", cls.auth_token),
            autostart_on_boot=runtime.get("autostart_on_boot", cls.autostart_on_boot),
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
