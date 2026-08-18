"""Config/env knob resolution — explicit ENV > Settings UI > default (ADR 0019)."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("protoagent.plugins.artifact")

# ── config ───────────────────────────────────────────────────────────────────
# Read live from the host's plugin config (the manifest `settings:` block, ADR 0019 —
# editable in Settings ▸ Plugins, persisted, no restart) with an env-var override and
# a literal default. Precedence: explicit ENV > UI/config > default. config() reads the
# LIVE config each call, so a Settings save takes effect immediately; under ACP (no
# graph state in the tool process) it falls back to env/default.
_TRUE = {"1", "true", "yes", "on"}


def _plugin_cfg() -> dict:
    try:
        from graph.sdk import config

        return (getattr(config(), "plugin_config", {}) or {}).get("artifact", {}) or {}
    except Exception:  # noqa: BLE001 — no host (tests) / not yet loaded → env+default
        return {}


def _cfg_bool(key: str, env: str) -> bool:
    e = os.environ.get(env)
    if e:
        return e.strip().lower() in _TRUE
    v = _plugin_cfg().get(key)
    if isinstance(v, bool):
        return v
    return v not in (None, "") and str(v).strip().lower() in _TRUE


def _cfg_str(key: str, env: str, default: str = "") -> str:
    e = os.environ.get(env)
    if e:
        return e
    v = _plugin_cfg().get(key)
    return str(v) if v not in (None, "") else default


def _cfg_int(key: str, env: str, default: int, minimum: int = 1) -> int:
    for raw in (os.environ.get(env, ""), _plugin_cfg().get(key)):
        if raw not in (None, ""):
            try:
                return max(minimum, int(raw))
            except (TypeError, ValueError):
                pass  # bad value → try the next source, never crash
    return default


# History/version/size caps — Settings ▸ Plugins number fields (env override).
# Read live via functions so a config change applies at once.
def _max_history() -> int:
    return _cfg_int("history", "ARTIFACT_HISTORY", 20)


def _max_versions() -> int:
    return _cfg_int("max_versions", "ARTIFACT_MAX_VERSIONS", 50)


def _max_code_bytes() -> int:
    return _cfg_int("max_code_kb", "ARTIFACT_MAX_CODE_KB", 512) * 1024


# The binary blob cap for `file` artifacts (ADR 0092 D2) — separate from max_code_kb
# because a real .docx/.xlsx/.pdf is bytes on disk (sidecar file), not source text.
def _max_blob_bytes() -> int:
    return _cfg_int("max_blob_kb", "ARTIFACT_MAX_BLOB_KB", 25 * 1024) * 1024


# The extracted-text PREVIEW cap for a `file` version — the diffable projection stored in
# `code` (docx→text, xlsx→sheet table, pptx→outline, pdf→text). Kept well under
# max_code_kb so a huge document can't bloat history.json (read on every panel poll).
def _max_preview_bytes() -> int:
    return _cfg_int("max_preview_kb", "ARTIFACT_MAX_PREVIEW_KB", 64) * 1024


# Interactive artifacts (window.protoArtifact.ask → the agent). OPT-IN: letting
# sandboxed artifact code trigger LLM calls is a cost surface. `ask_enabled` +
# `ask_system` are Settings ▸ Plugins fields (manifest `settings:`); ask_max_chars caps.
def _ask_enabled() -> bool:
    return _cfg_bool("ask_enabled", "ARTIFACT_ASK_ENABLED")


def _ask_system() -> str | None:
    return _cfg_str("ask_system", "ARTIFACT_ASK_SYSTEM") or None


def _ask_max_chars() -> int:
    return _cfg_int("ask_max_chars", "ARTIFACT_ASK_MAX_CHARS", 4000)
