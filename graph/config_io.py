"""Config I/O for the console's live-edit Settings drawer (operator_api/config_routes.py).

Three jobs:

1. **YAML round-trip** that preserves comments and unknown keys in
   ``config/langgraph-config.yaml``. ``LangGraphConfig.from_yaml``
   silently drops anything it doesn't know about, so writing back via
   a freshly-constructed dataclass would wipe fork-added sections
   (e.g. the ``memory`` / ``skills`` blocks the template already
   ships). We use ruamel.yaml when available for comment preservation;
   PyYAML is the fallback.

2. **Two-location SOUL.md handling.** The runtime reads
   ``/sandbox/SOUL.md`` (populated by ``entrypoint.sh`` at container
   start). The source-of-truth lives at ``config/SOUL.md`` in the
   repo. Drawer edits write to both so container restarts preserve
   the change and local-dev runs without a ``/sandbox`` directory
   still pick up the edit.

3. **Gateway introspection.** ``list_gateway_models`` hits
   ``{api_base}/models`` so the drawer's model dropdown reflects
   whatever the connected LiteLLM gateway (or OpenAI-compat endpoint)
   actually exposes — no hardcoded list to drift out of sync.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from graph.config import LangGraphConfig

log = logging.getLogger("protoagent.config_io")

REPO_ROOT = Path(__file__).parent.parent

# Two config roots, normally the same directory:
#
# * BUNDLE  — read-only shipped defaults: the ``.example`` template, SOUL
#   presets, the default SOUL.md. Lives next to the code (``REPO_ROOT/config``,
#   or _MEIPASS/config inside a PyInstaller-frozen sidecar).
# * LIVE    — writable per-deployment state: the live YAML, secrets, and the
#   setup marker. Overridable via ``PROTOAGENT_CONFIG_DIR``.
#
# The desktop sidecar is a read-only frozen binary, so it points
# ``PROTOAGENT_CONFIG_DIR`` at the per-user app-data dir — defaults are read
# from the bundle, live state is written to app-data. Unset (local dev, the
# Docker config volume) collapses both to ``REPO_ROOT/config`` — unchanged.
_BUNDLE_CONFIG_DIR = REPO_ROOT / "config"


def _live_config_dir() -> Path:
    override = os.environ.get("PROTOAGENT_CONFIG_DIR", "").strip()
    return Path(override).expanduser() if override else _BUNDLE_CONFIG_DIR


_LIVE_CONFIG_DIR = _live_config_dir()

# Bundled, read-only defaults.
CONFIG_EXAMPLE_PATH = _BUNDLE_CONFIG_DIR / "langgraph-config.example.yaml"
SOUL_SOURCE_PATH = _BUNDLE_CONFIG_DIR / "SOUL.md"
# SOUL.md starter templates. The wizard offers these as presets the
# user can pick then edit before saving. Adding a new file here
# automatically makes it a choice — no registry to update.
PRESETS_DIR = _BUNDLE_CONFIG_DIR / "soul-presets"

# Writable, per-deployment state.
#
# Live runtime config — untracked, per-deployment. Generated from the
# ``.example`` template on first run (see ``ensure_live_config``) and rewritten
# by the wizard/drawer. Keeping it out of git means setup edits never dirty a
# tracked file; the template carries the shipped defaults + comments.
# Instance-scoped (ADR 0004): a per-instance config when PROTOAGENT_INSTANCE is set, so
# `--instance foo` gets its own config/secrets instead of sharing the default's — a no-op
# (the base path) for the unscoped default. The unscoped base is kept for graceful seeding.
from infra.paths import scope_leaf as _scope_leaf


def _config_scope(path: Path) -> Path:
    """Instance-scoping for the config-dir-relative files (config / secrets /
    setup-marker).

    ``scope_leaf`` isolates co-located instances that SHARE a config dir (the
    bundle dir, or the data home) by ``PROTOAGENT_INSTANCE``. But when
    ``PROTOAGENT_CONFIG_DIR`` is set explicitly — the desktop app's per-user dir,
    or a FLEET MEMBER's per-workspace dir — that dir is ALREADY the isolated leaf.
    Scoping it again double-nests (``<ws>/<id>/secrets.yaml``), so the member
    writes a path its own plugin-config resolver (rooted at ``<ws>/plugins``)
    never reads — a saved plugin secret then reads back ``unset``. So scope the
    leaf only when the config dir is the implicit default.
    """
    if os.environ.get("PROTOAGENT_CONFIG_DIR", "").strip():
        return path
    return _scope_leaf(path)


_BASE_CONFIG_YAML = _LIVE_CONFIG_DIR / "langgraph-config.yaml"
CONFIG_YAML_PATH = _config_scope(_BASE_CONFIG_YAML)
SOUL_RUNTIME_PATH = Path("/sandbox/SOUL.md")

# Secrets overlay. The setup wizard / drawer collect a model API key and an
# A2A bearer token; persisting those into the tracked config YAML means every
# configured checkout carries credentials in git. Instead they live in this
# untracked sibling file (gitignored + dockerignored), read back by
# ``LangGraphConfig.from_yaml`` and stripped from the main YAML on every save.
_BASE_SECRETS_YAML = _LIVE_CONFIG_DIR / "secrets.yaml"
SECRETS_YAML_PATH = _config_scope(_BASE_SECRETS_YAML)

# (section, key) pairs that must never be written to the tracked YAML.
SECRET_PATHS: tuple[tuple[str, str], ...] = (
    ("model", "api_key"),
    ("auth", "token"),
    # The discord (`discord.bot_token`) and google (`google.client_secret`)
    # secrets are now declared by their plugin manifests and added dynamically
    # via secret_paths() (ADR 0019).
)


# Last successfully-discovered plugin secret paths. On a discovery FAILURE we fall back
# to this cache rather than an empty set — otherwise a transient error would stop
# recognizing a plugin's declared secret keys, and strip_secrets_from_doc (which uses
# secret_paths) would let that secret be written into the main, exportable/forkable YAML
# in plaintext (#877). The cache only fails safe (more keys treated as secret).
_PLUGIN_SECRET_PATHS_CACHE: tuple[tuple[str, str], ...] = ()


def secret_paths() -> tuple[tuple[str, str], ...]:
    """Base ``SECRET_PATHS`` plus the (section, key) pairs each INSTALLED plugin
    declares as secrets (ADR 0019). Used by the split/strip logic so a plugin
    secret is routed to ``secrets.yaml`` exactly like the model API key.

    Covers installed-but-DISABLED plugins too: otherwise a secret saved for a plugin
    that's off wouldn't be recognized as a secret and would be written to the live
    config YAML in plaintext (the wrong file — configs get exported/backed-up/forked).

    On a discovery failure, falls back to the last successful set (never an empty one),
    so a transient error can't silently downgrade a plugin secret to plaintext (#877)."""
    global _PLUGIN_SECRET_PATHS_CACHE
    try:
        from graph.plugins.pconfig import installed_plugin_config_schemas

        extra = tuple(
            (sch.section, key) for sch in installed_plugin_config_schemas() for key in sch.secrets
        )
        _PLUGIN_SECRET_PATHS_CACHE = extra  # remember the good set for next time
    except Exception as e:  # noqa: BLE001 — discovery is best-effort; fail SAFE, not empty
        extra = _PLUGIN_SECRET_PATHS_CACHE
        log.warning(
            "[plugins] secret-path discovery failed — keeping %d cached plugin secret "
            "path(s) so a declared secret isn't written to the main YAML in plaintext: %s",
            len(extra),
            e,
        )
    return SECRET_PATHS + extra

