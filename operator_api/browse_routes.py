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


def _listing(target: Path, *, files: bool, hidden: bool) -> list[dict]:
    out: list[dict] = []
    try:
        entries = list(target.iterdir())
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
    return out[:_MAX_ENTRIES]


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

        raw = (path or "").strip()
        target = Path(raw).expanduser() if raw else Path.home()
        try:
            target = target.resolve()
        except OSError as exc:  # pragma: no cover — resolve rarely raises on posix
            raise HTTPException(status_code=400, detail=f"can't resolve {raw}: {exc}") from exc
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"no such folder: {target}")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail=f"not a folder: {target}")

        try:
            # Listing a cold/large/network directory blocks; keep it off the event loop
            # like every other filesystem call in this package.
            entries = await asyncio.to_thread(_listing, target, files=files, hidden=hidden)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        parent = str(target.parent) if target.parent != target else None
        return {"path": str(target), "parent": parent, "entries": entries, "roots": _roots()}

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
