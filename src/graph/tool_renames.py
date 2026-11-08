"""Removed tool names and where they still turn up (ADR 0125 D3, #4555).

ADR 0125 renamed the memory tools with no aliases: ``memory_ingest`` → ``memory_save``,
``memory_recall`` → ``memory_search``, ``forget_memory`` → ``memory_forget``, and
``memory_stats`` folded into ``memory_list``. The new names are the only ones a tool is
ever registered under. Configs, plugins and stored rows written before the rename still
carry the old names, and a list that names a tool that doesn't exist fails silently: the
name just matches nothing.

This module is the one place that knows the old names. Two jobs:

* :func:`migrate_tool_names` translates a tool-name list wherever one is READ (config
  allowlists and denylists, the execute_code bridge list, stored tool fences, subagent
  lists), logging one WARNING per (source, old name) per process.
* :func:`legacy_tool_mentions` / :func:`audit_legacy_tool_references` find the old names
  in text the model reads (SKILL.md files, subagent prompts, SOUL), so the boot log names
  every plugin or file that still tells the model to call a tool that is gone.

Remove the whole module, and its callers, once fleet plugins and configs have moved over.
Never register these names as tool aliases.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

LEGACY_TOOL_RENAMES: dict[str, str] = {
    "memory_ingest": "memory_save",
    "memory_recall": "memory_search",
    "forget_memory": "memory_forget",
    "memory_stats": "memory_list",
}

_LEGACY_RE = re.compile(r"\b(" + "|".join(sorted(LEGACY_TOOL_RENAMES)) + r")\b")

# (source, old name) pairs already warned about — one line per pair per process.
_WARNED: set[tuple[str, str]] = set()
# Sources the reference audit already warned about — one line per source per process.
_AUDITED: set[str] = set()


def warn_once(source: str, old: str, message: str, *args) -> None:
    """Log ``message`` the first time (``source``, ``old``) is seen in this process."""
    key = (source, old)
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(message, *args)


def migrate_tool_names(names: Iterable | None, *, source: str) -> list[str]:
    """``names`` with each removed tool name replaced by its new name, deduplicated in
    order. ``source`` names where the list came from (``"tools.disabled"``,
    ``"execute_code.tools"``) and goes into the one-time warning, so the operator knows
    which file to edit. Non-string entries are kept as-is; a list with no old names comes
    back unchanged (as a new list)."""
    out = []
    for name in names or []:
        new = LEGACY_TOOL_RENAMES.get(name) if isinstance(name, str) else None
        if new:
            warn_once(
                source,
                name,
                "[tool-rename] %s lists removed tool '%s'; using '%s'. Update the config or plugin "
                "that sets it (ADR 0125: the old memory tool names go away after #4555).",
                source,
                name,
                new,
            )
        out.append(new or name)
    return list(dict.fromkeys(out))


def migrate_tool_keys(mapping: dict | None, *, source: str) -> dict:
    """A dict keyed by tool name (``enforcement.rate_limits``) with removed names renamed.
    When an old and a new key both exist, the new key's value wins."""
    out: dict = {}
    for key, value in (mapping or {}).items():
        new = migrate_tool_names([key], source=source)[0] if isinstance(key, str) else key
        if new in out and new != key:
            continue
        out[new] = value
    return out


def legacy_tool_mentions(text: str | None) -> list[str]:
    """The removed tool names ``text`` mentions, sorted and unique."""
    return sorted(set(_LEGACY_RE.findall(text or "")))


def audit_legacy_tool_references(
    *,
    skill_files: Iterable[Path] = (),
    subagents: Iterable = (),
    soul: str | None = None,
) -> list[dict]:
    """Every source that still names a removed tool: ``[{source, names}]``.

    ``skill_files`` are SKILL.md paths (read whole: frontmatter ``tools:`` and body).
    ``subagents`` are ``SubagentConfig``-shaped objects: their ``system_prompt`` and
    ``tools`` are checked. ``soul`` is the persona text. Pure apart from reading the skill
    files; an unreadable file is skipped."""
    findings: list[dict] = []
    for path in skill_files:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names = legacy_tool_mentions(text)
        if names:
            findings.append({"source": f"skill {path}", "names": names})
    for sub in subagents:
        name = getattr(sub, "name", "?")
        prompt_names = legacy_tool_mentions(getattr(sub, "system_prompt", "") or "")
        if prompt_names:
            findings.append({"source": f"subagent '{name}' prompt", "names": prompt_names})
        listed = sorted({t for t in (getattr(sub, "tools", None) or []) if t in LEGACY_TOOL_RENAMES})
        if listed:
            findings.append({"source": f"subagent '{name}' tools", "names": listed})
    soul_names = legacy_tool_mentions(soul)
    if soul_names:
        findings.append({"source": "SOUL.md", "names": soul_names})
    return findings


def log_legacy_tool_references(findings: list[dict]) -> int:
    """Log one WARNING per finding not yet logged in this process; returns how many."""
    logged = 0
    for f in findings:
        if f["source"] in _AUDITED:
            continue
        _AUDITED.add(f["source"])
        renames = ", ".join(f"{n} -> {LEGACY_TOOL_RENAMES[n]}" for n in f["names"])
        logger.warning(
            "[tool-rename] %s names removed tool(s) %s. The model can't call these; update it (ADR 0125, #4555).",
            f["source"],
            renames,
        )
        logged += 1
    return logged