# Setup wizard state.
# Presence of this (empty) marker file = wizard has been run and the
# server should boot straight into the chat UI. Absence = show the
# wizard on first page load. Lives in the live config dir so a Docker volume
# mount (or the desktop app-data dir) persists setup across runs.
_BASE_SETUP_MARKER = _LIVE_CONFIG_DIR / ".setup-complete"
SETUP_MARKER_PATH = _config_scope(_BASE_SETUP_MARKER)


# ---------------------------------------------------------------------------
# YAML round-trip
# ---------------------------------------------------------------------------

try:
    from ruamel.yaml import YAML  # type: ignore

    _ruamel = YAML(typ="rt")
    _ruamel.preserve_quotes = True
    _ruamel.indent(mapping=2, sequence=4, offset=2)
    _HAS_RUAMEL = True
except ImportError:
    _HAS_RUAMEL = False


def _reset_double_scoped_config() -> None:
    """Self-heal the double-scope bug (ADR 0042): earlier builds nested a fleet
    member's config under ``<config-dir>/<instance>/`` — ``scope_leaf`` ran on top of
    an already-per-member ``PROTOAGENT_CONFIG_DIR`` — so a saved plugin secret landed
    in a dir the member's own plugin-config resolver (rooted at ``<dir>/plugins``)
    never reads. Config now lives directly under the explicit dir; remove the orphaned
    nested copy (its secrets are re-entered — by design we don't migrate it). No-op
    unless an explicit config dir is set AND the old nested config exists."""
    if not os.environ.get("PROTOAGENT_CONFIG_DIR", "").strip():
        return
    old = _scope_leaf(_BASE_CONFIG_YAML)  # the OLD <dir>/<id>/langgraph-config.yaml
    if old == CONFIG_YAML_PATH or not old.exists():
        return  # not double-scoped on disk (already healed, or never happened)
    for name in ("langgraph-config.yaml", "secrets.yaml", ".setup-complete"):
        try:
            (old.parent / name).unlink()
        except OSError:
            pass
    try:
        old.parent.rmdir()  # only succeeds if now empty — never blow away other state
        log.info("[config] removed orphaned double-scoped config dir %s", old.parent)
    except OSError:
        pass


