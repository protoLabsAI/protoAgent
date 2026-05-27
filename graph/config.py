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
    temperature: float = 0.3
    max_tokens: int = 4096
    max_iterations: int = 75

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

    # Middleware / subsystem toggles. All default-on so a fresh fork has
    # a working memory loop + scheduler on day one. Forks that want a
    # purely stateless agent (no KB, no scheduled tasks) can flip these
    # via the drawer or by editing the YAML directly.
    knowledge_middleware: bool = True
    audit_middleware: bool = True
    memory_middleware: bool = True
    scheduler_enabled: bool = True

    # Knowledge store — sqlite + FTS5, see ``knowledge/store.py``.
    # The default path lives under ``/sandbox/`` to play well with the
    # bundled Docker volume; the store falls back to
    # ``~/.protoagent/knowledge/agent.db`` automatically when /sandbox
    # is read-only or absent (e.g. local ``python server.py``).
    knowledge_db_path: str = "/sandbox/knowledge/agent.db"
    embed_model: str = "qwen3-embedding"
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
