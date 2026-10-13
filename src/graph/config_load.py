"""Config LOADER helpers for ``graph.config`` (split out of graph/config.py, #3840).

Everything here is what ``LangGraphConfig.from_yaml`` / ``from_dict`` lean on to turn
files into a merged dict: reading the agent doc + secrets overlay, the Host layer
(ADR 0047) and its cascade merge, plugin-config resolution, external-secrets
hydration, and the scalar coercers / dict helpers ``from_dict`` calls.

``graph.config`` re-exports every name, so ``from graph.config import _x`` and
``patch("graph.config._x")`` keep working. Tests patch ``_load_host_layer`` on
``graph.config``; ``_read_config_docs`` therefore resolves it THROUGH that module at
call time. This module must not import ``graph.config`` at import time (it is imported
BY it) — only lazily, inside a function body.
"""

import copy
import logging
import os
from pathlib import Path

import yaml

log = logging.getLogger("protoagent.config")

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
    import graph.config as _cfg  # call-time: tests patch graph.config._load_host_layer

    host_layer = _cfg._load_host_layer()  # {} when absent/unreadable — never crashes boot
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
        # Captured BEFORE the merge: `_deep_merge_dicts` mutates `host_base` in place and
        # lists REPLACE, so after it runs `host_base["providers"]` IS the agent's list.
        # Reading it afterwards made the merge `(agent, agent)` and silently discarded
        # every box-level connection — the exact inheritance this merge exists to keep.
        host_providers = host_base.get("providers")
        merged = _deep_merge_dicts(host_base, agent_data)
        # A list does not deep-merge: the agent leaf's `providers:` would REPLACE the
        # box's outright, so an instance that adds one local connection would lose every
        # shared one. Merge by id instead — the box supplies the connection, the instance
        # may override any field of it or add its own.
        merged["providers"] = _merge_provider_lists(host_providers, agent_data.get("providers"))
        if (
            not merged["providers"]
            and not isinstance(agent_data.get("providers"), list)
            and not isinstance(host_layer.get("providers"), list)
        ):
            # Nothing declared one at either layer, so leave the key absent and let
            # migration run. If EITHER layer declared it — even as an empty list —
            # the key stays, because "no connections" is an answer.
            merged.pop("providers")
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

# The one home for the auto-trusted-sources default (ADR 0071 D3) — the field's
# default_factory and from_dict's absent-key branch both read it, so a fork
# override or a default change can't drift between the two.
_OFFICIAL_SOURCES_DEFAULT = ("github.com/protoLabsAI/*",)


def _coerce_budget_pct(value, default: float) -> float:
    """``context.budget_pct`` → float (ADR 0108 D6). A blank, missing-value or
    non-numeric entry falls back to the default (warned — the operator's knob
    would otherwise be silently inert); a negative value reads as ``0`` =
    unbounded, the documented off switch."""
    if value is None or (isinstance(value, str) and not value.strip()):
        log.warning("[config] context.budget_pct is blank — using the default %s%%", default)
        return float(default)
    try:
        pct = float(value)
    except (TypeError, ValueError):
        log.warning("[config] context.budget_pct=%r is not a number — using the default %s%%", value, default)
        return float(default)
    return max(pct, 0.0)


