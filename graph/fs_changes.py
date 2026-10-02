"""A live "these project files just changed" signal for the console code pane (ADR 0112).

The code pane's Diff tab and open file were fetched once and then sat still: when a coding
delegate (``@claude-code`` over ACP, a streaming A2A peer) edited files in a registered
project, the pane kept saying "No changes vs HEAD" until the operator clicked Refresh. This
module turns a write the runtime *observes* into one small bus event the console reacts to:

    topic ``fs.changed``  data ``{project, paths, source, target?}``

* ``project`` — a registered fs-fence project name; ``paths`` — project-relative POSIX
  paths (capped). An event only ever names a path inside a registered project's root: a
  write anywhere else is not the pane's business and is dropped here.
* ``source`` — ``"agent"`` (protoAgent's own ``write_file``/``edit_file``/``delete_file``)
  or ``"delegate"`` (a coder's tool call, reported through ``graph.delegate_progress``).
  The console's follow mode already tracks the agent's own tools on the live chat stream,
  so it follows only ``delegate`` events from here; every source refreshes the pane.
* ``target`` — the delegate's name, when there is one.

Published live-only (``retain=False``): a reconnecting console refetches anyway, and a busy
coder must not evict the replay ring's lifecycle events. Nothing is published while the code
pane toolset is off (``filesystem.code_pane``) — there is no consumer, and the routes 404.

Edits nothing reports (a terminal, an editor, ``sed -i`` inside a coder's shell tool) are the
console's cheap fallback poll's job (``GET /api/fs/stamp``), not this module's.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

log = logging.getLogger("protoagent.fs_changes")

TOPIC = "fs.changed"
#: Paths per event — a snapshot of WHICH files, not a manifest; the pane refetches the diff.
MAX_PATHS = 20
#: ACP ``ToolKind``s that change files on disk.
WRITE_KINDS = frozenset({"edit", "delete", "move"})
#: Tool names that write when the transport reports no (or a generic) kind — an A2A peer's
#: tool-call extension frames carry only a name, and some ACP coders send ``other``.
WRITE_TOOL_NAMES = frozenset(
    {
        "write",
        "edit",
        "multiedit",
        "notebookedit",
        "write_file",
        "edit_file",
        "delete_file",
        "create_file",
        "apply_patch",
        "str_replace",
        "str_replace_editor",
    }
)


def is_write_tool(kind: str | None, name: str | None) -> bool:
    """Does a tool call with this ACP ``kind`` / display ``name`` change files?

    ``kind`` wins when it is specific (``read``/``search``/``execute`` never count — an
    ``execute`` that rewrote files is the fallback poll's to notice). With no kind, or
    ``other``, the first word of the name decides: claude-agent-acp titles a call
    ``Edit src/app.ts`` / ``Write /abs/x.py``."""
    k = (kind or "").strip().lower()
    if k in WRITE_KINDS:
        return True
    if k and k != "other":
        return False
    first = (name or "").strip().split(" ", 1)[0].strip("`'\":").lower()
    return first in WRITE_TOOL_NAMES


def _live_config():
    try:
        from graph.plugins.host import HOST

        return HOST.config() if HOST.config is not None else None
    except Exception:  # noqa: BLE001 — a mid-reload config read must not cost a tool call
        return None


def _publisher():
    try:
        from graph.plugins.host import HOST

        return HOST.publish
    except Exception:  # noqa: BLE001
        return None


def _enabled(config) -> bool:
    from tools.fs_tools import code_pane_enabled

    return code_pane_enabled(config)


def _project_roots(config) -> list[tuple[str, Path]]:
    """``[(name, resolved root)]`` of the live fence, longest root first."""
    from tools.fs_tools import live_project_registry

    registry = live_project_registry(config)
    roots = []
    for name in registry.names():
        proj = registry.get(name)
        if proj is not None:
            roots.append((name, Path(proj.root)))
    return sorted(roots, key=lambda r: len(str(r[1])), reverse=True)


def map_to_projects(
    paths: Iterable[str], roots: list[tuple[str, Path]], *, workdir: str | None = None
) -> dict[str, list[str]]:
    """``{project: [relative paths]}`` for every path that lies inside a project root.

    A relative path is taken against ``workdir`` (the delegate's cwd) and dropped without
    one — guessing a base would name the wrong file. A path inside nested projects is
    reported for each of them (the pane may be showing either). Order kept, duplicates
    folded, each project's list capped at ``MAX_PATHS``."""
    out: dict[str, list[str]] = {}
    base = Path(os.path.expanduser(workdir)) if workdir else None
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        p = Path(os.path.expanduser(raw.strip()))
        if not p.is_absolute():
            if base is None:
                continue
            p = base / p
        try:
            resolved = p.resolve()
        except (OSError, RuntimeError):
            continue
        for name, root in roots:
            if resolved != root and root not in resolved.parents:
                continue
            rel = PurePosixPath(*resolved.relative_to(root).parts).as_posix() if resolved != root else "."
            bucket = out.setdefault(name, [])
            if rel not in bucket and len(bucket) < MAX_PATHS:
                bucket.append(rel)
    return out


def publish_fs_changed(project: str, paths: list[str], *, source: str, target: str | None = None, config=None) -> bool:
    """Publish one ``fs.changed`` for ``project`` (already-relative ``paths``). Returns
    whether it went out. Never raises — a live view must not cost the write."""
    try:
        cfg = config if config is not None else _live_config()
        publish = _publisher()
        if publish is None or not project or not _enabled(cfg):
            return False
        # The topic is a literal so docs/reference/plugin-events.md's scan catalogs it.
        publish(
            "fs.changed",
            {"project": project, "paths": list(paths)[:MAX_PATHS], "source": source, **({"target": target} if target else {})},
            retain=False,
        )
        return True
    except Exception:  # noqa: BLE001
        log.debug("[fs-changes] publish failed", exc_info=True)
        return False


def notify_paths_changed(
    paths: Iterable[str], *, source: str, target: str | None = None, workdir: str | None = None
) -> list[dict]:
    """Map arbitrary (absolute, or ``workdir``-relative) paths onto the registered projects
    and publish one ``fs.changed`` per project they touch. Returns the published payloads
    (empty when the pane is off, nothing maps, or there is no bus). Never raises."""
    try:
        cfg = _live_config()
        if _publisher() is None or not _enabled(cfg):
            return []
        mapped = map_to_projects(list(paths), _project_roots(cfg), workdir=workdir)
    except Exception:  # noqa: BLE001
        log.debug("[fs-changes] mapping failed", exc_info=True)
        return []
    sent: list[dict] = []
    for project, rels in mapped.items():
        if publish_fs_changed(project, rels, source=source, target=target, config=cfg):
            payload = {"project": project, "paths": rels, "source": source}
            if target:
                payload["target"] = target
            sent.append(payload)
    return sent
