"""Load human-authored skills in the AgentSkills ``SKILL.md`` format.

A skill is a folder containing a ``SKILL.md`` file: YAML frontmatter (``name`` +
``description``, the open AgentSkills standard) followed by a markdown body of
instructions. This is the same portable format Hermes, OpenClaw, and Claude
Code use — adopting it (rather than a bespoke shape) keeps protoAgent skills
shareable across the ecosystem.

Loaded skills are turned into the existing ``SkillV1Artifact`` shape and seeded
into the ``SkillsIndex`` (FTS5), so the dormant retrieval path in
``KnowledgeMiddleware`` (``load_skills`` → ``<learned_skills>`` injection) lights
up without any new retrieval code.

Two roots, mirroring the config bundle/live split:
- bundle (read-only, shipped): ``<repo>/config/skills/<slug>/SKILL.md``
- live (writable, user drop-in): ``<PROTOAGENT_CONFIG_DIR>/skills/<slug>/SKILL.md``
Live overrides bundle by skill ``name`` (later-wins).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from graph.extensions.skills import SkillV1Artifact

log = logging.getLogger("protoagent.skills.loader")

# AgentSkills caps the description (the trigger signal) at 1024 chars.
_MAX_DESCRIPTION = 1024


def parse_skill_md(path: Path) -> SkillV1Artifact | None:
    """Parse one ``SKILL.md`` into a ``SkillV1Artifact`` (or ``None`` if invalid).

    Never raises — a malformed skill file is logged and skipped so one bad drop
    can't take down skill loading (or the boot).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("[skills] cannot read %s: %s", path, exc)
        return None

    frontmatter, body = _split_frontmatter(text)
    if frontmatter is None:
        log.warning("[skills] %s has no YAML frontmatter — skipping", path)
        return None

    try:
        meta = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError as exc:
        log.warning("[skills] %s frontmatter is not valid YAML: %s", path, exc)
        return None
    if not isinstance(meta, dict):
        log.warning("[skills] %s frontmatter is not a mapping — skipping", path)
        return None

    name = str(meta.get("name", "")).strip()
    description = str(meta.get("description", "")).strip()
    if not name or not description:
        log.warning("[skills] %s missing required 'name'/'description' — skipping", path)
        return None
    if len(description) > _MAX_DESCRIPTION:
        log.warning(
            "[skills] %s description exceeds %d chars — truncating", path, _MAX_DESCRIPTION
        )
        description = description[:_MAX_DESCRIPTION]

    # Optional advisory tool hints (frontmatter `tools:` or `metadata.tools:`).
    tools = meta.get("tools")
    if tools is None and isinstance(meta.get("metadata"), dict):
        tools = meta["metadata"].get("tools")
    tools_used = [str(t) for t in tools] if isinstance(tools, (list, tuple)) else []

    return SkillV1Artifact(
        name=name,
        description=description,
        prompt_template=body.strip(),
        tools_used=tools_used,
        source_session_id=f"skill-md:{path.parent.name}",
    )


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split ``---``-fenced YAML frontmatter from the markdown body.

    Returns ``(frontmatter_text, body)`` or ``(None, text)`` when there's no
    leading frontmatter block.
    """
    stripped = text.lstrip("﻿")  # tolerate a BOM
    if not stripped.startswith("---"):
        return None, text
    lines = stripped.splitlines()
    # First line is the opening fence; find the closing fence.
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def discover_skill_files(roots: list[Path]) -> list[Path]:
    """Return every ``SKILL.md`` under the given roots (nested folders allowed).

    The skill is named by its frontmatter, not its folder, so grouping
    directories are purely organizational (the OpenClaw/AgentSkills rule).
    """
    files: list[Path] = []
    for root in roots:
        if root and root.exists() and root.is_dir():
            files.extend(sorted(root.glob("**/SKILL.md")))
    return files


def load_skills_from_disk(roots: list[Path]) -> list[SkillV1Artifact]:
    """Parse all skills under *roots*, de-duped by name (later root wins)."""
    by_name: dict[str, SkillV1Artifact] = {}
    for path in discover_skill_files(roots):
        artifact = parse_skill_md(path)
        if artifact is not None:
            by_name[artifact.name] = artifact  # later (live) root overrides earlier
    return list(by_name.values())


def seed_skills_index(index, roots: list[Path]) -> int:
    """Rebuild the skill index from the ``SKILL.md`` files under *roots*.

    Returns the number of skills indexed. ``rebuild_index`` is safe here because
    in this slice the index holds only disk-authored skills; agent-emitted
    skills (a later slice) will introduce a source column before they share it.
    """
    artifacts = load_skills_from_disk(roots)
    try:
        index.rebuild_index(artifacts)
    except Exception as exc:  # noqa: BLE001 — seeding must never be fatal
        log.warning("[skills] index seed failed: %s", exc)
        return 0
    return len(artifacts)