def _coerce_prior_sessions(value, default: str) -> str:
    """``context.prior_sessions`` normalized (ADR 0108 D9). A value outside
    ``newest``/``relevant``/``off`` is WARNED but passed through — the config
    layer consumes the key verbatim (test_config_drift_guard's contract); the
    projection layer (``graph.projection._coerce_prior_sessions_policy``) reads
    any unknown value as ``newest``, so a typo can't change what the model sees.
    A blank value falls back to the default.

    YAML 1.1 parses a bare ``off`` as the BOOLEAN False (likewise ``no``/``false``),
    and this knob's off switch is spelled exactly that way — so an operator writing
    the documented ``prior_sessions: off`` handed this function ``False``, which
    ``value or ""`` read as blank and resolved to the DEFAULT. The default is
    ``newest``: the most expensive setting, the opposite of the request, with no
    warning to say so. False-y booleans therefore mean ``off`` here. (The sibling
    ``self_improvement.*`` knobs take the same YAML values and were never immune —
    they landed right only on the ``or "off"`` literal in their own expression, so
    they resolve theirs through :func:`yaml_word_for_bool`; see its docstring.)"""
    if value is False:
        return "off"
    if value is True:
        # `on`/`yes`/`true` — YAML's other booleans. This knob has no such value.
        log.warning(
            "[config] context.prior_sessions=%r is not one of newest|relevant|off — treated as %r "
            "(quote the value if you meant the string)",
            value,
            default,
        )
        return default
    v = str(value or "").strip().lower()
    if not v:
        return default
    if v not in ("newest", "relevant", "off"):
        log.warning(
            "[config] context.prior_sessions=%r is not one of newest|relevant|off — treated as %r",
            value,
            default,
        )
    return v