def ensure_live_config() -> bool:
    """Seed the live config on first run. Returns True only when it created the file.

    The live ``langgraph-config.yaml`` is untracked, so a fresh clone / new container
    volume / **new instance** won't have one. Seed source:

    - An **instance-scoped** path (PROTOAGENT_INSTANCE set) inherits the unscoped *base*
      config + secrets + setup-marker when they exist — so `--instance foo` boots from
      the default's setup, then diverges on its own saves (graceful, no re-setup).
    - Otherwise (or no base yet) copy the tracked ``.example`` template.

    Idempotent — does nothing once the live file exists, so edits are never clobbered.
    """
    _reset_double_scoped_config()  # self-heal: drop the old <dir>/<id>/ nesting
    if CONFIG_YAML_PATH.exists():
        return False
    import shutil

    # Scoped iff the live path actually sits in an instance subdir of the base
    # (PROTOAGENT_INSTANCE set AND no explicit config dir — see `_config_scope`); a
    # fleet member's explicit dir is its own base, so it seeds from the template, not
    # a self-copy.
    scoped = CONFIG_YAML_PATH != _BASE_CONFIG_YAML
    source = _BASE_CONFIG_YAML if (scoped and _BASE_CONFIG_YAML.exists()) else CONFIG_EXAMPLE_PATH
    if not source.exists():
        return False

    CONFIG_YAML_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, CONFIG_YAML_PATH)
    # When seeding a scoped instance from the base, carry its secrets + setup state too,
    # so the instance is usable immediately instead of dropping into the setup wizard.
    if scoped and source == _BASE_CONFIG_YAML:
        if _BASE_SECRETS_YAML.exists() and not SECRETS_YAML_PATH.exists():
            shutil.copyfile(_BASE_SECRETS_YAML, SECRETS_YAML_PATH)
        if _BASE_SETUP_MARKER.exists() and not SETUP_MARKER_PATH.exists():
            shutil.copyfile(_BASE_SETUP_MARKER, SETUP_MARKER_PATH)
    log.info("[config] seeded live config %s from %s", CONFIG_YAML_PATH, source.name)
    return True


def load_yaml_doc(path: Path = CONFIG_YAML_PATH) -> Any:
    """Load the config YAML as a mutable document.

    With ruamel: returns a CommentedMap that preserves comments +
    key order on subsequent dump. Without: returns a plain dict and
    comments are lost on next save (a warning is logged once per
    save so the operator knows).
    """
    if path == CONFIG_YAML_PATH:
        ensure_live_config()
    if not path.exists():
        return {} if not _HAS_RUAMEL else _ruamel.load("{}\n")

    with open(path) as f:
        if _HAS_RUAMEL:
            return _ruamel.load(f) or _ruamel.load("{}\n")
        import yaml
        return yaml.safe_load(f) or {}


