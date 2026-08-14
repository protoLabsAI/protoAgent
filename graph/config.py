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
from graph.watches.types import DEFAULT_KEEP_TERMINAL_H, DEFAULT_WATCH_INTERVAL_S

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
        with open(secrets_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


# ``model.name`` and ``model.provider`` are ONE decision, not two independent fields —
# the same coupling ``settings_schema.MODEL_RESET_GROUP`` already encodes for reset. The
# cascade merged them per-field, which was survivable while every provider spoke the same
# OpenAI-compatible dialect: a mixed pair still built. Native OAuth (ADR 0097) ended that.
#
# What it cost: a host moved to `anthropic-oauth`, `sync_host_model_layer` mirrored
# `provider: anthropic-oauth` into the Host layer, and every fleet member that had
# overridden ONLY `model.name` with a gateway alias inherited the OAuth provider on top of
# its own model id. `anthropic-oauth` + `protolabs/smart` is unbuildable, so those members
# crash-looped at boot while members that overrode neither key (inheriting a coherent
# host pair) stayed up — which is why it read as random.
#
# So the identity travels together: an agent that names EITHER key supplies both, and the
# one it left out comes from App defaults rather than from a provider it never chose.
# ``api_base`` deliberately keeps per-field inheritance — it's an endpoint, not an
# identity, and members legitimately override the model while inheriting the box's gateway.
_MODEL_IDENTITY_KEYS = ("name", "provider")


def _drop_host_model_identity(host_layer: dict, agent_data: dict) -> list[str]:
    """Strip the Host layer's model identity when the agent names its own.

    Mutates ``host_layer`` (already a copy). Returns the keys dropped, for logging.
    """
    agent_model = agent_data.get("model") if isinstance(agent_data, dict) else None
    host_model = host_layer.get("model") if isinstance(host_layer, dict) else None
    if not isinstance(agent_model, dict) or not isinstance(host_model, dict):
        return []
    if not any(str(agent_model.get(k) or "").strip() for k in _MODEL_IDENTITY_KEYS):
        return []  # agent chose neither — inherit the host's pair, which is coherent
    dropped = [k for k in _MODEL_IDENTITY_KEYS if k in host_model and k not in agent_model]
    for key in dropped:
        host_model.pop(key, None)
    if dropped:
        log.info(
            "[config] agent sets model.%s, so model.%s is NOT inherited from the host layer "
            "— the model identity is one decision (App defaults fill the rest)",
            "/".join(k for k in _MODEL_IDENTITY_KEYS if k in agent_model),
            "/".join(dropped),
        )
    return dropped


def _read_config_docs(p: Path) -> tuple[dict, dict, bool]:
    """The file-reading half of ``from_yaml``: (host ⊕ agent) merged doc + secrets
    overlay. The third element is False when there is nothing to parse (no agent
    file and no host layer) — ``from_yaml``'s defaults short-circuit."""
    host_layer = _load_host_layer()  # {} when absent/unreadable — never crashes boot
    if not p.exists() and not host_layer:
        return {}, {}, False

    agent_data: dict = {}
    if p.exists():
        with open(p, encoding="utf-8") as f:
            agent_data = yaml.safe_load(f) or {}

    # Host is the base; the agent leaf overlays it (agent wins). No host layer ⇒
    # merged is exactly the agent doc — the pre-cascade input, unchanged.
    if host_layer:
        # Surface silent shadowing of a box default by the agent leaf (issue #1459).
        _warn_shadowed_host_keys(host_layer, agent_data)
        host_base = copy.deepcopy(host_layer)
        _drop_host_model_identity(host_base, agent_data)
        merged = _deep_merge_dicts(host_base, agent_data)
    else:
        merged = agent_data

    return merged, _load_secrets_doc(p.parent), True


def load_config_docs(path: str | Path) -> tuple[dict, dict]:
    """Public doc loader for callers that need the raw (merged, secrets) pair without
    a full parse — the secrets refresh loop and operator sync/test routes (ADR 0080)."""
    merged, secrets, _ = _read_config_docs(Path(path))
    return merged, secrets


def _hydrate_external_secrets(merged: dict, secrets: dict) -> None:
    """External secrets-manager hydration (ADR 0080): populate ``os.environ`` from the
    configured manager BEFORE the dataclass parse, so the env fallback tier (model
    api_key / auth token, plugin ``requires_env``, MCP child env) sees manager values
    on every load path — boot, ``--setup``, hot-reload, CLIs, fleet members.

    Inert without an enabled ``secrets_manager`` section (no import, no network).
    Never breaks config load: fetch failures warn and fall through to whatever the
    env already has — except ``secrets_manager.required: true``, which propagates so
    a boot with a hard manager dependency fails fast instead of serving a
    half-configured agent."""
    if not isinstance(merged, dict) or not (merged.get("secrets_manager") or {}).get("enabled"):
        return
    try:
        from infra.secrets import SecretsRequiredError, hydrate_from_docs
    except Exception as e:  # noqa: BLE001 — hydration must not take config load down
        log.warning("[secrets] hydration unavailable (import failed): %s", e)
        return
    try:
        hydrate_from_docs(merged, secrets)
    except SecretsRequiredError:
        raise
    except Exception as e:  # noqa: BLE001 — belt-and-suspenders around the never-raise contract
        log.warning("[secrets] hydration failed — continuing with the existing environment: %s", e)


def _resolve_plugin_config(data: dict, secrets: dict, config_dir: Path) -> dict:
    """Resolve each enabled plugin's declared config section (ADR 0019).

    For every plugin that claims a top-level section, merge: manifest defaults ⊕
    the (secret-stripped) YAML section ⊕ the secrets overlay for its secret keys.
    Best-effort — never breaks config load. Returns ``{section: resolved_dict}``.

    Plugins live at ``instance_paths().plugins_dir`` — the instance tier's own
    plugins root (a sibling of ``config/``, honoring ``PROTOAGENT_PLUGINS_DIR`` and
    the config ``plugins.dir`` override). No de-scope dance: the instance root IS
    the scoped leaf, so config and plugins share one tier. (``config_dir`` is kept
    for call compatibility but no longer determines the plugins location.)
    """
    try:
        from infra.paths import instance_paths

        from graph.plugins.pconfig import discover_plugin_config, plugin_roots_from

        plugins = data.get("plugins") or {}
        roots = plugin_roots_from(instance_paths().plugins_dir, str(plugins.get("dir") or ""))
        schemas = discover_plugin_config(
            roots,
            set(plugins.get("enabled") or []),
            set(plugins.get("disabled") or []),
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


_FALSE_STRINGS = {"false", "no", "off", "0", ""}


def _falsey(value, *, default: bool) -> bool:
    """Is this config value a NO? ``default`` is the answer when the key is absent.

    YAML already yields real booleans for ``false``/``no``/``off``, but a value that
    arrives from JSON, an env overlay or a hand-edit can be the *string* ``"false"`` —
    which is truthy, so a bare ``bool()`` would read it as YES. Every caller here gates
    filesystem access, so the string forms are honoured and the ambiguous direction
    resolves toward LESS access, never more.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in _FALSE_STRINGS
    return not bool(value)


def _default_filesystem_allow_run() -> bool:
    """Tier-aware app-default for ``filesystem.allow_run`` (#1849). ``run_command``
    is HITL-gated (``run_requires_approval``) — safe when an operator is watching to
    approve each call, but on a headless/A2A-only instance there's no one to click
    approve, so the pending ``interrupt()`` checkpoints the turn forever and every
    later message queues behind it. Default stays ON for an interactive tier
    (someone's presumably watching); default OFF for the 'none' (headless) UI tier.
    Reads ``PROTOAGENT_UI`` directly rather than via ``_env_default`` because this is
    a derived boolean, not a passthrough value — ``server/__init__`` mirrors the
    FINAL resolved tier there once, whether it came from ``--ui``, ``PROTOAGENT_UI``,
    or the deprecated ``--headless`` alias. An explicit ``filesystem.allow_run`` in
    config always wins over this default regardless of tier."""
    return os.environ.get("PROTOAGENT_UI", "").strip().lower() != "none"


def _host_scoped_fields():
    """The host-scoped (ADR 0047 ``scope=="host"``) settings fields — the single
    source for both the host-layer filter and the shadow check, so they can't drift."""
    from graph.settings_schema import FIELDS

    return [f for f in FIELDS if getattr(f, "scope", "agent") == "host"]


def _filter_to_host_keys(raw: dict) -> dict:
    """Keep only the host-scoped FIELDS keys present in a raw host-config doc.

    The Host file can set box-shared defaults but **cannot inject agent-only
    settings** (ADR 0047 D1/D4) — anything outside the ``scope=="host"`` set is
    dropped here before the merge."""
    out: dict = {}
    for f in _host_scoped_fields():
        found, val = _get_dotted(raw, f.key)
        if found:
            _set_dotted(out, f.key, val)
    return out


def _warn_shadowed_host_keys(host_layer: dict, agent_data: dict) -> None:
    """Warn when the agent leaf overrides a host-scoped (box-shared) key with a
    different, non-empty value. The agent wins (ADR 0047), so the box default in
    ``host-config.yaml`` is silently *shadowed* — otherwise invisible until you read
    the merge (issue #1459). Best-effort: a provenance warning must never break boot."""
    try:
        for f in _host_scoped_fields():
            h_found, h_val = _get_dotted(host_layer, f.key)
            a_found, a_val = _get_dotted(agent_data, f.key)
            if h_found and a_found and a_val not in (None, "") and a_val != h_val:
                log.warning(
                    "config: agent leaf overrides host-scoped %r — the box default %r is "
                    "shadowed by the agent value %r, which wins. Remove %r from the agent "
                    "config (langgraph-config.yaml) to use the box default.",
                    f.key,
                    h_val,
                    a_val,
                    f.key,
                )
    except Exception:  # noqa: BLE001 — never let a provenance warning break config load
        pass


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
        from infra.paths import read_text_utf8

        raw = yaml.safe_load(read_text_utf8(hp)) or {}
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
    # LangGraph `recursion_limit` for the main tool loop (server/chat.py). Counts graph
    # steps through the middleware chain, not tool calls 1:1 — roughly 8 steps per tool
    # call, so 2000 ≈ 250 tool calls/turn. Was a dead field until this was wired up (it
    # only fed a display value); the hardcoded `recursion_limit: 200` it replaces was
    # ≈25 tool calls/turn, so 2000 is deliberately generous headroom, not a copy of the
    # old number. The real backstop for a runaway turn is `turn_stall_timeout_seconds`.
    max_iterations: int = 2000
    # Seconds of stream SILENCE after which a turn is declared wedged and failed
    # (#2344). Deliberately a stall window, not a wall-clock cap: a long turn that
    # keeps emitting frames is healthy and must never be cut off, while a turn stuck
    # inside one tool call emits nothing at all. 900s clears the slowest legitimate
    # single step by a wide margin (an MCP call is separately bounded at 300s by
    # ``mcp.call_timeout_seconds``). ``0`` disables the guard.
    turn_stall_timeout_seconds: float = 900.0
    # Native vision (ADR 0021): set true when `model_name` is image-capable (e.g.
    # protolabs/fast, protolabs/smart). The chat composer then sends attached
    # images as native multimodal parts straight to the model instead of routing
    # them through the extraction pipeline. Off → images go through the pipeline.
    model_vision: bool = False
    # Pinned go-to models (#1957) — the chat `/model` quick-switch shows these (in
    # order) instead of the gateway's full list. Console-consumed via the settings
    # schema; empty = no favorites, /model falls back to the full model list.
    model_favorites: list[str] = field(default_factory=list)

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

    # Reasoning controls for OpenAI-compatible reasoning models (DeepSeek, the
    # OpenAI o-series, etc. behind the gateway). Both opt-in — unset means "let the
    # provider/model card decide", so an un-tuned config is a true no-op (#1113).
    #   ``thinking`` — "" (inherit) | "enabled" | "disabled". When set, rides
    #     ``extra_body`` as ``{"thinking": {"type": ...}}`` (DeepSeek's spelling).
    #   ``reasoning_effort`` — None (inherit) | "low" | "medium" | "high" | "max".
    #     A native ChatOpenAI param, sent top-level. DeepSeek maps low/medium→high
    #     and xhigh→max server-side; we just pass the string through.
    thinking: str = ""
    reasoning_effort: str | None = None

    # Subagents — template ships one example, `researcher` (see
    # graph/subagents/config.py). Add fields here as you add entries to
    # SUBAGENT_REGISTRY. Tool/max_turns here mirror the registry default and
    # are the YAML-overridable layer.
    # Defaults derived from the registry entry (SSOT) so the YAML-overridable layer
    # always matches the runtime default — an un-overridden config is a true no-op.
    researcher: SubagentDef = field(
        default_factory=lambda: SubagentDef(
            tools=list(RESEARCHER_CONFIG.tools),
            max_turns=RESEARCHER_CONFIG.max_turns,
            model=RESEARCHER_CONFIG.model,
        )
    )

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

    # The Discord (ADR 0015/0016) and Google (ADR 0017) surfaces moved out of core
    # to external plugins (ADR 0058/0059) — installed at runtime, their config lives
    # in plugin_config[<section>], never a typed field here.

    # Enforcement gate — opt-in safety middleware that blocks tool calls
    # before they execute (deny list + per-tool rate limits). Off by default;
    # forks enable it and supply a deny list / rate limits (and can attach a
    # custom predicate in code). See graph/middleware/enforcement.py.
    enforcement_enabled: bool = False
    enforcement_disallowed_tools: list[str] = field(default_factory=list)
    enforcement_rate_limits: dict = field(default_factory=dict)

    # Prompt caching — Anthropic prefix caching on the stable system prompt.
    # Safe no-op on non-Anthropic models (gated on model name unless forced).
    # NOTE: this middleware also DELIVERS KnowledgeMiddleware's context to the
    # model (create_agent doesn't read the `context` state key), so it's wired
    # unconditionally; the flags below only control the caching half.
    # Attempt-by-default (#2255): blocks are attached for EVERY model (aliases
    # included); a provider that rejects them auto-falls back per session, one
    # that silently ignores them draws a zero-hit warning.
    prompt_cache_enabled: bool = True
    prompt_cache_ttl: str = "5m"  # "5m" (ephemeral) or "1h" (persistent)
    prompt_cache_force: bool = False  # never auto-fall back — a rejection propagates

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
    compaction_model: str = ""  # blank = summarize with the main model

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

    # Skip re-executing an allowlisted read call when this turn's history already
    # has an up-to-date result for the identical (tool, project, path). Found via a
    # live-session audit: `read_file` on the same file, called twice non-
    # consecutively (so stall_guard's stuck-loop detector never saw it), returning
    # byte-identical truncated content both times. Scoped to explicitly idempotent
    # read tools only (never state-reading tools like a board/issue listing, which
    # can legitimately change within a turn as the agent's own actions alter it),
    # and invalidated the moment a write tool touches the same (project, path) this
    # turn. OFF by default — new, unproven in production.
    tools_memoize_reads_enabled: bool = False

    # Tool denylist — drop named core tools from the agent without editing
    # ``tools/lg_tools.py::get_all_tools``. A fork keeps what it wants by listing
    # the rest here (config ``tools.disabled``); plugins still ADD tools. So
    # "keep what you want, drop the rest, add your own" is fully config + plugin
    # driven — no core edit that conflicts on upstream re-sync.
    tools_disabled: list[str] = field(default_factory=list)

    # Tool HIDE list (config ``tools.hidden``, #2172) — a HARD superset of ``disabled``:
    # a hidden tool is denied like a disabled one (never bound to the graph), AND it is
    # dropped from the console's tool inventory entirely, so it never renders as a
    # toggle and can't be re-enabled from the UI. This is a trust/setup-time control
    # (ADR 0071) — an archetype or a restricted console pins the set in config; the UI
    # is presentation, not the boundary. Disabled = "off but toggleable"; hidden =
    # "not available, and not offered".
    tools_hidden: list[str] = field(default_factory=list)

    # Settings HIDE list (config ``settings.hidden``, #2172) — the settings half of
    # ``tools.hidden``. Entries are dotted field keys ("goal.max_iterations") or whole
    # group prefixes ("goal", including plugin groups like "careercoach"). A hidden
    # setting is dropped from the schema the console renders AND refused by the settings
    # write/reset APIs, so it can't be seen or changed from the UI; its VALUE stays live
    # in config (hiding ≠ disabling). Setup-time trust control (ADR 0071): the config
    # file is the boundary, the UI is presentation. Contrast a field's static
    # ``ui_hidden`` (dev-declared, #1076), which only moves rendering to a dedicated
    # panel and locks nothing.
    settings_hidden: list[str] = field(default_factory=list)

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
    goal_max_iterations: int = 8  # continuation budget per goal
    goal_no_progress_limit: int = 3  # identical verifier evidence N times -> unachievable
    goal_eval_model: str = ""  # blank = main model (llm verifier / fuzzy goals)
    goal_verify_timeout: float = 120.0  # seconds for command/test/ci verifiers

    # Watches (ADR 0067) — agent-held supervised conditions (a deploy, CI, a metric),
    # polled out-of-band; when one trips the agent is resumed to react. `enabled` gates
    # ONLY whether the agent-facing watch tools (create_watch / list_watches /
    # clear_watch) are BOUND — independent of goal mode, and NOT a kill switch: toggling
    # it off never deletes or mutates stored watch state, and the background poller runs
    # regardless (an operator/plugin watch keeps polling on an instance where the agent
    # has no watch tools). Default ON since the feature settled; it was OFF while it
    # cooked (#2020).
    watches_enabled: bool = True
    # Global poll cadence for the out-of-band watch tick, in seconds. A watch's own
    # `interval_s` overrides it as a FLOOR (#1753). The server clamps to >=5s. This was
    # referenced as `watch_interval` in three places for two releases WITHOUT ever being
    # a field, so every read silently took the getattr default and the cadence could not
    # be configured at all.
    watch_interval: float = DEFAULT_WATCH_INTERVAL_S
    # How long a met/expired watch is kept before the tick deletes it, in hours. Without
    # this a terminal watch lived on disk forever and every list surface (the agent's
    # list_watches, sdk.list_watches, GET /api/watches, the console panel) returned it
    # forever. 0 = keep forever, the repo's convention for a retention knob.
    watch_keep_terminal_h: float = DEFAULT_KEEP_TERMINAL_H

    # Self-authored persona (guarded, default OFF). When on, the lead agent gets the
    # ``edit_soul`` tool — it can rewrite SECTIONS of its own ``SOUL.md`` (persona /
    # identity ONLY, never operating doctrine — ADR 0079). Every edit is snapshotted to
    # soul-history (#1691) and reversible, and takes effect on the NEXT turn (a graph
    # reload). This crosses the operator-trust boundary that keeps "SOUL rewrite" an
    # ``/api`` operator capability (ADR 0066/0081), so it stays off unless an operator
    # opts in via ``soul.self_edit_enabled: true``.
    soul_self_edit_enabled: bool = False

    # Deterministic persona drift detection (#1986). A read-only curation pass
    # (lineage of the dream/distill maintenance passes) periodically diffs the live
    # ``SOUL.md`` against its EARLIEST recorded soul-history snapshot — net size
    # delta, section churn (added/dropped markdown headings), and a ``difflib``
    # retention ratio — and publishes a ``persona.drift_detected`` event on the bus
    # when the drift score (``1 - retention``, 0..1) crosses ``threshold``. It never
    # rewrites the persona: recovery already exists via the restore endpoint (ADR
    # 0081), so this only SURFACES the signal. ``interval_hours`` sets the cadence
    # (0 disables the pass); the pass is hosted by the checkpoint-prune maintenance
    # loop, so it runs at most once per interval. Cheap (a diff of two small text
    # files) and a true no-op until there's history to compare against, so it's on
    # by default like the other curation passes.
    soul_drift_enabled: bool = True
    soul_drift_interval_hours: int = 24
    soul_drift_threshold: float = 0.25
    # Semantic tier (#2272) — an opt-in LLM judge that answers what the retention ratio
    # structurally can't: was the persona REWRITTEN (identity lost) or did operating
    # procedure ACCRETE into it (``doctrine_leak`` — the CLAUDE.md/AGENTS.md bloat
    # failure)? Both read as low retention, so only a semantic judgement separates them.
    # Off by default: non-deterministic and it costs tokens. It runs only when the
    # deterministic tier already crossed its threshold — the cheap signal decides whether
    # to look, the judge decides what kind of drift it is. Blank model ⇒ the agent's own.
    soul_drift_judge_enabled: bool = False
    soul_drift_judge_model: str = ""

    # Knowledge store — sqlite + FTS5, see ``knowledge/store.py``.
    # The default path lives under ``/sandbox/`` to play well with the
    # bundled Docker volume; the store falls back to
    # ``~/.protoagent/knowledge/agent.db`` automatically when /sandbox
    # is read-only or absent (e.g. local ``python -m server``).
    knowledge_db_path: str = "/sandbox/knowledge/agent.db"
    # Tier (ADR 0041 / bd-2wu): scoped (private, default) | shared (the whole store is
    # the host-level commons) | layered (read commons ∪ private, write private, operator
    # `knowledge promote`). Blank → scoped. The commons lives at `commons.path`/knowledge.db
    # and a shared/layered fleet must share one `embed_model` (stamped + enforced).
    knowledge_scope: str = ""
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
    # Vision model used to DESCRIBE attached images for a text-only chat model (#1381) —
    # the screenshot is sent to this vision-capable gateway model, its description + any
    # transcribed text is inlined as context the chat model can read. Blank disables image
    # attachments on non-vision models (they error with a clear "switch models" message).
    image_describe_model: str = ""
    # Semantic recall (ADR 0021): when True, the knowledge store is the
    # HybridKnowledgeStore (FTS5 + vector embeddings via `embed_model`, fused
    # with RRF). OFF by default (#1681): out of the box the app must not depend
    # on an optional gateway route — a gateway without a working embedding model
    # turned recall (which runs pre-model on every turn) into a per-turn stall.
    # Opt in when your gateway serves `embed_model`; keyword recall works
    # everywhere, and the circuit breaker still guards runtime outages when on.
    knowledge_embeddings: bool = False
    # How many recalled chunks are injected per turn. Bumped 5 → 10 (RAG bake-off:
    # more candidates in-context lifted answer quality at sub-million-chunk scale).
    knowledge_top_k: int = 10
    # Namespace scope for the AUTO-INJECT RAG search (ADR 0069 D3a). Empty (the
    # default) = unfiltered — today's behavior, so box-commons sharing keeps
    # working. When set, only chunks whose `namespace` matches a listed value
    # are auto-injected; include "" to also match un-namespaced chunks. Gates
    # only what enters the prompt unasked — memory_recall stays unscoped.
    knowledge_inject_namespaces: list[str] = field(default_factory=list)
    # Trust floor for the AUTO-INJECT RAG hits (ADR 0069 D8). Rows rank into
    # deterministic trust tiers by source_type (knowledge/trust.py): 3 =
    # operator-authored, 2 = agent-derived (extracted facts, harvest,
    # memory_ingest), 1 = ingested/external (web, YouTube, PDF, media) and
    # unknown. 1 (default) excludes nothing — low tiers are only down-weighted
    # (ranked below higher tiers); 2 drops external/ingested content from
    # auto-injection; 3 auto-injects operator-authored rows only. memory_recall
    # is never gated — excluded content stays reachable on demand.
    knowledge_inject_min_trust: int = 1
    # Hot-memory write confirm gate (ADR 0069 D8). When True, the agent's own
    # write path (memory_ingest) refuses domain="hot" writes with an error
    # telling it to ask the operator — only operator surfaces (console
    # knowledge/memory routes) put facts in front of the model every turn.
    # Default False: single-operator boxes keep the frictionless flow; every
    # hot write emits a `memory.hot_written` bus event either way.
    knowledge_hot_write_confirm: bool = False
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
    # Prompt snapshot capture (#2243) — record the EXACT system prompt each model
    # call received (stable prefix hash-deduped, volatile tail per call), viewable
    # per turn in the console ("View prompt" / /prompt). ON by default — the
    # operator owns the prompt; cost is one hashed blob + a small tail per call.
    # Retention is trimmed in-write (no maintenance loop); 0 = keep forever.
    prompt_capture_enabled: bool = True
    prompt_capture_retention_days: int = 30
    # Fleet trace export (ADR 0006 / #1897) — write one per-turn trajectory JSONL
    # row (OpenAI chat format) to ``<instance>/fleet-traces/`` for the agent-fleet
    # flywheel. OFF by default; ``PROTOAGENT_FLEET_TRACE_EXPORT`` env overrides in
    # both directions (and can point at an explicit path). Dumps stay on the
    # machine until shipped (scripts/setup_fleet_tracing.sh).
    fleet_trace_export_enabled: bool = False
    # Inbox/Activity retention — delivered inbox items and activity log entries
    # are pruned by the same maintenance loop. 0 = keep forever.
    inbox_retention_days: int = 90
    activity_retention_days: int = 90
    # Checkpoint pruning — keeps the SQLite DB from growing unbounded. Keep the
    # latest N checkpoints per session, and TTL whole sessions idle past
    # max_age_days. Runs every prune_interval_hours (0 disables the sweep).
    checkpoint_keep_per_thread: int = 2
    # Background subagent threads (a2a:background:*) get a tighter cap — we only
    # resume-from-latest, never time-travel, so retaining more than 1 is waste.
    checkpoint_background_keep: int = 1
    # Push-resume (ADR 0070 D1): when a background job finishes, self-submit a nudge
    # turn into the session that spawned it, so its <task-notification> drains
    # immediately and the agent briefs the operator — instead of the report waiting
    # for that session's next manual turn. False restores pull-only delivery.
    background_auto_resume: bool = True
    checkpoint_max_age_days: int = 30
    checkpoint_prune_interval_hours: int = 6
    # When a session is retired (aged out or deleted), summarize it into the
    # knowledge base before dropping the raw checkpoints — so past conversations
    # stay searchable via memory_recall. Needs the knowledge store enabled.
    checkpoint_harvest_enabled: bool = True
    # Vacuum reclaim — after the prune sweep deletes rows, compact the DB file
    # (truncate WAL + free unused pages back to the OS). Best-effort; never
    # blocks pruning on error. True by default so the DB shrinks after deletes.
    checkpoint_vacuum: bool = True
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
    # Max skills listed in the always-on <available_skills> index the model scans
    # each turn (ADR 0060); the rest stay reachable via list_skills, and any one's
    # full procedure is loaded on demand via load_skill. Caps the per-turn "table
    # of contents", not what's usable.
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
    # Bounds a single tool INVOCATION (``timeout_seconds`` above bounds only
    # discovery/connect). Generous by design: legitimate calls really can run for
    # minutes — a filesystem search over a large tree measured ~4 min — so this
    # is a backstop against a call that will NEVER return, not a latency budget.
    # A timeout degrades to a recoverable tool error the model can retry with
    # narrower arguments, never a failed turn. ``0`` disables it; a single server
    # entry can override with ``call_timeout``.
    mcp_call_timeout_seconds: float = 300.0
    mcp_denylist: list[str] = field(default_factory=list)
    # Persistent sessions (default ON): each server keeps ONE long-lived MCP
    # session reused across tool calls, auto-reconnected once when it dies.
    # OFF restores stateless per-call sessions — for stdio servers that means a
    # fresh subprocess (~1s of spawn overhead) per invocation. A single server
    # can opt out alone with ``persistent: false`` on its ``servers`` entry.
    mcp_persistent_sessions: bool = True
    # Tier (ADR 0041) — does this agent run the box-shared MCP commons? "scoped"
    # (default/blank) = only this agent's ``mcp.servers``; "layered" = also run the
    # box commons (``~/.protoagent/commons/mcp-servers.json``), ∪ private (private
    # wins by name). Share/unshare moves an entry between this agent's config and the
    # commons (operator_api/mcp_routes). A shared server runs as a subprocess with its
    # configured secrets on every agent on the box.
    mcp_scope: str = ""

    # Operator MCP server (ADR 0033 slice 1) — expose THIS agent's tools as an MCP
    # server so any MCP client (Claude Desktop, Cursor) or an ACP coding-agent runtime
    # can operate the instance. Opt-in + allowlist-gated: only the named tools are
    # exposed (empty = none). Served standalone via ``python -m server.operator_mcp``.
    operator_mcp_enabled: bool = False
    operator_mcp_tools: list[str] = field(default_factory=list)
    # Curated profile preset over the allowlist (ADR 0075 D3): "" (default) = use
    # ``operator_mcp_tools`` verbatim (deny-by-default); "read-only" = reads/queries
    # only; "full" = everything (≡ "*"). A profile UNIONs with any explicit names.
    # env PROTOAGENT_MCP_TRUST=full forces "full" (vouch for the client). The safe
    # middle tier "safe-operator" arrives with the ops layer (ADR 0075 D2).
    operator_mcp_profile: str = ""

    # Agent runtime (ADR 0033) — which brain executes a turn. "native" = the built-in
    # LangGraph loop (default). "acp:<agent>" (e.g. "acp:codex", "acp:claude") = an
    # external coding agent drives the turn over ACP, mounting the operator MCP bus.
    agent_runtime: str = "native"
    # Developer channel (ADR 0068) — which pre-release feature tier this instance exposes:
    # "prod" (released only) | "beta" (opt-in previews) | "dev" (everything, incl. in-progress).
    # The dev sandbox instance defaults to "dev"; env fallback PROTOAGENT_CHANNEL. Read by
    # ``runtime.flags`` to resolve developer flags.
    developer_channel: str = "prod"
    # ACP launch overrides (ADR 0033) — per-agent ``{command, args}`` from the YAML
    # ``acp.agents`` block, e.g. ``acp: {agents: {claude: {command: claude-agent-acp}}}``.
    # ``runtime.acp_runtime.adapter_for`` reads this to override the built-in default
    # adapter (an ``npx -y …`` fetch) with a locally-installed binary. Empty = use the
    # defaults. Not a settings-UI field — an advanced, machine-local launch knob.
    acp_agents: dict = field(default_factory=dict)

    # Plugins — drop-in packages (manifest + register()) that contribute tools
    # and bundled skills. Run IN-PROCESS with the agent's privileges, so a
    # plugin loads only when enabled: listed here, or ``enabled: true`` in its
    # own manifest. ``dir`` overrides the live plugins root (default
    # ``<config_dir>/plugins``); shipped examples live in ``plugins/``.
    # See graph/plugins/ and docs/guides/plugins.md.
    plugins_enabled: list[str] = field(default_factory=list)
    # Denylist — turn OFF a plugin even if its manifest says ``enabled: true``.
    # Lets a fork disable a bundled first-party plugin (e.g. github, telegram)
    # without deleting its directory or editing core.
    plugins_disabled: list[str] = field(default_factory=list)
    plugins_dir: str = ""
    # Opt-in (ADR 0093): let the frozen desktop app install a plugin's UNBUNDLED
    # requires_pip as pure-Python wheels into a writable per-instance deps dir on
    # sys.path, instead of ADR 0058 D2's refusal. Off by default — installing packages
    # runs code on import, so it stays consent-gated (ADR 0071). No effect off-frozen
    # (a source/server run pip-installs into its own venv as before).
    plugins_allow_unbundled_deps: bool = False
    # Optional source allowlist for git-URL installs (ADR 0027 D3) — host/org globs
    # (e.g. ``github.com/protoLabsAI/*``); empty = any URL allowed (gated install).
    plugins_sources_allow: list[str] = field(default_factory=list)
    # Trust & consent (ADR 0071 D3, #2721) — decides only whether the console asks
    # for a one-time "this runs code" confirm before an install; install ≠ enable ≠
    # trust semantics are untouched. ``official``: auto-trusted source globs —
    # default the protoLabsAI org, FORK-OVERRIDABLE (an explicit empty list means
    # "no official sources", distinct from the key being absent). ``acked``:
    # sources the operator confirmed once (the ack API appends here).
    plugins_sources_official: list[str] = field(default_factory=lambda: ["github.com/protoLabsAI/*"])
    plugins_sources_acked: list[str] = field(default_factory=list)
    # The consent dialog's "don't show again" — every source treated as acked.
    plugins_trust_unverified: bool = False
    # Opt-in auto-update policy (#1720), keyed by plugin id. Each value is a policy
    # dict ``{"track": "main", "when": "idle"}`` — ``track`` opts the plugin in (a
    # non-empty value; the actual ref comes from the lock), ``when`` gates the
    # safe-moment (``idle`` = defer while a chat turn is/was just in flight;
    # ``always`` = update on the next sweep regardless). Empty = manual-only
    # updates (the default). A pinned-to-SHA plugin is never auto-updated.
    plugins_update_policy: dict = field(default_factory=dict)
    # Auto-update sweep cadence in hours (mirrors ``checkpoint_prune_interval_hours``);
    # ``0`` disables the loop entirely. Only plugins listed in ``update_policy`` are
    # ever touched, so this is a safe non-zero default.
    plugins_autoupdate_interval_hours: int = 6
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

    # A2A bearer token — None = unset (falls back to A2A_AUTH_TOKEN env),
    # "" = explicitly off (no env fallback, even if A2A_AUTH_TOKEN is set),
    # non-empty string = use this token. Kept in YAML / secrets.yaml so the
    # drawer can manage it; configure() in a2a_impl/auth.py owns the contract.
    auth_token: str | None = None

    # Optional federation token (ADR 0066) — a SECOND credential handed to
    # semi-trusted A2A peers. It reaches only the /a2a + /v1 consumer surfaces;
    # the /api operator surface (plugin install, config rewrite, subagent runs,
    # the operator goal set-path) is denied it. None = unset (env fallback);
    # "" = explicitly off. Env fallback: A2A_FEDERATION_TOKEN.
    federation_token: str | None = None

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
    bind_host: str = "127.0.0.1"  # uvicorn bind interface; env: PROTOAGENT_HOST
    fleet_port_base: int = 7870  # workspace agents get port_base+1, +2, …
    discovery_port_min: int = 7860  # fleet discovery scan window (inclusive)
    discovery_port_max: int = 7910
    # Off by default (#1802) — an agent should not announce itself on the LAN unless
    # the operator opts in. mDNS advertises this agent (and browses siblings) on the
    # _protoagent._tcp channel; default-off keeps a fleet quiet on the network
    # (privacy/security). Local-fleet enumeration is disk-based, so the console is
    # unaffected; set fleet.discovery.mdns: true to restore LAN auto-discovery.
    discovery_mdns: bool = False  # advertise + browse the _protoagent._tcp mDNS channel
    fleet_max_warm: int = 0  # warm-agent cap (0 = unlimited); env: PROTOAGENT_FLEET_MAX_WARM
    fleet_warm_grace_seconds: int = (
        0  # spare agents touched within N s from LRU eviction; env: PROTOAGENT_FLEET_WARM_GRACE
    )
    # Member ids/names (re)started at hub boot (ADR 0072 autostart slice) — a container
    # recreate/host restart kills the detached member processes, and without this the
    # declared crew stays down until re-activated by hand. env: PROTOAGENT_FLEET_AUTOSTART
    # (comma-separated). See graph/fleet/supervisor.start_autostart_members.
    fleet_autostart: list[str] = field(default_factory=list)

    # System lifecycle reactions (ADR 0074) — a top-level ``lifecycle_hooks:`` YAML list
    # of ``{event, prompt?, webhook?, session?}`` entries the runtime runs when a system
    # lifecycle event fires. ``event`` is one of ``app_loaded`` / ``agent_active`` /
    # ``system_wake``; ``prompt`` enqueues a follow-up turn (in ``session``, or the event's
    # own session for agent_active); ``webhook`` POSTs the event. **Opt-in**: empty ⇒ NOTHING
    # fires beyond the ADR 0039 bus broadcast (plugins can still react via the bus / a
    # ``register_lifecycle_hook`` hook). List of dicts, so it round-trips via config_io.py §B
    # (like ``filesystem_projects`` / ``mcp_servers``), not the string_list-typed FIELDS.
    lifecycle_hooks: list[dict] = field(default_factory=list)

    # DEPRECATED / INERT — was the operator-console path sandbox for the tasks/notes
    # APIs, enforced by ``operator_api.paths.resolve_project_path``. Nothing calls that
    # helper anymore: tasks became a single instance-scoped store (the routes accept
    # ``project_path`` and ignore it) and notes moved to a plugin with its own store, so
    # this list gates nothing. The agent's filesystem fence is ``filesystem_projects``
    # (ADR 0007). Kept in FIELDS + ui_hidden so existing YAML round-trips unchanged;
    # don't wire new behavior to it.
    operator_allowed_dirs: list[str] = field(default_factory=list)

    # Which directory the console calls "this project". Set in the setup wizard /
    # Settings; blank = the resolver's default (PROTOAGENT_PROJECT_DIR env, else the
    # protoAgent dir). Read by ``server._resolve_operator_project_root`` and surfaced as
    # ``project.path`` in runtime status, which the console uses to prefill the wizard.
    # NOT an access grant — it gives the agent no reach into that directory; only
    # ``filesystem_projects`` (ADR 0007) does that.
    operator_project_dir: str = ""

    # Fenced filesystem toolset (ADR 0007 — operator primitives). ON by default,
    # fenced to a default **workspace** dir (paths.workspace_dir) when no explicit
    # ``projects`` are configured — read/write/list/search, every path contained
    # under the workspace root (``..``/symlink escapes refused). A capable, safe
    # first run: the agent can actually work with files, but only inside the fence.
    # ``projects`` entries: ``{name, path, write: true|false}`` register extra dirs.
    # ``allow_run`` adds the dual-use ``run_command`` power tool. Gated: run_command
    # (like execute_code) is fenced cwd but arbitrary argv (not a real sandbox), so
    # each call pauses for HITL approval (``run_requires_approval``) — the operator
    # sees the command + approves. This dataclass default (True) is what a directly
    # constructed config gets; ``from_dict``/``from_yaml`` resolve an UNSET value
    # through ``_default_filesystem_allow_run()`` instead, which flips to False on a
    # headless instance — no operator means a pending approval wedges the turn
    # forever (#1849). Explicit config always wins either way. A fork can also drop
    # the approval gate itself inside a hardened container / trusted autonomous run.
    filesystem_enabled: bool = True
    filesystem_allow_run: bool = True
    filesystem_run_requires_approval: bool = True
    # Escape hatch (off by default per-turn): when True (default), an operator may toggle
    # bypass-permissions mode per-turn via A2A ``message.metadata.bypass_permissions`` to
    # auto-approve ``run_command`` — for trusted-autonomous / container runs. Set False to
    # FORBID bypass entirely regardless of caller metadata (locked-down hosts); the approval
    # gate is then always enforced.
    filesystem_bypass_allowed: bool = True
    filesystem_projects: list[dict] = field(default_factory=list)

    # Managed projects registry (ADR 0095) — the ONE place a project is declared. A
    # top-level ``projects:`` list of ``{name, path, github?, default_branch?, write?,
    # no_delete?, fs?}`` dicts, keyed on ``name`` (the identifier the fs tools already
    # address projects by — deliberately no separate slug). It describes a project's
    # binding on THIS host: where it lives, what it is on GitHub, what the agent may do
    # to it. Deliberately holds no planning state (goals/milestones/issues) — that lives
    # in whatever board owns the work.
    #
    # Consumers project from it rather than re-declaring: the fs fence via
    # ``effective_filesystem_projects`` below, the github plugin's repo picker via
    # ``github``, the board's repo/base branch via ``name``. An EXPLICIT
    # ``filesystem.projects`` (or ``github.repos``, or ``project_board.repo``) still
    # wins, so every existing config and fork loads unchanged.
    #
    # ADR 0095 D3: a registered project is fenced READ-WRITE by default. That's the
    # reverse of the cautious default, and deliberate — the case for "membership grants
    # nothing until separately opted in" needs entries to arrive from somewhere other
    # than the operator (a sync feed / org registry / onboarding webhook), and no such
    # source exists. Opt out per entry: ``write: false`` (read-only), ``no_delete: true``
    # (create/edit, never delete), ``fs: false`` (registered, but NOT in the fence at
    # all — for a project tracked only for github/board purposes).
    #
    # A list of dicts, so it round-trips via config_io.py §B (like ``lifecycle_hooks`` /
    # ``filesystem_projects``), not the string_list-typed FIELDS.
    projects: list[dict] = field(default_factory=list)

    # Project onboarding — bounded clone + register within operator-consented space (#2555).
    # Off by default; the operator opts in by setting enabled: true and declaring a root + allow globs.
    onboarding_enabled: bool = False
    onboarding_root: str = ""            # e.g. ~/dev — clones land here; registrations must resolve UNDER it
    onboarding_allow: list[str] = field(default_factory=list)  # e.g. [github.com/protoLabsAI/*]
    onboarding_write_default: bool = False  # registered read-only unless overridden per-call

    # Core media output store (#1929) — tool-generated binary artifacts
    # (images/audio/video) persisted via ``registry.save_media()`` and served on
    # ``/media/<file>``. Gated by default: each saved URL carries a per-file HMAC
    # signature (an inline ``<img>`` can't send a bearer header), so unsigned
    # requests still hit the default-deny gate. ``public`` opts the whole store
    # out of the auth gate (explicit exposure); ``retention_days`` prunes files
    # older than N days on each save (0 = keep forever). See infra/media.py.
    media_public: bool = False
    media_retention_days: int = 0

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

    # External secrets manager (ADR 0080) — pull env vars from Infisical (etc.) at
    # config load, on hot-reload, and on a refresh interval. client_id/client_secret
    # are the bootstrap machine identity: SECRET_PATHS entries, overlaid from
    # secrets.yaml exactly like model.api_key, with INFISICAL_CLIENT_ID /
    # INFISICAL_CLIENT_SECRET as the env fallback.
    secrets_manager_enabled: bool = False
    secrets_manager_provider: str = "infisical"
    secrets_manager_host: str = "https://us.infisical.com"
    secrets_manager_project_id: str = ""
    secrets_manager_environment: str = "prod"
    secrets_manager_path: str = "/"
    secrets_manager_recursive: bool = True
    secrets_manager_client_id: str = ""
    secrets_manager_client_secret: str = ""
    secrets_manager_refresh_seconds: int = 300
    secrets_manager_required: bool = False
    secrets_manager_override_env: bool = False
    secrets_manager_timeout_seconds: float = 10.0

    # Hosted chat-thread publishing (#2179 P2, #2683) — POST a redacted bundle
    # (graph.chat_bundle) to a hosted viewer service and get back a public link. Empty
    # host is the expected default until the hosted service (#2685, a separate,
    # not-yet-started repo) exists: publish then reports "not configured" rather than
    # erroring or silently no-opping. Behind the chat.publish developer flag (ADR 0068).
    publish_endpoint_url: str = ""
    publish_timeout_seconds: float = 15.0
    # Revocation (#2684) — a SEPARATE configured endpoint, not a path convention off
    # endpoint_url: presuming a URL shape on a service that doesn't exist yet would be a
    # guess dressed up as a contract. See infra/publish/client.py's revoke_bundle.
    publish_revoke_endpoint_url: str = ""

    def __post_init__(self):
        # PROTOAGENT_MODEL wins over the YAML/default model so an eval sweep can
        # boot the same agent against different models without editing config
        # (evals/sweep.py). Applied here so it holds on *every* construction
        # path — including the defaults fallback when no YAML is present (CI,
        # fresh forks), not just the from_yaml parse branch.
        env_model = os.environ.get("PROTOAGENT_MODEL")
        if env_model:
            self.model_name = env_model

    def fenced_projects(self) -> list[dict]:
        """The ADR 0095 ``projects:`` registry projected onto the fs-fence shape.

        Entries opt out with ``fs: false``; everything else is fenced read-write
        by default (D3). Only the fence keys are emitted — ``github`` /
        ``default_branch`` are identity for other consumers and have no business
        in the fence's vocabulary.

        This grants filesystem access, so every rejection is **loud** and every
        ambiguity resolves toward *less* access:

        - a malformed entry (no name, no path, duplicate name, non-absolute path)
          is dropped with a WARNING naming it. ADR 0095's own constraint is that a
          wrong entry must be visible; these are dropped here, so nothing
          downstream can report them and this is the only place that can.
        - ``fs`` / ``write`` are read with :func:`_falsey`, so a string
          ``"false"`` from JSON or a hand-edit opts out instead of silently
          granting read-write on a truthy non-empty string.
        - ``~`` is expanded here so every consumer (the fence, and the ADR 0008
          OpenShell policy) sees the same path. A RELATIVE path is refused
          outright for the reason the work-folders POST route already refuses it:
          it resolves against the server's CWD (``/`` under the desktop sidecar),
          never the operator's.
        """
        fenced: list[dict] = []
        seen: set[str] = set()
        for entry in self.projects or []:
            if not isinstance(entry, dict):
                log.warning("[projects] skipping non-object entry: %r", entry)
                continue
            name = str(entry.get("name") or "").strip()
            path = str(entry.get("path") or "").strip()
            # Malformed is reported BEFORE the opt-out is honoured: `{fs: false}` with no
            # name or path is junk config, and staying quiet about it because it happens
            # to be opted out is the same invisibility the warnings exist to end. A
            # WELL-FORMED opt-out below stays silent — that one is deliberate, not a typo.
            if not name or not path:
                log.warning("[projects] skipping entry missing name/path: %r", entry)
                continue
            if _falsey(entry.get("fs"), default=False):
                continue  # registered for other consumers; no filesystem reach
            if name in seen:
                log.warning(
                    "[projects] duplicate project name %r — keeping the first, dropping this one. "
                    "Names are the fence's identifier; two entries can't share one.",
                    name,
                )
                continue
            # Absoluteness is checked on the EXPANDED-but-unresolved path, because
            # `.resolve()` makes every path absolute (it resolves a relative one against
            # the process CWD) — resolving first would silently swallow the very input
            # this rejects.
            expanded = Path(path).expanduser()
            if not expanded.is_absolute():
                log.warning(
                    "[projects] project %r path is not absolute: %s — skipped. A relative path "
                    "resolves against the SERVER's working directory, never yours.",
                    name,
                    path,
                )
                continue
            seen.add(name)
            projected = {
                "name": name,
                # Emitted RESOLVED, matching tools/fs_tools.py and gen_openshell_policy.py.
                # Without this a symlinked project is projected as the link while the
                # enforced fence and the Landlock policy follow it to the target — the
                # declared-vs-enforced divergence this registry exists to prevent, in the
                # one function whose docstring promises every consumer sees the same path.
                "path": str(expanded.resolve()),
                "write": not _falsey(entry.get("write"), default=False),
            }
            if not _falsey(entry.get("no_delete"), default=True):
                projected["no_delete"] = True
            fenced.append(projected)
        return fenced

    def effective_filesystem_projects(self, *, create: bool = False) -> list[dict]:
        """The fs project registry the agent actually gets. Explicit
        ``filesystem_projects`` win; then the ADR 0095 ``projects:`` registry
        projected onto the fence; otherwise (when filesystem is enabled) a
        single default ``workspace`` project so the on-by-default fs toolset has a
        fenced place to work. ``create=True`` mkdirs the workspace dir."""
        if self.filesystem_projects:
            return self.filesystem_projects
        if not self.filesystem_enabled:
            return []
        # A CONFIGURED registry is the answer, even when it projects to nothing.
        # ``fs: false`` on every entry means "registered, but NOT in the fence at all"
        # (D3) and is honoured literally — substituting the workspace default would
        # grant read-write on a directory the operator just said they didn't want.
        #
        # This used to fall through to that default, on the grounds that an empty fence
        # unbinds the whole toolset with no visible cause (#2251). That reason is gone:
        # every dropped entry now WARNs by name, and ``/api/projects`` reports
        # ``fence_source: "unbound"`` with a console banner. The failure is loud, so it
        # no longer has to be papered over with access nobody asked for.
        #
        # An ABSENT registry still gets the workspace default below — that's the
        # default-install path and it is deliberately unchanged.
        if self.projects:
            return self.fenced_projects()
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
        merged, secrets, present = _read_config_docs(p)
        if not present:
            return cls()

        # External secrets-manager hydration (ADR 0080) — before the parse, so the
        # env fallbacks consulted below (and later, lazily, by create_llm / plugin
        # requires_env gates) already see manager-sourced values.
        _hydrate_external_secrets(merged, secrets)
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
        # A top-level section present but empty/commented-out in YAML parses to
        # None (e.g. a bare `routing:` with every key commented). The many
        # `data.get("x", {}).get(...)` reads below would then return None and
        # crash on `.get`. Normalize null sections to {} so editing the example —
        # commenting a whole block out — can't break the loader. (Subsumes the
        # per-section `or {}` guards; top-level config keys are all mappings.)
        data = {k: ({} if v is None else v) for k, v in data.items()}
        secrets = secrets or {}
        config_dir = Path(config_dir) if config_dir is not None else Path(".")

        model = data.get("model", {})
        subagents = data.get("subagents", {})
        middleware = data.get("middleware", {})
        knowledge = data.get("knowledge", {})
        skills = data.get("skills", {})
        mcp = data.get("mcp", {})
        operator_mcp = data.get("operator_mcp", {})
        acp = data.get("acp", {}) or {}
        plugins = data.get("plugins", {})
        identity = data.get("identity", {})
        # `or {}` (not a default arg): a section present but empty/commented in
        # YAML parses to None, and `.get(...)` on the default arg wouldn't catch
        # that — the example ships an all-commented `a2a:` block.
        a2a = data.get("a2a") or {}
        auth = data.get("auth", {})
        runtime = data.get("runtime", {})
        operator = data.get("operator", {})
        # `or {}` at each level: a present-but-empty `soul:` or `soul.drift:` block
        # parses to None (top-level null is normalized above, a nested null is not).
        soul = data.get("soul", {}) or {}
        soul_drift = soul.get("drift", {}) or {}
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
        secret_federation_token = secrets.get("auth", {}).get("federation_token")
        secrets_manager = data.get("secrets_manager", {})
        secret_sm_client_id = secrets.get("secrets_manager", {}).get("client_id")
        secret_sm_client_secret = secrets.get("secrets_manager", {}).get("client_secret")
        publish = data.get("publish", {}) or {}

        config = cls(
            model_provider=model.get("provider", cls.model_provider),
            model_name=model.get("name", cls.model_name),
            api_base=model.get("api_base", cls.api_base),
            api_key=secret_api_key or model.get("api_key", cls.api_key),
            temperature=model.get("temperature", cls.temperature),
            max_tokens=model.get("max_tokens", cls.max_tokens),
            model_vision=model.get("vision", cls.model_vision),
            model_favorites=list(model.get("favorites", []) or []),
            max_iterations=model.get("max_iterations", cls.max_iterations),
            turn_stall_timeout_seconds=model.get(
                "turn_stall_timeout_seconds", cls.turn_stall_timeout_seconds
            ),
            request_timeout=model.get("request_timeout", cls.request_timeout),
            llm_max_retries=model.get("max_retries", cls.llm_max_retries),
            top_p=model.get("top_p", cls.top_p),
            top_k=model.get("top_k", cls.top_k),
            presence_penalty=model.get("presence_penalty", cls.presence_penalty),
            repetition_penalty=model.get("repetition_penalty", cls.repetition_penalty),
            chat_template_kwargs=model.get("chat_template_kwargs", cls.chat_template_kwargs),
            thinking=model.get("thinking", cls.thinking),
            reasoning_effort=model.get("reasoning_effort", cls.reasoning_effort),
            knowledge_middleware=middleware.get("knowledge", cls.knowledge_middleware),
            audit_middleware=middleware.get("audit", cls.audit_middleware),
            memory_middleware=middleware.get("memory", cls.memory_middleware),
            scheduler_enabled=middleware.get("scheduler", cls.scheduler_enabled),
            enforcement_enabled=middleware.get("enforcement", cls.enforcement_enabled),
            enforcement_disallowed_tools=(data.get("enforcement", {}).get("disallowed_tools", [])),
            enforcement_rate_limits=(data.get("enforcement", {}).get("rate_limits", {})),
            prompt_cache_enabled=data.get("prompt_cache", {}).get("enabled", cls.prompt_cache_enabled),
            prompt_cache_ttl=data.get("prompt_cache", {}).get("ttl", cls.prompt_cache_ttl),
            prompt_cache_force=data.get("prompt_cache", {}).get("force", cls.prompt_cache_force),
            cache_warming_enabled=data.get("prompt_cache", {})
            .get("warm", {})
            .get("enabled", cls.cache_warming_enabled),
            cache_warming_interval_seconds=data.get("prompt_cache", {})
            .get("warm", {})
            .get("interval_seconds", cls.cache_warming_interval_seconds),
            compaction_enabled=data.get("compaction", {}).get("enabled", cls.compaction_enabled),
            compaction_trigger=data.get("compaction", {}).get("trigger", cls.compaction_trigger),
            compaction_keep_messages=data.get("compaction", {}).get("keep_messages", cls.compaction_keep_messages),
            compaction_model=data.get("compaction", {}).get("model", cls.compaction_model),
            tools_deferred_enabled=data.get("tools", {}).get("deferred", {}).get("enabled", cls.tools_deferred_enabled),
            tools_deferred_keep=list(data.get("tools", {}).get("deferred", {}).get("keep", []) or []),
            tools_memoize_reads_enabled=data.get("tools", {})
            .get("memoize_reads", {})
            .get("enabled", cls.tools_memoize_reads_enabled),
            tools_disabled=list(data.get("tools", {}).get("disabled", []) or []),
            tools_hidden=list(data.get("tools", {}).get("hidden", []) or []),
            settings_hidden=list(data.get("settings", {}).get("hidden", []) or []),
            routing_fallback_models=data.get("routing", {}).get("fallback_models", []),
            aux_model=data.get("routing", {}).get("aux_model", cls.aux_model),
            goal_enabled=data.get("goal", {}).get("enabled", cls.goal_enabled),
            goal_max_iterations=data.get("goal", {}).get("max_iterations", cls.goal_max_iterations),
            goal_no_progress_limit=data.get("goal", {}).get("no_progress_limit", cls.goal_no_progress_limit),
            goal_eval_model=data.get("goal", {}).get("eval_model", cls.goal_eval_model),
            goal_verify_timeout=data.get("goal", {}).get("verify_timeout", cls.goal_verify_timeout),
            watches_enabled=data.get("watches", {}).get("enabled", cls.watches_enabled),
            watch_interval=float(data.get("watches", {}).get("interval", cls.watch_interval) or cls.watch_interval),
            watch_keep_terminal_h=float(
                data.get("watches", {}).get("keep_terminal_h", cls.watch_keep_terminal_h) or 0
            ),
            soul_self_edit_enabled=soul.get("self_edit_enabled", cls.soul_self_edit_enabled),
            soul_drift_enabled=bool(soul_drift.get("enabled", cls.soul_drift_enabled)),
            soul_drift_interval_hours=int(
                soul_drift.get("interval_hours", cls.soul_drift_interval_hours) or 0
            ),
            soul_drift_threshold=float(
                soul_drift.get("threshold", cls.soul_drift_threshold) or 0.0
            ),
            soul_drift_judge_enabled=bool(
                (soul_drift.get("judge", {}) or {}).get("enabled", cls.soul_drift_judge_enabled)
            ),
            soul_drift_judge_model=str(
                (soul_drift.get("judge", {}) or {}).get("model", cls.soul_drift_judge_model) or ""
            ),
            subagent_max_concurrency=subagents.get("max_concurrency", cls.subagent_max_concurrency),
            subagent_output_truncate=subagents.get("output_truncate", cls.subagent_output_truncate),
            knowledge_db_path=knowledge.get("db_path", cls.knowledge_db_path),
            knowledge_scope=knowledge.get("scope", cls.knowledge_scope),
            knowledge_backend=knowledge.get("backend", cls.knowledge_backend),
            knowledge_embedder=knowledge.get("embedder", cls.knowledge_embedder),
            checkpoint_db_path=data.get("checkpoint", {}).get("db_path", cls.checkpoint_db_path),
            telemetry_enabled=data.get("telemetry", {}).get("enabled", cls.telemetry_enabled),
            telemetry_db_path=data.get("telemetry", {}).get("db_path", cls.telemetry_db_path),
            telemetry_retention_days=data.get("telemetry", {}).get("retention_days", cls.telemetry_retention_days),
            prompt_capture_enabled=data.get("prompts", {}).get("capture", cls.prompt_capture_enabled),
            prompt_capture_retention_days=data.get("prompts", {}).get(
                "retention_days", cls.prompt_capture_retention_days
            ),
            fleet_trace_export_enabled=data.get("telemetry", {}).get("fleet_trace_export", cls.fleet_trace_export_enabled),
            inbox_retention_days=data.get("inbox", {}).get("retention_days", cls.inbox_retention_days),
            activity_retention_days=data.get("activity", {}).get("retention_days", cls.activity_retention_days),
            checkpoint_keep_per_thread=data.get("checkpoint", {}).get(
                "keep_per_thread", cls.checkpoint_keep_per_thread
            ),
            checkpoint_background_keep=data.get("checkpoint", {}).get(
                "background_keep", cls.checkpoint_background_keep
            ),
            background_auto_resume=data.get("background", {}).get("auto_resume", cls.background_auto_resume),
            checkpoint_max_age_days=data.get("checkpoint", {}).get("max_age_days", cls.checkpoint_max_age_days),
            checkpoint_prune_interval_hours=data.get("checkpoint", {}).get(
                "prune_interval_hours", cls.checkpoint_prune_interval_hours
            ),
            checkpoint_harvest_enabled=data.get("checkpoint", {}).get(
                "harvest_enabled", cls.checkpoint_harvest_enabled
            ),
            checkpoint_vacuum=data.get("checkpoint", {}).get("vacuum", cls.checkpoint_vacuum),
            knowledge_facts=data.get("knowledge", {}).get("facts", cls.knowledge_facts),
            workflow_dir=data.get("workflows", {}).get("dir", cls.workflow_dir),
            embed_model=knowledge.get("embed_model", cls.embed_model),
            transcribe_model=knowledge.get("transcribe_model", cls.transcribe_model),
            image_describe_model=knowledge.get("image_describe_model", cls.image_describe_model),
            knowledge_embeddings=knowledge.get("embeddings", cls.knowledge_embeddings),
            knowledge_top_k=knowledge.get("top_k", cls.knowledge_top_k),
            knowledge_inject_namespaces=list(knowledge.get("inject_namespaces", []) or []),
            knowledge_inject_min_trust=knowledge.get("inject_min_trust", cls.knowledge_inject_min_trust),
            knowledge_hot_write_confirm=knowledge.get("hot_write_confirm", cls.knowledge_hot_write_confirm),
            knowledge_vector_k=knowledge.get("vector_k", cls.knowledge_vector_k),
            knowledge_rrf_k=knowledge.get("rrf_k", cls.knowledge_rrf_k),
            knowledge_min_score=knowledge.get("min_score", cls.knowledge_min_score),
            knowledge_recall_preview_chars=knowledge.get("recall_preview_chars", cls.knowledge_recall_preview_chars),
            knowledge_embed_breaker_threshold=knowledge.get(
                "embed_breaker_threshold", cls.knowledge_embed_breaker_threshold
            ),
            knowledge_embed_breaker_cooldown_s=knowledge.get(
                "embed_breaker_cooldown_s", cls.knowledge_embed_breaker_cooldown_s
            ),
            knowledge_chunk_max_chars=knowledge.get("chunk_max_chars", cls.knowledge_chunk_max_chars),
            knowledge_chunk_overlap_chars=knowledge.get("chunk_overlap_chars", cls.knowledge_chunk_overlap_chars),
            knowledge_chunk_min_chars=knowledge.get("chunk_min_chars", cls.knowledge_chunk_min_chars),
            knowledge_contextual_enrichment=knowledge.get("contextual_enrichment", cls.knowledge_contextual_enrichment),
            knowledge_context_max_doc_chars=knowledge.get("context_max_doc_chars", cls.knowledge_context_max_doc_chars),
            knowledge_attach_inline_budget=knowledge.get("attach_inline_budget", cls.knowledge_attach_inline_budget),
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
            mcp_call_timeout_seconds=mcp.get(
                "call_timeout_seconds", cls.mcp_call_timeout_seconds
            ),
            mcp_denylist=list(mcp.get("denylist", []) or []),
            mcp_persistent_sessions=bool(
                mcp.get("persistent_sessions", cls.mcp_persistent_sessions)
            ),
            mcp_scope=mcp.get("scope", cls.mcp_scope),
            operator_mcp_enabled=operator_mcp.get("enabled", cls.operator_mcp_enabled),
            operator_mcp_tools=list(operator_mcp.get("tools", []) or []),
            operator_mcp_profile=str(operator_mcp.get("profile", "") or "").strip(),
            agent_runtime=str(data.get("agent_runtime", cls.agent_runtime) or "native"),
            developer_channel=str((data.get("developer", {}) or {}).get("channel", cls.developer_channel) or "prod"),
            acp_agents=dict(acp.get("agents", {}) or {}),
            plugins_enabled=list(plugins.get("enabled", []) or []),
            plugins_disabled=list(plugins.get("disabled", []) or []),
            plugins_dir=plugins.get("dir", cls.plugins_dir),
            plugins_allow_unbundled_deps=bool(plugins.get("allow_unbundled_deps", cls.plugins_allow_unbundled_deps)),
            plugins_sources_allow=list((plugins.get("sources", {}) or {}).get("allow", []) or []),
            # ``official``'s absent-vs-explicit-empty distinction is load-bearing
            # (#2691's lesson): absent → the protoLabsAI default; an explicit empty
            # list → "no official sources" (a fork's hardening choice).
            plugins_sources_official=(
                list((plugins.get("sources", {}) or {})["official"] or [])
                if "official" in (plugins.get("sources", {}) or {})
                else ["github.com/protoLabsAI/*"]
            ),
            plugins_sources_acked=list((plugins.get("sources", {}) or {}).get("acked", []) or []),
            plugins_trust_unverified=bool(plugins.get("trust_unverified", cls.plugins_trust_unverified)),
            plugins_update_policy=dict(plugins.get("update_policy", {}) or {}),
            plugins_autoupdate_interval_hours=int(
                plugins.get("autoupdate_interval_hours", cls.plugins_autoupdate_interval_hours) or 0
            ),
            identity_name=identity.get("name", cls.identity_name),
            identity_operator=identity.get("operator", cls.identity_operator),
            identity_org=identity.get("org", cls.identity_org),
            a2a_skills=list(a2a.get("skills", []) or []),
            a2a_description=a2a.get("description", "") or "",
            a2a_require_routable_url=bool(a2a.get("require_routable_url", False)),
            instance_id=data.get("instance", {}).get("id", "") or data.get("instance_id", cls.instance_id),
            auth_token=secret_auth_token or auth.get("token", cls.auth_token),
            federation_token=secret_federation_token or auth.get("federation_token", cls.federation_token),
            secrets_manager_enabled=bool(secrets_manager.get("enabled", cls.secrets_manager_enabled)),
            secrets_manager_provider=str(
                secrets_manager.get("provider", cls.secrets_manager_provider) or "infisical"
            ),
            secrets_manager_host=str(secrets_manager.get("host", cls.secrets_manager_host) or ""),
            secrets_manager_project_id=str(secrets_manager.get("project_id", cls.secrets_manager_project_id) or ""),
            secrets_manager_environment=str(
                secrets_manager.get("environment", cls.secrets_manager_environment) or "prod"
            ),
            secrets_manager_path=str(secrets_manager.get("path", cls.secrets_manager_path) or "/"),
            secrets_manager_recursive=bool(secrets_manager.get("recursive", cls.secrets_manager_recursive)),
            secrets_manager_client_id=secret_sm_client_id
            or secrets_manager.get("client_id", cls.secrets_manager_client_id),
            secrets_manager_client_secret=secret_sm_client_secret
            or secrets_manager.get("client_secret", cls.secrets_manager_client_secret),
            secrets_manager_refresh_seconds=int(
                secrets_manager.get("refresh_seconds", cls.secrets_manager_refresh_seconds) or 0
            ),
            secrets_manager_required=bool(secrets_manager.get("required", cls.secrets_manager_required)),
            secrets_manager_override_env=bool(
                secrets_manager.get("override_env", cls.secrets_manager_override_env)
            ),
            secrets_manager_timeout_seconds=float(
                secrets_manager.get("timeout_seconds", cls.secrets_manager_timeout_seconds) or 10.0
            ),
            publish_endpoint_url=str(publish.get("endpoint_url", cls.publish_endpoint_url) or ""),
            publish_timeout_seconds=float(publish.get("timeout_seconds", cls.publish_timeout_seconds) or 15.0),
            publish_revoke_endpoint_url=str(
                publish.get("revoke_endpoint_url", cls.publish_revoke_endpoint_url) or ""
            ),
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
            fleet_autostart=list(
                fleet.get(
                    "autostart",
                    [s.strip() for s in os.environ.get("PROTOAGENT_FLEET_AUTOSTART", "").split(",") if s.strip()],
                )
                or []
            ),
            # System lifecycle reactions (ADR 0074) — top-level ``lifecycle_hooks:`` list.
            # Opt-in: absent/empty ⇒ [] ⇒ nothing fires beyond the bus broadcast.
            lifecycle_hooks=list(data.get("lifecycle_hooks", []) or []),
            # Managed projects registry (ADR 0095) — top-level ``projects:`` list.
            projects=list(data.get("projects", []) or []),
            onboarding_enabled=bool((data.get("onboarding") or {}).get("enabled", False)),
            onboarding_root=str((data.get("onboarding") or {}).get("root", "") or ""),
            onboarding_allow=list((data.get("onboarding") or {}).get("allow") or []),
            onboarding_write_default=bool((data.get("onboarding") or {}).get("write_default", False)),
            operator_allowed_dirs=list(operator.get("allowed_dirs", []) or []),
            operator_project_dir=str(operator.get("project_dir", "") or ""),
            filesystem_enabled=data.get("filesystem", {}).get("enabled", cls.filesystem_enabled),
            filesystem_allow_run=data.get("filesystem", {}).get("allow_run", _default_filesystem_allow_run()),
            filesystem_run_requires_approval=data.get("filesystem", {}).get(
                "run_requires_approval", cls.filesystem_run_requires_approval
            ),
            filesystem_bypass_allowed=data.get("filesystem", {}).get(
                "bypass_allowed", cls.filesystem_bypass_allowed
            ),
            filesystem_projects=list(data.get("filesystem", {}).get("projects", []) or []),
            media_public=bool((data.get("media", {}) or {}).get("public", cls.media_public)),
            media_retention_days=int(
                (data.get("media", {}) or {}).get("retention_days", cls.media_retention_days) or 0
            ),
            egress_allowed_hosts=list(data.get("egress", {}).get("allowed_hosts", []) or []),
            security_callback_allowlist=list((data.get("security") or {}).get("callback_allowlist", []) or []),
            plugin_config=_resolve_plugin_config(data, secrets, config_dir),
        )

        for name in ("researcher",):
            if name in subagents:
                sub = subagents[name]
                setattr(
                    config,
                    name,
                    SubagentDef(
                        enabled=sub.get("enabled", True),
                        tools=sub.get("tools", getattr(config, name).tools),
                        max_turns=sub.get("max_turns", getattr(config, name).max_turns),
                        model=sub.get("model", getattr(config, name).model),
                    ),
                )

        return config