def _coerce_digest_cap(value, default: int, key: str) -> int:
    """``memory.max_sessions`` / ``memory.max_tokens`` → a positive int.

    Both knobs are ceilings on the ``<prior_sessions>`` digest, and neither has a
    meaningful zero: ``max_sessions: 0`` would be a second, undocumented spelling
    of ``context.prior_sessions: off``, and ``max_tokens: 0`` would trim every
    entry away and render the empty ``<prior_sessions/>`` tag. A non-positive or
    non-numeric value therefore falls back to the default, WARNED — the knob was
    inert for its whole life before #3308, and a silent fallback is how it stayed
    invisible."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        log.warning("[config] %s=%r is not a whole number — using the default %s", key, value, default)
        return default
    if n <= 0:
        log.warning(
            "[config] %s=%s must be positive (it is a ceiling, not a switch) — using the default %s. "
            "To turn the digest off entirely set context.prior_sessions: off.",
            key,
            n,
            default,
        )
        return default
    return n


def yaml_word_for_bool(value):
    """The word an operator wrote, for a STRING knob YAML parsed as a bool (#3253).

    YAML 1.1 reads a bare ``off``/``no``/``false`` as ``False`` and ``on``/``yes``/
    ``true`` as ``True``, so a knob whose values are spelled that way never sees the
    word. ``False`` becomes ``"off"`` and ``True`` becomes ``"on"``; everything else
    — including ``None`` and ``""``, which genuinely mean unset — passes through.

    The ``self_improvement.*`` knobs (``off | propose | auto``) read their value as
    ``str(... or "off")``. That lands ``False`` on ``"off"`` **by coincidence**: the
    literal in the ``or`` fallback happens to be the same word the operator meant.
    Note it is that literal doing the work, not the field defaults — those are
    ``auto``/``propose``/``propose``. So the blank path is one refactor away from the
    ``context.prior_sessions`` bug: replace the magic ``"off"`` with the field's own
    default (the obvious cleanup) and a bare ``off`` on ``soul_md`` silently becomes
    ``auto``. Mapping the word explicitly makes the answer independent of that
    literal, and stops a bare ``on`` from becoming the string ``"True"``."""
    if value is True:
        return "on"
    if value is False:
        return "off"
    return value


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


def _valid_a2a_skills(entries) -> list[dict]:
    """Keep only well-formed A2A card skill specs from ``a2a.skills`` (#2754):
    a dict with truthy ``id``/``name``/``description``, unique by id. The card
    build hard-indexes all three, so a malformed YAML entry must be dropped HERE
    with an attributed warning, not surface as a ``KeyError`` inside the
    boot-time card build. Mirrors ``register_a2a_skill``'s contract for the
    plugin path — the two ingestion routes enforce one rule."""
    kept: list[dict] = []
    for e in entries or []:
        if not isinstance(e, dict) or not e.get("id") or not e.get("name") or not e.get("description"):
            log.warning("a2a.skills: skipping malformed entry (needs id+name+description): %r", e)
            continue
        if any(k["id"] == e["id"] for k in kept):
            log.warning("a2a.skills: duplicate skill id %r — keeping the first", e["id"])
            continue
        kept.append(e)
    return kept


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


def _parse_sources_allow(sources: dict):
    """``plugins.sources.allow`` with the absent-vs-explicit-empty distinction
    (#2743 item 1): absent → ``None`` (open), explicit ``[]`` → deny-all,
    non-empty → the allowlist. An explicit empty list previously meant OPEN —
    warn loudly on the flip so an operator carrying a literal ``allow: []``
    learns their config now hardens instead of silently changing behavior."""
    if "allow" not in sources:
        return None
    allow = [str(x) for x in (sources.get("allow") or [])]
    if not allow:
        import logging

        logging.getLogger("graph.config").warning(  # the logger name it had in graph/config.py
            "[plugins] sources.allow is an EXPLICIT empty list — since #2743 that means "
            "DENY-ALL plugin installs (it used to mean open). Remove the key to allow any "
            "source, or list the origins you trust."
        )
    return allow


def _default_prompt_cache_ttl() -> str:
    """Profile-aware app-default for ``prompt_cache.ttl`` (#2780, ADR 0101 D7).

    The 5m ephemeral tier fits an interactive dev chat, where turns arrive
    faster than the TTL. Fleet members and the packaged desktop app are
    long-lived agents that routinely idle past 5m between turns — every re-warm
    re-pays the full stable prefix uncached, so they default to the 1h
    persistent tier (Anthropic prices 1h writes at 2x base input vs 1.25x for
    5m; one avoided re-warm already covers the premium on these profiles).

    Signals, deliberately coarse: a fleet member runs with ``PROTOAGENT_HOME``
    (its workspace as instance root — ``graph/workspaces/manager.run_exec``);
    the desktop app is a frozen binary. A standalone instance an operator
    relocated via ``PROTOAGENT_HOME`` matches too — same long-lived profile,
    same trade. An explicit ``prompt_cache.ttl`` in config always wins.
    """
    import sys

    if getattr(sys, "frozen", False) or os.environ.get("PROTOAGENT_HOME", "").strip():
        return "1h"
    return "5m"


def _host_scoped_fields():
    """The host-scoped (ADR 0047 ``scope=="host"``) settings fields — the single
    source for both the host-layer filter and the shadow check, so they can't drift."""
    from graph.settings_schema import FIELDS

    return [f for f in FIELDS if getattr(f, "scope", "agent") == "host"]


def _merge_provider_lists(host_entries, agent_entries) -> list:
    """Host connections overlaid by the agent's, matched on ``id`` (ADR 0106).

    Order follows the host list first, then anything the instance adds — stable, so a
    picker built from it does not reshuffle when the box gains a connection."""
    out: list = []
    index: dict[str, int] = {}
    for entry in list(host_entries or []) + list(agent_entries or []):
        if not isinstance(entry, dict):
            continue
        pid = str(entry.get("id", "") or "").strip().lower()
        if not pid:
            continue
        if pid in index:
            out[index[pid]] = {**out[index[pid]], **entry}  # per-field override
        else:
            index[pid] = len(out)
            out.append(dict(entry))
    return out


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
    # `providers:` is not a FIELDS key (it is a list of objects, ADR 0106) but IS
    # box-shared, mirroring the host-scoped `model.api_base` it replaces: one endpoint
    # per box, inherited by every instance on it. Keys are deliberately NOT carried —
    # `model.api_key` is agent-scoped, so a connection's key comes from the instance's
    # own secrets.yaml and the Host file must not be able to hand one out.
    host_providers = raw.get("providers")
    if isinstance(host_providers, list):
        kept = []
        for entry in host_providers:
            if not isinstance(entry, dict):
                continue
            kept.append({k: v for k, v in entry.items() if k != "api_key"})
        if kept:
            out["providers"] = kept
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
