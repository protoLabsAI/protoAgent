"""Config I/O for the live-edit drawer in chat_ui.py.

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
from io import StringIO
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
CONFIG_YAML_PATH = _LIVE_CONFIG_DIR / "langgraph-config.yaml"
SOUL_RUNTIME_PATH = Path("/sandbox/SOUL.md")

# Secrets overlay. The setup wizard / drawer collect a model API key and an
# A2A bearer token; persisting those into the tracked config YAML means every
# configured checkout carries credentials in git. Instead they live in this
# untracked sibling file (gitignored + dockerignored), read back by
# ``LangGraphConfig.from_yaml`` and stripped from the main YAML on every save.
SECRETS_YAML_PATH = _LIVE_CONFIG_DIR / "secrets.yaml"

# (section, key) pairs that must never be written to the tracked YAML.
SECRET_PATHS: tuple[tuple[str, str], ...] = (
    ("model", "api_key"),
    ("auth", "token"),
)

# Setup wizard state.
# Presence of this (empty) marker file = wizard has been run and the
# server should boot straight into the chat UI. Absence = show the
# wizard on first page load. Lives in the live config dir so a Docker volume
# mount (or the desktop app-data dir) persists setup across runs.
SETUP_MARKER_PATH = _LIVE_CONFIG_DIR / ".setup-complete"


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


def ensure_live_config() -> bool:
    """Seed the live config from the tracked ``.example`` template on first run.

    The live ``langgraph-config.yaml`` is untracked, so a fresh clone / a new
    container volume won't have one. Copy the template (comments + shipped
    defaults intact) into place when it's missing. Idempotent — does nothing
    once the live file exists, so wizard/drawer edits are never clobbered.
    Returns True only when it created the file.
    """
    if CONFIG_YAML_PATH.exists() or not CONFIG_EXAMPLE_PATH.exists():
        return False
    import shutil

    CONFIG_YAML_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CONFIG_EXAMPLE_PATH, CONFIG_YAML_PATH)
    log.info(
        "[config] seeded live config %s from %s",
        CONFIG_YAML_PATH, CONFIG_EXAMPLE_PATH.name,
    )
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
    """Persist the document. Creates parent dirs if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if _HAS_RUAMEL:
        with open(path, "w") as f:
            _ruamel.dump(doc, f)
        return

    log.warning(
        "ruamel.yaml not installed — YAML comments in %s will not be "
        "preserved on save. Add `ruamel.yaml>=0.18` to requirements.txt "
        "to fix.", path,
    )
    import yaml
    with open(path, "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)


# ---------------------------------------------------------------------------
# Config dict <-> dataclass
# ---------------------------------------------------------------------------

def config_to_dict(config: LangGraphConfig) -> dict[str, Any]:
    """Serialize a LangGraphConfig into the nested dict shape the UI
    works with. Mirrors the YAML schema so round-tripping is trivial.

    Secret fields (model API key, A2A bearer) are redacted to ``""`` — the
    UI never needs the value back, only whether one is set (runtime status
    carries that as a boolean). Combined with the blank-means-unchanged save
    semantics in ``split_secret_updates``, a save that echoes the blank back
    leaves the stored secret intact.
    """
    return {
        "model": {
            "provider": config.model_provider,
            "name": config.model_name,
            "api_base": config.api_base,
            "api_key": "",
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "max_iterations": config.max_iterations,
        },
        "subagents": {
            "researcher": {
                "enabled": config.researcher.enabled,
                "tools": list(config.researcher.tools),
                "max_turns": config.researcher.max_turns,
            },
        },
        "middleware": {
            "knowledge": config.knowledge_middleware,
            "audit": config.audit_middleware,
            "memory": config.memory_middleware,
            "scheduler": config.scheduler_enabled,
        },
        "knowledge": {
            "db_path": config.knowledge_db_path,
            "embed_model": config.embed_model,
            "top_k": config.knowledge_top_k,
        },
        "skills": {
            "enabled": config.skills_enabled,
            "db_path": config.skills_db_path,
            "top_k": config.skills_top_k,
            "dir": config.skills_dir,
        },
        "mcp": {
            "enabled": config.mcp_enabled,
            "servers": list(config.mcp_servers),
            "timeout_seconds": config.mcp_timeout_seconds,
            "denylist": list(config.mcp_denylist),
        },
        "identity": {
            "name": config.identity_name,
            "operator": config.identity_operator,
        },
        "auth": {
            "token": "",
        },
        "runtime": {
            "autostart_on_boot": config.autostart_on_boot,
        },
        "operator": {
            "allowed_dirs": list(config.operator_allowed_dirs),
        },
    }


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
    for section, key in SECRET_PATHS:
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
    for section, key in SECRET_PATHS:
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
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return [], f"connection failed: {e}"

    if resp.status_code >= 400:
        detail = resp.text[:200] if resp.text else ""
        return [], f"HTTP {resp.status_code} from {url}: {detail}"

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
        MEMORY_TOOL_NAMES,
        SCHEDULER_TOOL_NAMES,
        get_all_tools,
    )

    names = [t.name for t in get_all_tools(knowledge_store)]
    # Deduplicate while preserving order: tools already present
    # (because their backend was passed in) shouldn't appear twice.
    seen = set(names)
    for extra in (*MEMORY_TOOL_NAMES, *SCHEDULER_TOOL_NAMES):
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
