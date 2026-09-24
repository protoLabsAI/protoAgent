"""Server-side directory browser for the console's path fields.

Every path setting — Work folders (``filesystem.projects``, ADR 0007), the project
dir, the checkpoint/commons/secrets paths — was a free-text box, and typing is the
one input method that can't tell you the folder doesn't exist. A fat-fingered work
folder is skipped at graph build, and if it was the only one the ENTIRE fs toolset
unbinds; the operator sees tools vanish with no cause. A picker makes that
unrepresentable: you can only choose a directory that's really there.

**Why server-side and not the browser's own picker.** The console configures a
server that is often NOT the machine running the browser (tailnet, fleet members,
Docker). ``<input webkitdirectory>`` yields the CLIENT's files under a fake root, and
``showDirectoryPicker()`` yields an opaque handle with no path at all — both describe
the wrong machine, and neither can produce the absolute server path these settings
need. So the server lists its own filesystem and the client walks it. In the desktop
app the two machines coincide, which is why this one path works everywhere.

**Authority.** Listing necessarily reaches OUTSIDE the fs fence — you're choosing
what to fence. That grants nothing new: the same operator can already type any
absolute path into the same field, and every ``/api`` route is already
operator-authed. Kept narrow anyway — directory NAMES only, never file contents,
never a write.

**The code pane (ADR 0112)** lives here too — ``/api/fs/file`` and ``/api/fs/diff`` —
but on the opposite side of that line: they return file CONTENT, so they never leave
the fs fence (``tools.fs_tools.live_project_registry``, the ``read_file`` chokepoint),
refuse secret-like names (``tools.fs_secrets``), and run git only through the hardened
argv in ``tools.git_read``. Still read-only.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("protoagent.server")

# A directory listing is a UI convenience, not a data dump: cap it so a browse of
# /nix/store or a node_modules can't build a 100k-entry response.
_MAX_ENTRIES = 1000


def _safe_name(entry) -> str:
    try:
        return entry.name
    except Exception:  # noqa: BLE001 — a broken dirent must not kill the listing
        return ""


def _listing(target: Path, *, files: bool, hidden: bool) -> tuple[list[dict], bool]:
    """The visible entries plus whether the cap dropped any.

    Consumes ``iterdir()`` lazily rather than materializing it — a /nix/store-sized
    directory shouldn't cost a list of 100k Path objects to show 1000 rows. The cap is
    applied AFTER the sort on purpose: truncating mid-scan would show an arbitrary
    subset, where this shows the first N in the order the operator is looking at. That
    keeps a bounded worst case for the FILTERED set only, which is the right trade for
    a picker — and `truncated` tells the UI to say so rather than imply completeness.
    """
    out: list[dict] = []
    try:
        entries = target.iterdir()
    except PermissionError as exc:
        raise PermissionError(f"permission denied: {target}") from exc
    for entry in entries:
        name = _safe_name(entry)
        if not name or (not hidden and name.startswith(".")):
            continue
        try:
            # follow_symlinks default: a symlinked dir is browsable as a dir, which is
            # what the operator sees in Finder. Containment is enforced when the chosen
            # path is SAVED (fs_tools re-resolves), not here.
            is_dir = entry.is_dir()
        except OSError:  # dangling symlink / unreadable mount — skip, don't fail
            continue
        if not is_dir and not files:
            continue
        out.append({"name": name, "path": str(entry), "kind": "dir" if is_dir else "file"})
    # Directories first, then case-insensitive by name — Finder/Explorer ordering, so
    # the list reads the way the operator expects it to.
    out.sort(key=lambda e: (e["kind"] != "dir", e["name"].casefold()))
    return out[:_MAX_ENTRIES], len(out) > _MAX_ENTRIES


def register_browse_routes(app) -> None:
    from fastapi import HTTPException

    @app.get("/api/fs/browse")
    async def _api_fs_browse(path: str = "", files: bool = False, hidden: bool = False):
        """List directories under ``path`` so the console can offer a folder picker.

        Blank ``path`` starts at the operator's home. ``files=true`` also lists files
        (for the file-valued settings like ``checkpoint.db_path``); ``hidden=true``
        includes dot-entries. Read-only: names + kinds, never contents.
        """
        import asyncio

        try:
            # EVERY filesystem call goes in one worker — resolve/exists/is_dir block just
            # as hard as the listing does on a cold or network mount, so leaving those
            # three on the event loop would stall the whole server for the same reason
            # the listing would.
            return await asyncio.to_thread(_browse, path, files=files, hidden=hidden)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotADirectoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except OSError as exc:  # pragma: no cover — resolve rarely raises on posix
            raise HTTPException(status_code=400, detail=f"can't read {path}: {exc}") from exc

    @app.get("/api/fs/roots")
    async def _api_fs_roots():
        """``{roots: {project name: absolute root}}`` — the live fs fence.

        The fs tools speak in PROJECT-RELATIVE paths (``read_file(project, path)``),
        so the console needs each project's absolute root to turn a tool call's path
        into an "open in editor" link. Computed by ``tools.fs_tools.project_roots``,
        the same projection the tools resolve against — NOT ``/api/projects``, whose
        ADR 0095 registry can be shadowed by explicit ``filesystem.projects`` or the
        workspace default. Read-only; empty when the filesystem primitive is off.
        """
        import asyncio

        from runtime.state import STATE
        from tools.fs_tools import project_roots

        cfg = getattr(STATE, "graph_config", None)
        if cfg is None:
            return {"roots": {}}
        # is_dir() per root — off the event loop, like every other fs call here.
        return {"roots": await asyncio.to_thread(project_roots, cfg)}

    # ── code pane (ADR 0112) ─────────────────────────────────────────────────────
    # Unlike /api/fs/browse these return file CONTENT, so they stay INSIDE the fs fence:
    # every path goes through the same `live_project_registry(cfg).resolve` chokepoint
    # read_file uses, and a secret-like name (tools/fs_secrets.py) is refused even for
    # the operator — the pane is a display surface (screen shares, screenshots), not an
    # exfiltration-proof boundary, so it errs on the side of not painting keys.

    def _fs_error(status: int, code: str, reason: str):
        return HTTPException(status_code=status, detail={"code": code, "reason": reason})

    def _fence(project: str, path: str):
        """(registry project root, resolved target) or raise ValueError — the fs-tool fence."""
        from runtime.state import STATE
        from tools.fs_tools import live_project_registry

        registry = live_project_registry(getattr(STATE, "graph_config", None))
        target = registry.resolve(project, path)
        return registry.get(project).root, target

    def _read_file(project: str, path: str, start: int, end: int | None) -> dict:
        import os

        from tools.fs_secrets import is_secret_path
        from tools.fs_view import NotARegularFile, guess_language, open_regular, read_window, sniff_binary

        try:
            root, target = _fence(project, path)
        except ValueError as exc:
            raise _fs_error(400, "bad_path", str(exc)) from exc
        # Denied BEFORE any existence check — a 403-vs-404 split would be an oracle for
        # which secret files exist. Checked on the requested name AND on the resolved
        # target, so `notes.txt -> .env` is refused too.
        reason = is_secret_path(path) or is_secret_path(target.relative_to(root))
        if reason:
            raise _fs_error(403, "denied", f"secret-like file: {reason}")
        if not target.exists():
            raise _fs_error(404, "not_found", f"no such file: {path}")
        if target.is_dir():
            raise _fs_error(400, "not_a_file", f"not a file: {path}")
        # Everything below reads ONE descriptor that open_regular has verified is a regular
        # file — a FIFO would block the worker forever, and a path swapped for a symlink
        # after `_fence` resolved it must not be followed.
        try:
            fh = open_regular(target)
        except NotARegularFile as exc:
            raise _fs_error(400, "not_a_file", f"not a file: {path}") from exc
        except FileNotFoundError as exc:
            raise _fs_error(404, "not_found", f"no such file: {path}") from exc
        rel = target.relative_to(root).as_posix()
        with fh:
            base = {"project": project, "path": rel, "size": os.fstat(fh.fileno()).st_size, "language": guess_language(rel)}
            if sniff_binary(fh):
                return {**base, "line_count": None, "start": None, "end": None, "truncated": False, "binary": True, "text": None}
            try:
                win = read_window(fh, start, end)
            except ValueError as exc:
                raise _fs_error(400, "bad_range", str(exc)) from exc
        return {
            **base,
            "line_count": win.line_count,
            "start": win.start,
            "end": win.end,
            "truncated": win.truncated,
            "binary": False,
            "text": win.text,
        }

    @app.get("/api/fs/file")
    async def _api_fs_file(project: str, path: str, start: str = "1", end: str | None = None):
        """Lines ``start..end`` of a fenced project file for the console code pane.

        Same fence as ``read_file``; secret-like names are 403 ``denied``; a binary file
        answers with metadata and ``text: null``. Capped per response (2 MB of text,
        20,000 lines, 2,000 chars per line) with ``truncated`` set — page with
        ``start=end+1``. Read-only.
        """
        import asyncio

        # Parsed by hand, not as `int` params: FastAPI's own 422 has a different shape from
        # every other error this route returns ({code, reason}), and the console keys on code.
        try:
            start_n = int(start.strip()) if start and start.strip() else 1
            end_n = int(end.strip()) if end is not None and end.strip() else None
        except ValueError as exc:
            raise _fs_error(400, "bad_range", f"start/end must be integers (got start={start!r}, end={end!r})") from exc
        try:
            return await asyncio.to_thread(_read_file, project, path, start_n, end_n)
        except HTTPException:
            raise
        except OSError as exc:
            raise _fs_error(400, "unreadable", f"can't read {path}: {exc}") from exc

    def _diff(project: str) -> dict:
        from tools.git_read import working_tree_diff

        try:
            root, _ = _fence(project, ".")
        except ValueError as exc:
            raise _fs_error(400, "bad_path", str(exc)) from exc
        d = working_tree_diff(root)
        if not d.is_git:
            return {"project": project, "is_git": False, "files": [], "patch": ""}
        return {
            "project": project,
            "is_git": True,
            "head": d.head,
            "branch": d.branch,
            "files": [f.as_dict() for f in d.files],
            "patch": d.patch,
            "truncated": d.truncated,
        }

    @app.get("/api/fs/diff")
    async def _api_fs_diff(project: str):
        """The project's working tree vs ``HEAD`` (tracked changes + untracked files).

        Hardened git (tools/git_read.py): no external diff, textconv, filter, fsmonitor or
        hook can run, whatever the repository's config/attributes say. Secret-like paths
        are listed ``denied: true`` with their content omitted. Patch capped at 1 MB.
        """
        import asyncio

        from tools.git_read import GitError, GitTimeout

        try:
            return await asyncio.to_thread(_diff, project)
        except HTTPException:
            raise
        except GitTimeout as exc:
            raise _fs_error(504, "timeout", "git took longer than 10s") from exc
        except GitError as exc:
            raise _fs_error(400, "git_error", str(exc)) from exc
        except OSError as exc:
            raise _fs_error(400, "unreadable", f"can't read {project}: {exc}") from exc

    def _browse(path: str, *, files: bool, hidden: bool) -> dict:
        """Resolve + validate + list, entirely inside the worker thread. Raises the
        stdlib OSError subclasses; the handler maps them to status codes."""
        raw = (path or "").strip()
        target = (Path(raw).expanduser() if raw else Path.home()).resolve()
        if not target.exists():
            raise FileNotFoundError(f"no such folder: {target}")
        if not target.is_dir():
            raise NotADirectoryError(f"not a folder: {target}")
        entries, truncated = _listing(target, files=files, hidden=hidden)
        parent = str(target.parent) if target.parent != target else None
        return {
            "path": str(target),
            "parent": parent,
            "entries": entries,
            "truncated": truncated,
            "roots": _roots(),
        }

    def _roots() -> list[dict]:
        """Jump-off points for the picker, so a first open isn't a walk up from wherever.

        Home, the box root (where instance data lives), and any already-configured work
        folders — the places an operator actually points this thing. Non-existent
        entries are dropped so the picker never offers a dead link.
        """
        from runtime.state import STATE

        candidates: list[tuple[str, str]] = [("Home", str(Path.home()))]
        try:
            from infra.paths import instance_paths, workspace_dir

            candidates.append(("Workspace", str(workspace_dir())))
            candidates.append(("protoAgent", str(instance_paths().box_root)))
        except Exception:  # noqa: BLE001 — roots are a convenience, never a hard failure
            pass
        cfg = getattr(STATE, "graph_config", None)
        for proj in (getattr(cfg, "filesystem_projects", None) or []) if cfg else []:
            if isinstance(proj, dict) and str(proj.get("path") or "").strip():
                candidates.append((str(proj.get("name") or proj["path"]), str(proj["path"])))
        out: list[dict] = []
        seen: set[str] = set()
        for label, raw in candidates:
            try:
                resolved = Path(raw).expanduser().resolve()
                if not resolved.is_dir():
                    continue
            except OSError:
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            out.append({"label": label, "path": key})
        return out
