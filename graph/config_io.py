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
CONFIG_YAML_PATH = REPO_ROOT / "config" / "langgraph-config.yaml"
SOUL_SOURCE_PATH = REPO_ROOT / "config" / "SOUL.md"
SOUL_RUNTIME_PATH = Path("/sandbox/SOUL.md")

# Setup wizard state.
# Presence of this (empty) marker file = wizard has been run and the
# server should boot straight into the chat UI. Absence = show the
# wizard on first page load. Lives in ``config/`` so a Docker volume
# mount at /opt/<agent>/config persists setup across container runs.
SETUP_MARKER_PATH = REPO_ROOT / "config" / ".setup-complete"

# SOUL.md starter templates. The wizard offers these as presets the
# user can pick then edit before saving. Adding a new file here
# automatically makes it a choice — no registry to update.
PRESETS_DIR = REPO_ROOT / "config" / "soul-presets"


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


def load_yaml_doc(path: Path = CONFIG_YAML_PATH) -> Any:
    """Load the config YAML as a mutable document.

    With ruamel: returns a CommentedMap that preserves comments +
    key order on subsequent dump. Without: returns a plain dict and
    comments are lost on next save (a warning is logged once per
    save so the operator knows).
    """
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
    """
    return {
        "model": {
            "provider": config.model_provider,
            "name": config.model_name,
            "api_base": config.api_base,
            "api_key": config.api_key,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "max_iterations": config.max_iterations,
        },
        "subagents": {
            "worker": {
                "enabled": config.worker.enabled,
                "tools": list(config.worker.tools),
                "max_turns": config.worker.max_turns,
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
        "identity": {
            "name": config.identity_name,
            "operator": config.identity_operator,
        },
        "auth": {
            "token": config.auth_token,
        },
        "runtime": {
            "autostart_on_boot": config.autostart_on_boot,
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

        worker = updates.get("subagents", {}).get("worker", {})
        if worker:
            max_turns = int(worker.get("max_turns", 20))
            if max_turns < 1:
                return False, f"worker.max_turns must be >= 1, got {max_turns}"
            tools = worker.get("tools", [])
            if not isinstance(tools, list):
                return False, "worker.tools must be a list"

        knowledge = updates.get("knowledge", {})
        if knowledge:
            top_k = int(knowledge.get("top_k", 5))
            if top_k < 1:
                return False, f"knowledge.top_k must be >= 1, got {top_k}"
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