def save_yaml_doc(doc: Any, path: Path = CONFIG_YAML_PATH) -> None:
    """Persist the document atomically (temp + rename). Creates parent dirs.

    This file is the single most important one the agent owns — a crash
    mid-dump must never leave a truncated YAML behind, so the dump goes to a
    buffer and lands via ``paths.atomic_write``.
    """
    import io

    from infra.paths import atomic_write

    buf = io.StringIO()
    if _HAS_RUAMEL:
        _ruamel.dump(doc, buf)
    else:
        log.warning(
            "ruamel.yaml not installed — YAML comments in %s will not be "
            "preserved on save. Add `ruamel.yaml>=0.18` to requirements.txt "
            "to fix.", path,
        )
        import yaml
        yaml.safe_dump(doc, buf, sort_keys=False, default_flow_style=False)
    atomic_write(path, buf.getvalue())


# ---------------------------------------------------------------------------
# Config dict <-> dataclass
# ---------------------------------------------------------------------------

def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``src`` into ``dst`` (src wins on leaf conflicts)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def config_to_dict(config: LangGraphConfig) -> dict[str, Any]:
    """Serialize a LangGraphConfig into the nested dict shape the UI works with.

    SINGLE SOURCE (B1): every settings-schema field (``graph.settings_schema.FIELDS``)
    is emitted from its declared ``key -> attr`` mapping, so adding a ``Field``
    auto-serializes it — this function no longer hand-mirrors the schema (it used
    to, and silently drifted: 27 fields were unserialized). Secret-typed fields are
    redacted to ``""`` — the UI only needs to know one is set, and the blank-means-
    unchanged save semantics (``split_secret_updates``) keep the stored secret
    intact when the blank is echoed back. The non-FIELDS legacy keys
    (mcp / knowledge.db_path / skills / plugins / researcher) and the ADR-0019
    plugin sections are layered on explicitly below.
    """
    from graph.settings_schema import FIELDS

    # (A) Schema-driven: every settings-exposed key, from its declared key->attr.
    d: dict[str, Any] = {}
    for f in FIELDS:
        val = "" if f.type == "secret" else getattr(config, f.attr)
        if isinstance(val, (list, tuple)):
            val = list(val)
        cursor = d
        parts = f.key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = val

    # (B) Non-FIELDS legacy keys the UI/round-trip needs but the settings schema
    # doesn't expose. Their attrs still round-trip via from_yaml; they're just not
    # in FIELDS, so they stay explicit.
    r = config.researcher
    _deep_merge(d, {
        "knowledge": {
            "db_path": config.knowledge_db_path,
            # Breaker knobs aren't settings-schema fields (operational, config-only),
            # so emit them here for round-trip completeness.
            "embed_breaker_threshold": config.knowledge_embed_breaker_threshold,
            "embed_breaker_cooldown_s": config.knowledge_embed_breaker_cooldown_s,
            # Same for the chunk-fold floor — max_chars/overlap are settings
            # fields (round-trip via FIELDS); min_chars is config-only.
            "chunk_min_chars": config.knowledge_chunk_min_chars,
        },
        "mcp": {
            "enabled": config.mcp_enabled,
            "servers": list(config.mcp_servers),
            "timeout_seconds": config.mcp_timeout_seconds,
            "denylist": list(config.mcp_denylist),
        },
        "skills": {
            "enabled": config.skills_enabled,
            "db_path": config.skills_db_path,
            "dir": config.skills_dir,
        },
        # Every plugins.* key from_dict consumes must be emitted here, or any
        # consumer treating this dict as the COMPLETE config silently loses it
        # (the YAML file itself was never at risk — apply_updates_to_yaml merges
        # in place). `disabled` + `sources.allow` were omitted until the
        # 2026-06-10 prod-readiness audit (N6); plugin-hardening P1 writes
        # `sources.*`, so the dict has to carry them.
        "plugins": {
            "enabled": list(config.plugins_enabled),
            "disabled": list(config.plugins_disabled),
            "dir": config.plugins_dir,
            "sources": {"allow": list(config.plugins_sources_allow)},
        },
        "subagents": {
            "researcher": {
                "enabled": r.enabled,
                "tools": list(r.tools),
                "max_turns": r.max_turns,
                "model": r.model,
            },
        },
    })

    # (C) Plugin-declared sections (ADR 0019) — reflect the PASSED config's resolved
    # plugin_config (not a re-discovery), with declared secrets redacted
    # (blank-means-unchanged, like api_key). A default config has none.
    plugin_cfg = getattr(config, "plugin_config", {}) or {}
    if plugin_cfg:
        try:
            # ALL installed plugins (not just enabled) — so a disabled plugin's stored
            # secret is redacted from the API response too, never echoed back in the clear.
            from graph.plugins.pconfig import installed_plugin_config_schemas

            secrets_by_section = {s.section: set(s.secrets) for s in installed_plugin_config_schemas()}
        except Exception:  # noqa: BLE001
            secrets_by_section = {}
        for section, vals in plugin_cfg.items():
            redacted = dict(vals)
            for skey in secrets_by_section.get(section, set()):
                if skey in redacted:
                    redacted[skey] = ""
            d[section] = redacted
    return d


