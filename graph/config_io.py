"""Config I/O for the console's live-edit Settings drawer (operator_api/config_routes.py).

Three jobs:

1. **YAML round-trip** that preserves comments and unknown keys in
   ``config/langgraph-config.yaml``. ``LangGraphConfig.from_yaml``
   silently drops anything it doesn't know about, so writing back via
   a freshly-constructed dataclass would wipe fork-added sections
   (e.g. the ``memory`` / ``skills`` blocks the template already
   ships). We use ruamel.yaml when available for comment preservation;
   PyYAML is the fallback.

2. **SOUL.md persona.** The live persona lives at the instance's
   ``<instance_root>/config/SOUL.md`` (``instance_paths().soul_path``);
   drawer edits write there and ``read_soul`` falls back to the bundled
   ``config/SOUL.md`` seed when the instance has none yet.

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
from infra.paths import instance_paths

log = logging.getLogger("protoagent.config_io")


# ── Path accessors (resolved at call time from infra.paths.instance_paths) ───
# Every config-dir location is derived from the frozen ``InstancePaths`` object
# on each call — never captured at import time (the old import-time constants
# read ``PROTOAGENT_*`` before the env was finalized, the fragility this cutover
# removes). ``instance_root`` IS the per-instance scoped leaf, so there is no
# ``scope_leaf`` / double-scope dance here: config / secrets / setup-marker /
# theme / SOUL all sit directly under ``<instance_root>/config``. The bundled,
# read-only seeds (``.example`` template, default SOUL.md, presets) live in the
# App tier under ``app_root/config``.


def config_yaml_path() -> Path:
    """The live agent config YAML — ``<instance_root>/config/langgraph-config.yaml``.

    Untracked + per-instance: generated from the ``.example`` template on first
    run (see ``ensure_live_config``) and rewritten by the wizard/drawer."""
    return instance_paths().config_yaml


def secrets_yaml_path() -> Path:
    """Untracked secrets overlay sibling of the config YAML — the model API key
    and A2A bearer live here (gitignored + dockerignored), never in the tracked
    YAML, read back by ``LangGraphConfig.from_yaml`` and stripped on every save."""
    return instance_paths().secrets_yaml


def setup_marker_path() -> Path:
    """Setup-complete marker — presence ⇒ the wizard has been run."""
    return instance_paths().setup_marker


def theme_json_path() -> Path:
    """Per-agent console theme file (ADR 0042)."""
    return instance_paths().theme_json


def config_example_path() -> Path:
    """Bundled, read-only ``.example`` template (App-tier seed)."""
    return instance_paths().config_example


def soul_source_path() -> Path:
    """Bundled, read-only default ``SOUL.md`` (App-tier seed)."""
    return instance_paths().soul_source


def presets_dir() -> Path:
    """Bundled ``SOUL.md`` starter-presets dir (App-tier seed). Dropping a new
    markdown file in makes it a wizard choice — no registry to update."""
    return instance_paths().presets_dir


# (section, key) pairs that must never be written to the tracked YAML.
SECRET_PATHS: tuple[tuple[str, str], ...] = (
    ("model", "api_key"),
    ("auth", "token"),
    # Plugin secrets (e.g. discord's `discord.bot_token`) are declared by their
    # plugin manifests and added dynamically via secret_paths() (ADR 0019).
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

        extra = tuple((sch.section, key) for sch in installed_plugin_config_schemas(strict=True) for key in sch.secrets)
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


# Files that make up the live config tier — migrated as a unit by the one-shot
# legacy-layout bridge below.
_MIGRATED_CONFIG_FILES = ("langgraph-config.yaml", "secrets.yaml", ".setup-complete", "theme.json")

# Per-instance DATA stores that lived flat under the box root in the old layout
# (``~/.protoagent/checkpoints.db``, ``~/.protoagent/knowledge``, …) and now live
# under ``instance_root`` (``~/.protoagent/default/...``). The store-tier bridge
# below carries them for the DEFAULT instance only. Box-tier shared state
# (host-config.yaml, commons, .instances, .data-version, workspaces) is deliberately
# NOT in either list — it stays at the box root, shared by every instance.
_LEGACY_STORE_FILES = ("checkpoints.db", "telemetry.db", "skills.db", "a2a-tasks.db", "a2a-push.db")
_LEGACY_STORE_DIRS = (
    "knowledge",
    "memory",
    "scheduler",
    "inbox",
    "background",
    "activity",
    "audit",
    "tasks",
    "workflows",
    "acp_sessions",
    "goals",
    "workspace",
)


def _legacy_config_dirs() -> list[Path]:
    """Old-layout directories that may hold this instance's pre-redesign config, in
    priority order. Computed independently of the (now-deleted) legacy resolvers.

    Two shapes cover every deployment:
      * flat under the instance root — ``<instance_root>/langgraph-config.yaml`` (the
        desktop ``PROTOAGENT_HOME`` dir and a fleet member's ``<ws>`` dir used to hold
        the config file directly), and
      * the bundle/repo config dir ``<app_root>/config[/<iid>]`` (local default in
        ``REPO/config``, the dev sandbox in ``REPO/config/dev``, the container in
        ``/opt/protoagent/config``), plus an explicit ``PROTOAGENT_CONFIG_DIR`` when the
        upgrading environment still exports the retired var.
    """
    p = instance_paths()
    out: list[Path] = [p.instance_root]
    cd = os.environ.get("PROTOAGENT_CONFIG_DIR", "").strip()
    if cd:
        out.append(Path(cd).expanduser())
    else:
        base = p.app_root / "config"
        iid = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
        out.append(base / iid if iid else base)
    cfg = p.config_dir.resolve()
    return [d for d in out if d.resolve() != cfg]


def migrate_legacy_layout() -> bool:
    """One-shot, idempotent, non-destructive bridge from the pre-redesign on-disk
    layout into the new ``instance_root/config``. Returns True if it copied anything.

    Runs only when the new live config is ABSENT, then copies an old config bundle
    (``langgraph-config.yaml`` + ``secrets.yaml`` + ``.setup-complete`` + ``theme.json``)
    from the first legacy dir that has one — so a build upgraded in place keeps its
    config, secrets and setup state with no user action. Copy (never move) leaves the
    originals as harmless orphans; ``copy2`` preserves ``secrets.yaml``'s 0600 mode. A
    no-op once migrated. Self-contained and deletable in a future major — the path
    *resolver* stays single-rule; this is the only bridge from the old layout.
    """
    import shutil

    p = instance_paths()
    if p.config_yaml.exists():
        return False
    migrated = False
    for src_dir in _legacy_config_dirs():
        if not (src_dir / "langgraph-config.yaml").is_file():
            continue
        p.config_dir.mkdir(parents=True, exist_ok=True)
        for name in _MIGRATED_CONFIG_FILES:
            src, dst = src_dir / name, p.config_dir / name
            if src.is_file() and not dst.exists():
                shutil.copy2(src, dst)  # copy2 keeps secrets.yaml's 0600 mode
                migrated = True
        log.warning(
            "[config] migrated legacy config layout %s → %s (one-time; the originals are "
            "left untouched and can be removed once you've confirmed the upgrade)",
            src_dir,
            p.config_dir,
        )
        break
    # The container's old runtime SOUL (entrypoint used to write /sandbox/SOUL.md).
    legacy_soul = Path("/sandbox/SOUL.md")
    if legacy_soul.is_file() and not p.soul_path.exists():
        p.soul_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_soul, p.soul_path)
        migrated = True
    # Carry the default instance's data stores into the new instance_root (first
    # boot only — gated by the same absent-live-config condition above so a store
    # the operator later clears is never resurrected). Only the DEFAULT instance
    # auto-migrates; scoped sandboxes (e.g. the dev instance) re-init from scratch.
    migrated = _migrate_legacy_stores(p) or migrated
    return migrated


def _migrate_legacy_stores(p) -> bool:
    """One-shot, idempotent, non-destructive copy of the pre-redesign data stores from
    the flat box root into ``instance_root`` (``box_root/<store>`` → ``box_root/default/
    <store>``). Returns True if it copied anything.

    Only runs for the standard local DEFAULT instance (``instance_id == "default"`` and
    ``box_root`` is the parent of ``instance_root``) — a ``PROTOAGENT_HOME`` deploy or a
    named/dev instance has a distinct root and re-inits rather than inheriting the
    default's data. Copy (never move); skip any store whose destination already exists.
    Box-tier shared state is never touched. Best-effort — a copy failure must never
    block boot."""
    import shutil

    if p.instance_id != "default" or p.box_root != p.instance_root.parent:
        return False
    moved: list[str] = []
    for name in _LEGACY_STORE_FILES:
        src, dst = p.box_root / name, p.instance_root / name
        if src.is_file() and not dst.exists():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                moved.append(name)
            except OSError as exc:
                log.warning("[migrate] could not carry store file %s: %s", name, exc)
    for name in _LEGACY_STORE_DIRS:
        src, dst = p.box_root / name, p.instance_root / name
        if src.is_dir() and not dst.exists():
            try:
                shutil.copytree(src, dst)
                moved.append(f"{name}/")
            except OSError as exc:
                log.warning("[migrate] could not carry store dir %s: %s", name, exc)
    if moved:
        log.warning(
            "[migrate] carried default-instance data stores into %s (one-time; originals "
            "left untouched, removable once you've confirmed the upgrade): %s",
            p.instance_root,
            ", ".join(moved),
        )
    return bool(moved)


def ensure_live_config() -> bool:
    """Seed the live config on first run. Returns True only when it created the file.

    The live ``langgraph-config.yaml`` is untracked, so a fresh clone / new container
    volume / **new instance** won't have one. Seed source, in precedence order:

    - ``PROTOAGENT_SEED_CONFIG`` — an explicit baked config file. A container/fleet
      deploy points at it so a fresh instance comes up **pre-configured** (then
      console edits override it, persisted on the config volume). This is the
      config-as-code seed: bake your config into the image, point this env at it, and
      you never hand-bake the live ``langgraph-config.yaml`` (which a config volume
      would then freeze + shadow on later image updates).
    - Otherwise copy the bundled ``.example`` template (``config_example_path()``).

    Idempotent — does nothing once the live file exists, so edits are never clobbered.
    """
    # Bridge an in-place upgrade first: if an old-layout config exists, copy it into the
    # new instance_root/config so the seed-from-.example branch below never strands it.
    migrate_legacy_layout()
    live = config_yaml_path()
    if live.exists():
        return False
    import shutil

    # An explicit baked seed (PROTOAGENT_SEED_CONFIG) wins over the bundled .example.
    # Missing/blank env → seed from the template.
    seed_override = os.environ.get("PROTOAGENT_SEED_CONFIG", "").strip()
    seed_path = Path(seed_override).expanduser() if seed_override else None
    if seed_path is not None and seed_path.is_file():
        source = seed_path
    else:
        if seed_override:
            log.warning(
                "[config] PROTOAGENT_SEED_CONFIG=%r is not a readable file — "
                "seeding from the default template instead", seed_override
            )
        source = config_example_path()
    if not source.exists():
        return False

    live.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, live)
    log.info("[config] seeded live config %s from %s", live, source.name)
    return True


def load_yaml_doc(path: Path | None = None) -> Any:
    """Load the config YAML as a mutable document.

    ``path`` defaults to ``config_yaml_path()`` (resolved at call time, never an
    import-time constant). With ruamel: returns a CommentedMap that preserves
    comments + key order on subsequent dump. Without: returns a plain dict and
    comments are lost on next save (a warning is logged once per save so the
    operator knows).
    """
    resolved = Path(path) if path is not None else config_yaml_path()
    if resolved == config_yaml_path():
        ensure_live_config()
    if not resolved.exists():
        return {} if not _HAS_RUAMEL else _ruamel.load("{}\n")

    with open(resolved) as f:
        if _HAS_RUAMEL:
            return _ruamel.load(f) or _ruamel.load("{}\n")
        import yaml

        return yaml.safe_load(f) or {}


def save_yaml_doc(doc: Any, path: Path | None = None) -> None:
    """Persist the document atomically (temp + rename). Creates parent dirs.

    ``path`` defaults to ``config_yaml_path()``. This file is the single most
    important one the agent owns — a crash mid-dump must never leave a truncated
    YAML behind, so the dump goes to a buffer and lands via ``paths.atomic_write``.
    """
    import io

    from infra.paths import atomic_write

    resolved = Path(path) if path is not None else config_yaml_path()
    buf = io.StringIO()
    if _HAS_RUAMEL:
        _ruamel.dump(doc, buf)
    else:
        log.warning(
            "ruamel.yaml not installed — YAML comments in %s will not be "
            "preserved on save. Add `ruamel.yaml>=0.18` to requirements.txt "
            "to fix.",
            resolved,
        )
        import yaml

        yaml.safe_dump(doc, buf, sort_keys=False, default_flow_style=False)
    atomic_write(resolved, buf.getvalue())


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
    _deep_merge(
        d,
        {
            # background_keep is an operational knob (not a settings-schema field),
            # so emit it here for round-trip completeness like the breaker knobs.
            "checkpoint": {
                "background_keep": config.checkpoint_background_keep,
            },
            "knowledge": {
                "db_path": config.knowledge_db_path,
                # Breaker knobs aren't settings-schema fields (operational, config-only),
                # so emit them here for round-trip completeness.
                "embed_breaker_threshold": config.knowledge_embed_breaker_threshold,
                "embed_breaker_cooldown_s": config.knowledge_embed_breaker_cooldown_s,
                # Same for the chunk-fold floor — max_chars/overlap are settings
                # fields (round-trip via FIELDS); min_chars is config-only.
                "chunk_min_chars": config.knowledge_chunk_min_chars,
                # contextual_enrichment is a settings field; its doc cap is config-only.
                "context_max_doc_chars": config.knowledge_context_max_doc_chars,
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
        },
    )

    # (C) Plugin-declared sections (ADR 0019) — reflect the PASSED config's resolved
    # plugin_config (not a re-discovery), with declared secrets redacted
    # (blank-means-unchanged, like api_key). A default config has none.
    plugin_cfg = getattr(config, "plugin_config", {}) or {}
    if plugin_cfg:
        discovery_ok = True
        try:
            # ALL installed plugins (not just enabled) — so a disabled plugin's stored
            # secret is redacted from the API response too, never echoed back in the clear.
            from graph.plugins.pconfig import installed_plugin_config_schemas

            secrets_by_section = {s.section: set(s.secrets) for s in installed_plugin_config_schemas(strict=True)}
        except Exception:  # noqa: BLE001 — discovery FAILED (vs genuinely-empty): we no longer know which keys are secret
            discovery_ok = False
            secrets_by_section = {}
        for section, vals in plugin_cfg.items():
            if not discovery_ok:
                # Fail SAFE: with no schema we can't tell a secret from a non-secret
                # value, so blank the WHOLE section rather than risk echoing a plugin
                # secret in the clear (blank-means-unchanged on save, like api_key).
                d[section] = {k: "" for k in vals}
                continue
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
    secrets_path = secrets_yaml_path()
    if not secrets_path.exists():
        return {}
    import yaml as _yaml

    try:
        with open(secrets_path) as f:
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

    secrets_path = secrets_yaml_path()
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = secrets_path.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        _yaml.safe_dump(current, f, sort_keys=False, default_flow_style=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, secrets_path)


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
            if "project_dir" in operator and not isinstance(operator["project_dir"], str):
                return False, "operator.project_dir must be a string"
    except (TypeError, ValueError) as e:
        return False, f"config validation: {e}"
    return True, ""


# ---------------------------------------------------------------------------
# SOUL.md
# ---------------------------------------------------------------------------


def read_soul() -> str:
    """Return the current persona text.

    Reads the instance's live ``SOUL.md`` (``instance_paths().soul_path``, what
    ``graph/prompts.build_system_prompt`` resolves), falling back to the bundled
    seed (``soul_source_path()``) when the instance hasn't written one yet.
    """
    for path in (instance_paths().soul_path, soul_source_path()):
        if path.exists():
            return path.read_text(encoding="utf-8")
    return ""


def write_soul(text: str) -> list[Path]:
    """Write persona text to the instance's live ``SOUL.md`` (mkdir parents).

    Returns the path(s) written for UI feedback.
    """
    target = instance_paths().soul_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return [target]


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
    # SSRF guard (#871): an operator-supplied api_base must not steer this probe at
    # cloud-metadata (169.254.169.254) or another link-local/reserved address. But a
    # custom api_base is an OPERATOR-configured gateway — overwhelmingly localhost
    # (Ollama / LM Studio / local vLLM / LiteLLM) or a LAN/tailnet host — so allow_private
    # (same as the fleet-remote probe in graph/fleet/supervisor.py) while STILL blocking
    # link-local/metadata/multicast/reserved. An unresolvable host isn't itself an SSRF
    # target — let the real httpx connection error surface instead of a misleading block.
    # If an egress allowlist IS set, it still enforces (the host must be allowlisted, which
    # it also needs for actual gateway egress / the OpenShell policy anyway).
    from security import egress

    if egress.check_url(url, allow_private=True, block_unresolvable=False):
        return [], "api_base host is blocked by the egress guard (set egress.allowed_hosts to permit it)"
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return [], f"connection failed: {e}"
    except Exception as e:  # noqa: BLE001 — a malformed api_base (httpx.InvalidURL, e.g.
        # "Invalid port", a bad scheme/host) is NOT an httpx.HTTPError, so it would
        # otherwise propagate as a 500 and lock the setup wizard. A probe must always
        # return a fixable error, never raise.
        return [], f"invalid api_base ({type(e).__name__}): {e}"

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
    # SSRF guard (#871) — same as list_gateway_models: allow_private so a localhost /
    # LAN / tailnet operator gateway works, link-local/metadata stays blocked, and an
    # allowlist (when set) still enforces.
    from security import egress

    if egress.check_url(url, allow_private=True, block_unresolvable=False):
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

    detail = (
        _re.split(
            r"\s*(?:Received API Key|Key Hash|Unable to find token|Token=)",
            detail,
            maxsplit=1,
        )[0]
        .strip()
        .rstrip(".,")
    )
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
    return setup_marker_path().exists()


def mark_setup_complete() -> None:
    """Write the marker so subsequent boots skip the wizard.

    Idempotent — safe to call repeatedly. The file is empty; only
    its presence matters.
    """
    marker = setup_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


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
    setup_marker_path().unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# SOUL.md presets
# ---------------------------------------------------------------------------


def list_soul_presets() -> list[str]:
    """Return preset names (file stems, no extension) sorted alphabetically.

    The wizard's preset dropdown reads from this — dropping a new
    markdown file into ``config/soul-presets/`` makes it a choice
    without code changes.
    """
    root = presets_dir()
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.md"))


def read_soul_preset(name: str) -> str:
    """Return the preset's content.

    Returns empty string for an unknown name rather than raising —
    the wizard treats that as "no preset selected, blank canvas".

    Path-traversal guarded: the resolved target must live inside
    ``presets_dir()``. A name like ``"../secret"`` would otherwise
    escape the presets directory and read arbitrary ``.md`` files
    anywhere the process can reach.
    """
    root = presets_dir()
    presets_root = root.resolve()
    candidate = (root / f"{name}.md").resolve()
    if presets_root not in candidate.parents or not candidate.is_file():
        return ""
    return candidate.read_text(encoding="utf-8")
