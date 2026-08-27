"""Operator-authored skill CRUD — write/edit/remove ``SKILL.md`` files.

The console's Skills surface lets the operator create and edit skills directly.
Those skills are persisted as real ``SKILL.md`` files (the portable AgentSkills
format, same as bundled examples) under the writable user-skills root
(``infra.paths.user_skills_dir`` → ``~/.protoagent/skills``), NOT as bare DB rows
— so they survive reboots (re-seeded each boot), are exportable/git-trackable on
their own, and round-trip through the same loader the agent already uses.

This module is the file layer; the HTTP routes (``operator_api/knowledge_routes``)
compose it with the live ``SkillsIndex`` so an edit shows up without a restart.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from graph.skills.loader import parse_skill_md

log = logging.getLogger("protoagent.skills.authoring")


def _atomic_copy_file(source: Path, destination: Path) -> None:
    """Copy a file byte-for-byte, atomically replacing ``destination``."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(destination.parent), prefix=f".{destination.name}.")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, destination)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def slugify(name: str) -> str:
    """Folder-safe slug for a skill name (lowercased, non-alphanumerics → hyphens).

    Mirrors ``SkillV1Artifact.slash_token`` so a skill's folder, slash token, and
    ``source_session_id`` stay aligned."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def skill_md_text(
    name: str,
    description: str,
    body: str,
    *,
    tools: list[str] | None = None,
    user_facing: bool = False,
    slash: str = "",
    user_only: bool = False,
    provenance: dict[str, str] | None = None,
) -> str:
    """Render a ``SKILL.md`` document: YAML frontmatter + markdown body."""
    meta: dict[str, object] = {"name": name.strip(), "description": description.strip()}
    if tools:
        meta["tools"] = [str(t).strip() for t in tools if str(t).strip()]
    # user_only implies user_facing — the /slash is the only way to use it.
    if user_facing or user_only:
        meta["user_facing"] = True
        if slash.strip():
            meta["slash"] = slash.strip()
    if user_only:
        meta["user_only"] = True
    if provenance:
        meta["provenance"] = {str(k): str(v) for k, v in provenance.items() if str(v).strip()}
    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{frontmatter}\n---\n\n{body.strip()}\n"


def skill_path(root: Path, name: str) -> Path:
    """The ``SKILL.md`` path for *name* under *root* (``<root>/<slug>/SKILL.md``)."""
    slug = slugify(name)
    if not slug:
        raise ValueError("skill name must contain at least one ASCII letter or digit")
    return root / slug / "SKILL.md"


def user_skill_exists(root: Path, name: str) -> bool:
    """True when *name* resolves to an operator-authored ``SKILL.md`` under *root*."""
    if not slugify(name):
        return False
    return skill_path(root, name).is_file()


def write_skill(
    root: Path,
    name: str,
    description: str,
    body: str,
    *,
    tools: list[str] | None = None,
    user_facing: bool = False,
    slash: str = "",
    user_only: bool = False,
    provenance: dict[str, str] | None = None,
):
    """Write ``<root>/<slug>/SKILL.md`` for *name* and return its parsed artifact.

    Atomic at the file level (write to a temp sibling, then replace). Returns the
    ``SkillV1Artifact`` so the caller can index it immediately."""
    from infra.paths import atomic_write

    path = skill_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        path,
        skill_md_text(
            name,
            description,
            body,
            tools=tools,
            user_facing=user_facing,
            slash=slash,
            user_only=user_only,
            provenance=provenance,
        ),
    )
    artifact = parse_skill_md(path)
    if artifact is None:
        # Shouldn't happen — we just wrote valid frontmatter — but never hand back None.
        raise ValueError("authored SKILL.md failed to parse back")
    return artifact


def archive_skill(root: Path, skill: dict, *, session_id: str, reason: str) -> Path:
    """Snapshot a skill before an agent-authored update/delete and return its path.

    A file-backed skill is copied byte-for-byte; mutation provenance lives in a sidecar
    so the archived artifact's original frontmatter, comments, formatting, and provenance
    stay untouched.
    """
    from infra.paths import atomic_write

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    slug = slugify(str(skill.get("name", "")))
    if not slug:
        raise ValueError("skill name must contain at least one ASCII letter or digit")
    folder = root / ".history" / slug
    path = folder / f"{stamp}-SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    live = skill_path(root, str(skill.get("name", "")))
    if live.is_file():
        _atomic_copy_file(live, path)
    else:
        atomic_write(
            path,
            skill_md_text(
                str(skill.get("name", "")),
                str(skill.get("description", "")),
                str(skill.get("prompt_template", "")),
                tools=list(skill.get("tools_used") or []),
                user_facing=bool(skill.get("user_facing", False)),
                slash=str(skill.get("slash", "") or ""),
                user_only=bool(skill.get("user_only", False)),
                provenance={"session_id": str(skill.get("source_session_id", "") or "")},
            ),
        )
    atomic_write(
        path.with_suffix(".json"),
        json.dumps(
            {"archived_at": stamp, "source_session_id": session_id, "reason": reason},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return path


def restore_skill_snapshot(root: Path, name: str, snapshot: Path):
    """Restore an archived ``SKILL.md`` exactly and return its parsed artifact."""
    live = skill_path(root, name)
    _atomic_copy_file(snapshot, live)
    artifact = parse_skill_md(live)
    if artifact is None:
        raise ValueError("restored SKILL.md failed to parse")
    return artifact


def remove_skill(root: Path, name: str) -> bool:
    """Remove the operator-authored skill folder for *name*. Returns True if removed."""
    folder = (root / slugify(name)).resolve()
    root_resolved = root.resolve()
    # Defensive: never delete outside the user-skills root.
    if folder == root_resolved or root_resolved not in folder.parents:
        log.warning("[skills] refusing to remove %s — outside user root %s", folder, root_resolved)
        return False
    if folder.is_dir():
        try:
            shutil.rmtree(folder)
        except OSError as exc:
            log.warning("[skills] failed to remove %s: %s", folder, exc)
            return False
        return not folder.exists()
    return False


def classify(skill: dict, root: Path) -> tuple[str, bool]:
    """Return ``(origin, editable)`` for a skill dict from ``index.all_skills()``.

    - ``commons``  — shared (layered tier); read-only here (promote is one-way).
    - ``user``     — operator-authored ``SKILL.md`` under the user root; editable.
    - ``learned``  — agent-emitted/distilled DB skill; editable (an edit materializes
                     it as a durable user ``SKILL.md``).
    - ``bundled``  — a shipped/plugin example (``disk`` source, no user file); read-only.
    """
    if skill.get("tier") == "commons":
        return "commons", False
    source = skill.get("source") or "emitted"
    if source == "disk":
        if user_skill_exists(root, skill.get("name", "")):
            return "user", True
        return "bundled", False
    # emitted / distilled / promoted-in-a-flat-library → operator-curatable
    return "learned", True