def pop_keys_from_yaml(doc: Any, dotted_keys: list[str]) -> Any:
    """Delete each dotted key (``"model.name"``, ``"prompt_cache.warm.enabled"``)
    from the loaded YAML document, pruning any parent maps left empty by the
    deletion.

    Reset-to-inherited (ADR 0047 slice 3): removing a key from the leaf doc makes
    the cascade fall back to the Host/App layer for that field. Preserves comments +
    key order on the surviving sections (mutates the ruamel ``CommentedMap`` in
    place). A key that isn't present is silently skipped — reset is idempotent.
    """
    for dotted in dotted_keys:
        parts = dotted.split(".")
        # Walk to the leaf's parent, remembering the chain so we can prune
        # now-empty ancestors after the delete.
        chain: list[tuple[Any, str]] = []
        cur = doc
        ok = True
        for part in parts[:-1]:
            if not isinstance(cur, dict) or part not in cur or not isinstance(cur.get(part), dict):
                ok = False
                break
            chain.append((cur, part))
            cur = cur[part]
        if not ok or not isinstance(cur, dict) or parts[-1] not in cur:
            continue
        del cur[parts[-1]]
        # Prune empty parents from the deepest outward.
        for parent, key in reversed(chain):
            child = parent.get(key)
            if isinstance(child, dict) and not child:
                del parent[key]
            else:
                break
    return doc


def apply_updates_to_yaml(doc: Any, updates: dict[str, Any]) -> Any:
    """Merge a nested updates dict into the loaded YAML document.

    Uses __setitem__ on whatever container ruamel loaded (CommentedMap
    acts like dict), so comments / key order / unknown sections are
    preserved. Keys that don't exist yet get added at the end of the
    containing section.
    """
    for section, values in updates.items():
        if not isinstance(values, dict):
            doc[section] = values
            continue
        if section not in doc or not isinstance(doc.get(section), dict):
            doc[section] = {}
        for key, val in values.items():
            if isinstance(val, dict):
                if key not in doc[section] or not isinstance(doc[section].get(key), dict):
                    doc[section][key] = {}
                for inner_key, inner_val in val.items():
                    doc[section][key][inner_key] = inner_val
            else:
                doc[section][key] = val
    return doc


# ---------------------------------------------------------------------------
# Secrets overlay
# ---------------------------------------------------------------------------


def split_secret_updates(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a UI config dict into (non-secret, secret) halves.

    Secret fields (``SECRET_PATHS``) are pulled out of the returned
    non-secret dict so they never reach the tracked YAML. Only *non-blank*
    secret values are routed to the secret half — a blank value means "leave
    the stored secret unchanged" (the UI sends blank when the user didn't
    re-enter a key), so it's dropped entirely rather than clobbering.
    """
    import copy

    main = copy.deepcopy(config)
    secrets: dict[str, Any] = {}
    for section, key in secret_paths():
        sect = main.get(section)
        if not isinstance(sect, dict) or key not in sect:
            continue
        value = sect.pop(key)
        if isinstance(value, str) and value.strip():
            secrets.setdefault(section, {})[key] = value.strip()
        # Drop the section entirely if popping the secret emptied it, so we
        # don't write an empty `auth: {}` block to the main YAML.
        if not sect:
            main.pop(section, None)
    return main, secrets


def strip_secrets_from_doc(doc: Any) -> Any:
    """Remove any secret keys already present in the main YAML document.

    Belt-and-suspenders alongside ``split_secret_updates``: even if an older
    YAML still carries an ``api_key`` (or a hand-edit reintroduces one), every
    save scrubs it so the tracked file converges to secret-free.
    """
    for section, key in secret_paths():
        sect = doc.get(section) if hasattr(doc, "get") else None
        if isinstance(sect, dict) and key in sect:
            del sect[key]
        if isinstance(sect, dict) and not sect:
            try:
                del doc[section]
            except (KeyError, TypeError):
                pass
    return doc


def load_secrets() -> dict[str, Any]:
    """Load the untracked secrets overlay (empty dict if absent/unreadable)."""
    if not SECRETS_YAML_PATH.exists():
        return {}
    import yaml as _yaml

    try:
        with open(SECRETS_YAML_PATH) as f:
            data = _yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, _yaml.YAMLError):
        return {}


def save_secrets(secret_updates: dict[str, Any]) -> None:
    """Merge non-blank secret updates into the untracked secrets file.

    Written with mode 0600 (owner-only). Merges rather than overwrites so a
    save that only changes the API key doesn't drop a stored bearer token.
    """
    if not secret_updates:
        return
    import os
    import yaml as _yaml

    current = load_secrets()
    for section, values in secret_updates.items():
        if not isinstance(values, dict):
            continue
        dest = current.setdefault(section, {})
        for key, val in values.items():
            dest[key] = val

    SECRETS_YAML_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS_YAML_PATH.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        _yaml.safe_dump(current, f, sort_keys=False, default_flow_style=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SECRETS_YAML_PATH)


def validate_config_dict(updates: dict[str, Any]) -> tuple[bool, str]:
    """Validate without persisting. Returns (ok, error-message).

    Catches type mismatches and obvious range errors before we touch
    disk or rebuild the graph.
    """
    try:
        model = updates.get("model", {})
        temp = float(model.get("temperature", 0.2))
        if not 0.0 <= temp <= 2.0:
            return False, f"temperature must be 0.0-2.0, got {temp}"
        max_tokens = int(model.get("max_tokens", 4096))
        if max_tokens < 1:
            return False, f"max_tokens must be >= 1, got {max_tokens}"
        max_iter = int(model.get("max_iterations", 50))
        if max_iter < 1:
            return False, f"max_iterations must be >= 1, got {max_iter}"

        researcher = updates.get("subagents", {}).get("researcher", {})
        if researcher:
            max_turns = int(researcher.get("max_turns", 40))
            if max_turns < 1:
                return False, f"researcher.max_turns must be >= 1, got {max_turns}"
            tools = researcher.get("tools", [])
            if not isinstance(tools, list):
                return False, "researcher.tools must be a list"

        knowledge = updates.get("knowledge", {})
        if knowledge:
            top_k = int(knowledge.get("top_k", 5))
            if top_k < 1:
                return False, f"knowledge.top_k must be >= 1, got {top_k}"

        operator = updates.get("operator", {})
        if operator:
            allowed = operator.get("allowed_dirs", [])
            if not isinstance(allowed, list) or not all(isinstance(d, str) for d in allowed):
                return False, "operator.allowed_dirs must be a list of strings"
    except (TypeError, ValueError) as e:
        return False, f"config validation: {e}"
    return True, ""


# ---------------------------------------------------------------------------
# SOUL.md
# ---------------------------------------------------------------------------


def read_soul() -> str:
    """Return the current persona text.

    Prefers the runtime path (``/sandbox/SOUL.md``) since that's what
    ``graph/prompts.build_system_prompt`` actually reads; falls back
    to the repo source so local-dev picks it up even when no sandbox
    volume is mounted.
    """
    for path in (SOUL_RUNTIME_PATH, SOUL_SOURCE_PATH):
        if path.exists():
            return path.read_text(encoding="utf-8")
    return ""


def write_soul(text: str) -> list[Path]:
    """Write persona text to every reachable SOUL.md path.

    Always writes the repo source (``config/SOUL.md``). Additionally
    writes the runtime path if its parent directory exists — in the
    container ``/sandbox`` is created by Dockerfile; in local dev it
    usually isn't, so we skip quietly instead of erroring.

    Returns the paths that were written for UI feedback.
    """
    written: list[Path] = []
    SOUL_SOURCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOUL_SOURCE_PATH.write_text(text, encoding="utf-8")
    written.append(SOUL_SOURCE_PATH)

    if SOUL_RUNTIME_PATH.parent.exists():
        SOUL_RUNTIME_PATH.write_text(text, encoding="utf-8")
        written.append(SOUL_RUNTIME_PATH)

    return written


# ---------------------------------------------------------------------------
# Gateway model discovery
# ---------------------------------------------------------------------------


def list_gateway_models(
    api_base: str,
    api_key: str = "",
    timeout: float = 10.0,
) -> tuple[list[str], str]:
    """Fetch the model list from ``{api_base}/models``.

    Works against any OpenAI-compatible endpoint — LiteLLM gateway,
    OpenAI proper, vLLM, Ollama with the OpenAI adapter. Returns
    ``(model_ids, error_message)``. On success ``error_message`` is
    empty; on failure model_ids is empty and the message is human-
    readable.
    """
    import httpx

    if not api_base:
        return [], "api_base is empty"

    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    url = api_base.rstrip("/") + "/models"
    # SSRF guard (#871): an operator-supplied api_base must not steer this probe at an
    # internal service or cloud-metadata (169.254.169.254). Blocked unless explicitly
    # allowlisted via egress.allowed_hosts.
    from security import egress

    if egress.check_url(url):
        return [], "api_base host is blocked by the egress guard (set egress.allowed_hosts to permit it)"
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return [], f"connection failed: {e}"

    if resp.status_code >= 400:
        # Don't echo the raw upstream body (#871) — just the status.
        return [], f"HTTP {resp.status_code} from the gateway's /models"

    try:
        data = resp.json()
    except ValueError:
        return [], f"non-JSON response from {url}"

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [], f"unexpected shape from {url} — no 'data' array"

    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("name")
            if isinstance(model_id, str):
                ids.append(model_id)
    ids.sort()
    return ids, ""


def validate_model_connection(
    api_base: str,
    api_key: str = "",
    model: str = "",
    timeout: float = 20.0,
) -> tuple[bool, str]:
    """Probe the model with a minimal real completion — the *true* auth check.

    ``list_gateway_models`` only hits ``/models``, which gateways answer for
    keys that can't actually run a completion (e.g. the proto-labs LiteLLM
    gateway lists models but rejects a non-``sk-`` virtual key at
    ``/chat/completions`` with a 401). This sends a 1-token completion down the
    same path the agent uses, so a bad key / wrong model / unreachable gateway
    is caught *before* setup completes instead of surfacing as a cryptic failed
    chat turn. Returns ``(ok, error_message)`` — the message is the gateway's
    own human-readable detail (e.g. "expected to start with 'sk-'") when it has
    one, so the UI can show something actionable.
    """
    import httpx

    if not api_base:
        return False, "api_base is empty"
    if not model:
        return False, "model is empty"

    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    url = api_base.rstrip("/") + "/chat/completions"
    # SSRF guard (#871) — same as list_gateway_models.
    from security import egress

    if egress.check_url(url):
        return False, "api_base host is blocked by the egress guard (set egress.allowed_hosts to permit it)"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        return False, f"connection failed: {e}"

    if resp.status_code < 400:
        return True, ""

    # Surface the gateway's own error message when present (OpenAI-shaped
    # ``{"error": {"message": ...}}``), else the raw body, capped.
    detail = ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            detail = err.get("message") or ""
        elif isinstance(err, str):
            detail = err
    except ValueError:
        detail = ""
    if not detail:
        detail = (resp.text or "")[:300]
    # Sanitize: gateways (e.g. LiteLLM) dump the masked key, a token *hash*, and
    # internal table names into auth errors — never echo those into the setup UI.
    # Keep the leading human-readable cause, drop everything from the first
    # secret-ish marker on, and cap the length.
    import re as _re
    detail = _re.split(
        r"\s*(?:Received API Key|Key Hash|Unable to find token|Token=)",
        detail, maxsplit=1,
    )[0].strip().rstrip(".,")
    detail = detail[:200]
    if resp.status_code in (401, 403) and not detail:
        detail = "authentication failed — check the API key"
    return False, f"HTTP {resp.status_code}: {detail}" if detail else f"HTTP {resp.status_code}"


# ---------------------------------------------------------------------------
# Tool registry introspection
# ---------------------------------------------------------------------------


def list_available_tools(knowledge_store: Any = None) -> list[str]:
    """Return every tool name the runtime *could* wire into the graph.

    The wizard's tool checkbox group reads this. We deliberately
    expose the scheduler tool names even when no scheduler has been
    constructed yet (fresh boot, pre-setup) — otherwise the wizard
    would hide tools that the runtime will register the moment the
    user finishes setup. Same logic for memory tools when the
    knowledge store is absent.
    """
    from tools.lg_tools import (
        INBOX_TOOL_NAMES,
        MEMORY_TOOL_NAMES,
        SCHEDULER_TOOL_NAMES,
        get_all_tools,
    )

    names = [t.name for t in get_all_tools(knowledge_store)]
    # Deduplicate while preserving order: tools already present
    # (because their backend was passed in) shouldn't appear twice.
    seen = set(names)
    for extra in (*MEMORY_TOOL_NAMES, *SCHEDULER_TOOL_NAMES, *INBOX_TOOL_NAMES):
        if extra not in seen:
            names.append(extra)
            seen.add(extra)
    return names


# ---------------------------------------------------------------------------
# Setup wizard state
# ---------------------------------------------------------------------------


def is_setup_complete() -> bool:
    """True once the wizard has been completed at least once.

    Checked at server boot to decide wizard-first vs chat-first
    rendering. Don't read the YAML to infer this — a fork that ships
    with a baked-in config still needs to walk a user through the
    wizard on first run.
    """
    return SETUP_MARKER_PATH.exists()


def mark_setup_complete() -> None:
    """Write the marker so subsequent boots skip the wizard.

    Idempotent — safe to call repeatedly. The file is empty; only
    its presence matters.
    """
    SETUP_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETUP_MARKER_PATH.touch()


def validate_for_headless(config) -> tuple[bool, str]:
    """Can a headless tier compile the graph from this config without a wizard?

    Returns ``(ok, reason)`` (ADR 0010). Requires a model endpoint and a
    resolvable key — the wizard's job, done here from config + env instead.
    Used by ``--setup`` and the boot-time auto-complete; on failure the caller
    fails fast rather than marking a broken config complete.
    """
    # ACP-only setup (ADR 0033): an external coding agent drives turns + backs aux calls,
    # so no OpenAI-compatible gateway is required. (Semantic recall degrades to keyword
    # without an embed endpoint — fine, not a blocker.)
    if str(getattr(config, "agent_runtime", "native") or "native").startswith("acp:"):
        return True, "ok"

    if not str(getattr(config, "api_base", "") or "").strip():
        return False, "model.api_base is not set"
    key = str(getattr(config, "api_key", "") or "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return False, "no model api_key — set model.api_key in config/secrets.yaml or the OPENAI_API_KEY env var"
    return True, "ok"


def reset_setup() -> None:
    """Remove the marker, forcing the wizard to run on next page load.

    Exposed to the drawer as a "Re-run setup" action. Leaves the YAML
    + SOUL.md in place so the wizard pre-populates with the current
    values — reset is for revisiting choices, not for wiping config.
    """
    SETUP_MARKER_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# SOUL.md presets
# ---------------------------------------------------------------------------


def list_soul_presets() -> list[str]:
    """Return preset names (file stems, no extension) sorted alphabetically.

    The wizard's preset dropdown reads from this — dropping a new
    markdown file into ``config/soul-presets/`` makes it a choice
    without code changes.
    """
    if not PRESETS_DIR.exists():
        return []
    return sorted(p.stem for p in PRESETS_DIR.glob("*.md"))


def read_soul_preset(name: str) -> str:
    """Return the preset's content.

    Returns empty string for an unknown name rather than raising —
    the wizard treats that as "no preset selected, blank canvas".

    Path-traversal guarded: the resolved target must live inside
    ``PRESETS_DIR``. A name like ``"../secret"`` would otherwise
    escape the presets directory and read arbitrary ``.md`` files
    anywhere the process can reach.
    """
    presets_root = PRESETS_DIR.resolve()
    candidate = (PRESETS_DIR / f"{name}.md").resolve()
    if presets_root not in candidate.parents or not candidate.is_file():
        return ""
    return candidate.read_text(encoding="utf-8")
