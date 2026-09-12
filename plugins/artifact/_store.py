"""The artifact store: file-backed version chains + sidecar blobs (ADR 0092 D2).

State is persisted to a FILE (instance-scoped), not module memory — under the ACP
runtime the tool executes in the operator-MCP process while the route is served by
the main process, so the two only share state through disk — and so every mutation
takes a cross-PROCESS lock, not just a thread lock (see ``serialized``).
"""

from __future__ import annotations

import contextlib
import errno
import functools
import json
import logging
import os
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import _config

log = logging.getLogger("protoagent.plugins.artifact")

# ── the store ──────────────────────────────────────────────────────────────────
# An artifact is a VERSION CHAIN: {id, kind, title, versions:[{code, ts, by}], …}.
# show_artifact creates one; update_artifact/rewrite_artifact append a version (the
# proven Claude "update vs rewrite" model — iterate the same artifact, don't spam the
# panel with near-duplicates). The file is {"artifacts": [newest-first], "current": id}.
#
# An artifact may carry ``"pinned": true`` — it is then exempt from the history eviction in
# _write_store (see _evict), and pinned artifacts are stored FIRST: the list is really
# [pinned] + [unpinned], each group most-recently-touched first (pinning or unpinning counts as
# a touch for order, though not for `current`). The key is ABSENT on an unpinned artifact
# rather than false, so a store that never pinned anything is byte-for-byte the pre-pin format.
# An older (pre-0.18) plugin reading a pinned store ignores the key and keeps only
# artifacts[:history] on its next write — pins-first means that keeps them until `history`
# newer artifacts push them out, rather than evicting them on the very first write.


def _store_path() -> Path:
    base = Path(os.environ.get("ARTIFACT_DIR") or (Path.home() / ".protoagent" / "artifact"))
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        base = base / inst
    base.mkdir(parents=True, exist_ok=True)
    return base / "history.json"


def _store_etag() -> str:
    """Weak validator for the store file (#2256) — mtime+size suffices because every
    mutation goes through ``_write_store``, which rewrites the file. Constant when the
    store doesn't exist yet (the default empty store is constant too)."""
    try:
        st = _store_path().stat()
        return f'W/"{st.st_mtime_ns}-{st.st_size}"'
    except OSError:
        return 'W/"empty"'


# ── store mutations are serialised — across threads (#3401) AND processes ────────────
# Every mutating path is read-whole-store → change → write-whole-store, and the harness
# runs independent tool calls in PARALLEL. Two updates to one artifact both read the same
# snapshot, each appends version N+1 to its own copy, and the second whole-store write
# overwrites the first — losing an edit AND its version while both report success. Two
# writes reporting the SAME new version is the tell (observed live: both said "version 2").
#
# A thread lock alone only orders callers inside ONE process, and this store is shared by
# two: under the ACP runtime the tools run in the operator-MCP process while the panel's
# routes are served by the main one (see the module docstring). The same clobber happened
# across that boundary — a /render-status stamp that read the store before a pin_artifact in
# the other process wrote it overwrote the pin, which had already replied "Pinned". So a
# mutation also holds an OS file lock (flock on POSIX, msvcrt byte-range lock on Windows) on
# a sidecar beside history.json, for the whole read-modify-write.
#
# The thread lock stays and is taken FIRST: it queues this process's own callers, and both
# OS locks belong to an open file, so two threads each opening the sidecar would block each
# other forever (POSIX) or fail (Windows) — holding the thread lock is also what makes the
# depth counter below safe. Reentrant, because a mutating path may call a helper that locks.
#
# `_write_store` is atomic at the file level (tempfile + os.replace), so a lock-free READER
# never sees a torn store; the lock is only for atomicity ACROSS a read and its write-back.
_MUTATION_LOCK = threading.RLock()
_FILE_LOCK_DEPTH = 0  # this process's hold depth — only touched while _MUTATION_LOCK is held
_FILE_LOCK_POLL_S = 0.01  # Windows polls a non-blocking lock (see _os_lock)
_WIN_LOCK_BUSY = {errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)}
_lock_unavailable_warned = False


def _lock_path() -> Path:
    """The cross-process lock's sidecar, beside history.json. Never deleted: unlinking a
    lock file another process has open (or is about to open) splits the lock in two."""
    return _store_path().with_name("history.json.lock")


def _os_lock(fd: int) -> None:
    """Block until this process holds the exclusive OS lock on ``fd``."""
    if sys.platform == "win32":
        import msvcrt

        # msvcrt's blocking mode (LK_LOCK) gives up with an error after ~10 s; a writer queued
        # behind a slow save must wait, not fail, so poll the non-blocking mode instead. The
        # region is byte 0 — locking past EOF is allowed, and the sidecar stays empty.
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as e:
                if e.errno not in _WIN_LOCK_BUSY:
                    raise
            time.sleep(_FILE_LOCK_POLL_S)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def _os_unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _acquire_file_lock() -> int | None:
    """Open the sidecar and take the OS lock; the held fd, or ``None`` when the filesystem
    refuses locking (e.g. a network mount without lock support). That degrades to the thread
    lock alone — the behaviour before this lock existed — with one warning, rather than
    failing every store write."""
    global _lock_unavailable_warned
    fd = None
    try:
        fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        _os_lock(fd)
        return fd
    except OSError:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if not _lock_unavailable_warned:
            _lock_unavailable_warned = True
            log.warning(
                "[artifact] cannot take the store's cross-process lock at %s — store writes are "
                "serialised within this process only",
                _lock_path(),
                exc_info=True,
            )
        return None


def _release_file_lock(fd: int) -> None:
    try:
        _os_unlock(fd)
    except OSError:
        log.debug("[artifact] store unlock failed (closing the fd releases it)", exc_info=True)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


@contextlib.contextmanager
def _store_lock():
    """Hold the store: this process's thread lock, then the cross-process file lock.
    Reentrant — a nested hold neither re-opens nor releases the file lock."""
    global _FILE_LOCK_DEPTH
    with _MUTATION_LOCK:
        fd = _acquire_file_lock() if _FILE_LOCK_DEPTH == 0 else None
        _FILE_LOCK_DEPTH += 1
        try:
            yield
        finally:
            _FILE_LOCK_DEPTH -= 1
            if fd is not None:
                _release_file_lock(fd)


def serialized(fn):
    """Run ``fn`` holding the store lock (``_store_lock``) — for any path that reads the
    store, changes it, and writes it back. Read-only paths don't need it. Hold it for the
    read-modify-write and nothing longer: a waiter may be another PROCESS.

    Deliberately SYNC-only. An async wrapper that acquired this lock would block the
    event-loop thread for as long as a tool call held it, stalling unrelated requests —
    so the panel's mutating routes are plain ``def`` handlers, which FastAPI already runs
    in a worker thread. Keep them that way: making one ``async def`` would put the wait
    back on the event loop."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _store_lock():
            return fn(*args, **kwargs)

    return wrapper


# ── binary blobs (ADR 0092 D2) ───────────────────────────────────────────────
# A `file` artifact's BYTES live as sidecar files under <artifact-dir>/blobs/<id>/,
# NOT inlined into history.json — the store is read on every panel poll, so a base64
# .docx would bloat it badly. history.json keeps only {mime, filename, size, preview,
# thumb, blob:"<token>.<ext>"}; the version's ``blob`` names the sidecar file. The name
# is a random token (not the version index) so trimming to max_versions — which shifts
# indices — never mis-points a version at another version's bytes.


def _blob_root() -> Path:
    return _store_path().parent / "blobs"


def _blob_path(art_id: str, name: str) -> Path:
    """The sidecar file for ``name`` (a version's ``blob`` token) under artifact ``art_id``.
    ``name`` is sanitized to a bare filename — no path traversal out of the blob dir."""
    safe = os.path.basename(str(name))
    return _blob_root() / art_id / safe


@serialized
def _gc_blobs(store: dict) -> None:
    """Delete sidecar blob files/dirs no longer referenced by a surviving version — the
    retention sweep that pairs with _write_store's version/history trim. Best-effort: a
    filesystem hiccup must never break a store write.

    Holds the store lock (reentrant — _write_store already does): ``store`` is only the
    on-disk truth while no other process can write, and every blob write happens inside a
    locked save, so a blob this sweep sees unreferenced can't be one another process has
    just written and is about to reference."""
    root = _blob_root()
    if not root.exists():
        return
    live: dict[str, set[str]] = {}
    for a in store.get("artifacts", []):
        names = {v["blob"] for v in a.get("versions", []) if isinstance(v.get("blob"), str) and v["blob"]}
        if names:
            live[a["id"]] = names
    try:
        art_dirs = list(root.iterdir())
    except OSError:
        log.debug("[artifact] blob GC: cannot list %s", root, exc_info=True)
        return
    for art_dir in art_dirs:
        # Per-directory isolation: a failure sweeping one dir (e.g. an unexpected subdir, a
        # permissions/lock issue) must NOT abort the rest — it'd strand every other orphan.
        try:
            if not art_dir.is_dir():
                continue
            keep = live.get(art_dir.name)
            if keep is None:  # artifact gone (deleted / trimmed out) → drop its whole dir
                for f in art_dir.iterdir():
                    f.unlink(missing_ok=True)
                art_dir.rmdir()
                continue
            for f in art_dir.iterdir():  # artifact lives; drop only orphaned versions' blobs
                if f.name not in keep:
                    f.unlink(missing_ok=True)
        except OSError:
            log.debug("[artifact] blob GC hiccup on %s", art_dir, exc_info=True)


def _now() -> int:
    return int(time.time() * 1000)


def _new_id() -> str:
    return f"a-{_now()}-{secrets.token_hex(3)}"


def _migrate_legacy(it: dict) -> dict:
    """A pre-0.6 flat history item → a single-version artifact."""
    ts = it.get("ts") or _now()
    return {
        "id": str(it.get("id") or _new_id()),
        "title": it.get("title", ""),
        "kind": it.get("kind", ""),
        "versions": [{"code": it.get("code", ""), "ts": ts, "by": "agent"}],
        "created": ts,
        "updated": ts,
    }


def _read_store() -> dict:
    """``{"artifacts": [newest-first], "current": id|None}``. Tolerates a
    missing/corrupt file (→ empty) and migrates the legacy flat ``{items:[…]}`` /
    ``[…]`` shape into single-version artifacts."""
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"artifacts": [], "current": None}
    if isinstance(data, dict) and isinstance(data.get("artifacts"), list):
        arts = [a for a in data["artifacts"] if isinstance(a, dict) and a.get("versions")]
        cur = data.get("current")
        if not any(a["id"] == cur for a in arts):
            cur = arts[0]["id"] if arts else None
        return {"artifacts": arts, "current": cur}
    legacy = data.get("items") if isinstance(data, dict) else data
    if isinstance(legacy, list):
        arts = [_migrate_legacy(it) for it in legacy if isinstance(it, dict)]
        return {"artifacts": arts, "current": arts[0]["id"] if arts else None}
    return {"artifacts": [], "current": None}


def _is_pinned(art: dict) -> bool:
    """Pinned = exempt from history eviction. Strictly ``True`` — a hand-edited truthy
    string must not silently pin an artifact past the ``max_pinned`` cap."""
    return art.get("pinned") is True


def _pinned(store: dict) -> list[dict]:
    """The pinned artifacts, in store (most-recently-touched first) order."""
    return [a for a in store.get("artifacts", []) if _is_pinned(a)]


def _evict(arts: list[dict], keep: int) -> list[dict]:
    """Keep every PINNED artifact plus the ``keep`` most-recently-touched unpinned ones. Pins
    don't count toward ``keep``: with nothing pinned this is exactly ``arts[:keep]``, the
    eviction point the store has always had. The number of pins is bounded separately
    (``_config._max_pinned``, enforced by pin_artifact).

    Pinned artifacts go FIRST (a stable partition, so each group keeps its most-recently-touched-
    first order; pin_artifact moves a new pin to the front) as a downgrade guard:
    a pre-0.18 plugin knows nothing of pins and keeps just ``artifacts[:history]``, so pins at
    the front survive its writes until ``history`` newer artifacts push them out — at the back,
    where a long-lived artifact usually sits, its first write would evict them."""
    pinned = [a for a in arts if _is_pinned(a)]
    unpinned = [a for a in arts if not _is_pinned(a)]
    return pinned + unpinned[:keep]


_REPLACE_RETRY_S = 2.0  # Windows: how long a store write rides out a reader holding the file


def _replace(tmp: str, path: Path) -> None:
    """``os.replace(tmp, path)``, riding out Windows sharing violations. Readers don't take
    the store lock (the panel polls the store continuously), and Windows can't replace a file
    another handle has open — a reader mid-``read_text``, an AV scanner — so the swap fails
    with PermissionError instead of waiting. That holder lets go within moments: retry briefly.
    POSIX replaces an open file without complaint, so it never retries there."""
    deadline = time.monotonic() + _REPLACE_RETRY_S
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if sys.platform != "win32" or time.monotonic() >= deadline:
                raise
        time.sleep(0.005)


@serialized
def _write_store(store: dict) -> None:
    """Persist ``store`` (evicting + version-trimming first) and sweep orphaned blobs.

    Always runs under the store lock — reentrant, so inside a ``serialized`` path it's
    free — which keeps the blob sweep from deleting a blob that a writer in another process
    has just referenced. It does NOT make a caller's read-modify-write atomic: a caller that
    read the store must hold the lock across that read too (``serialized``)."""
    max_versions = _config._max_versions()
    store["artifacts"] = _evict(store.get("artifacts", []), _config._max_history())
    # Version trimming applies to pinned artifacts too, deliberately: a pin keeps the artifact
    # (its id keeps resolving), not every edit ever made to it — a long-lived document edited
    # daily would otherwise grow history.json without bound, and it's read on every panel poll.
    for a in store["artifacts"]:
        if len(a.get("versions", [])) > max_versions:
            a["versions"] = a["versions"][-max_versions:]
    path = _store_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh)
        _replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _gc_blobs(store)  # drop sidecar blobs orphaned by the version/history trim above


def _find(store: dict, art_id: str | None) -> dict | None:
    return next((a for a in store["artifacts"] if a["id"] == art_id), None)


def _is_file(art: dict) -> bool:
    """A `file` artifact (ADR 0092 D2) — bytes in a sidecar blob, `code` is a derived
    preview. Text-source edits (update/rewrite/panel PUT) must NOT touch these (they'd
    append a version with no blob → orphaned bytes + a broken card), and save_file_artifact
    must NOT revise a non-file artifact (its kind stays wrong → renders the preview as raw
    source). Both directions are guarded on this."""
    return art.get("kind") == "file"


def _file_not_editable(art: dict) -> str:
    """The refusal returned when a text-source edit tool targets a file artifact."""
    return (
        f"Artifact {art['id']} is a file artifact (its source is a file on disk, not "
        f"editable text). Re-generate the file and re-save it with save_file_artifact."
    )


def _too_big(code: str) -> str | None:
    limit = _config._max_code_bytes()
    if len(code.encode("utf-8")) > limit:
        return (
            f"Artifact too large ({len(code.encode('utf-8')) // 1024} KB > "
            f"{limit // 1024} KB). Trim the source or split it; raise "
            f"the artifact max_code_kb setting if you really need more."
        )
    return None


def _touch(store: dict, art: dict) -> None:
    """Move ``art`` to the front (most-recently-touched first) and make it current."""
    store["current"] = art["id"]
    store["artifacts"] = [art] + [a for a in store["artifacts"] if a["id"] != art["id"]]


# Set at register() so the tool can broadcast on the bus (ADR 0039). Under the default runtime the
# tool runs in the server process where the bus is wired; the dot lights from artifact.created.
_REGISTRY = None


def _emit(event: str, data: dict) -> None:
    try:
        if _REGISTRY is not None:
            _REGISTRY.emit(event, data)  # → "artifact.<event>" (namespace-guarded)
    except Exception:  # noqa: BLE001 — emitting must never break the tool
        log.debug("[artifact] emit(%s) failed", event, exc_info=True)


def _new_version(code: str, by: str = "agent", extra: dict | None = None) -> dict:
    """A fresh version record. ``by`` is provenance: "agent" (a tool) or "user" (panel edit).
    ``extra`` merges in kind-specific fields — for a `file` version, ``file`` metadata
    ({mime, filename, size, thumb}) and the ``blob`` sidecar token (ADR 0092 D2)."""
    v = {"code": code, "ts": _now(), "by": by}
    if extra:
        v.update(extra)
    return v


def _commit_version(store: dict, art: dict, code: str, by: str = "agent", extra: dict | None = None) -> int:
    """Append a version to ``art``, move it to the front, persist, broadcast ``updated``, and
    return the new 1-based version count. The shared tail of update/rewrite_artifact + the
    panel's user-edit PUT — one place owns append→touch→write→emit ordering."""
    nv = _new_version(code, by, extra)
    art["versions"].append(nv)
    # Lifetime total, counted BEFORE _write_store's trim — unlike len(art["versions"]) (the
    # number reported back to the caller below), this never shrinks. resolve_for_bundle
    # (#2681) needs it to tell "never trimmed" from "trimmed", which the returned/reported
    # count alone can't: that count is POST-trim, so it can repeat across different commits.
    art["version_count"] = art.get("version_count", len(art["versions"]) - 1) + 1
    art["updated"] = nv["ts"]
    _touch(store, art)
    _write_store(store)  # may trim to _config._max_versions(), so count AFTER
    v = len(art["versions"])
    _emit("updated", {"id": art["id"], "version": v})
    return v


# ── version identity ─────────────────────────────────────────────────────────────────
# A version NUMBER is a list position, and at the max_versions cap every commit trims the front,
# shifting every survivor down a slot: the slot "version 5" named a moment ago now holds the next
# edit. Anything that has to find ONE version again later — a render verdict the tool waits for —
# identifies it by (lifetime number, ts) instead. The lifetime number needs no stored key: commits
# only append and trims only drop from the front, so the version at 1-based position p has lifetime
# number version_count - len(versions) + p. The ts cross-checks it (a store rewritten by something
# that didn't keep version_count can't make it point at the wrong version).


def _version_key(art: dict) -> tuple[int, int]:
    """The stable identity of ``art``'s LATEST version: ``(lifetime number, ts)``."""
    vers = art["versions"]
    return art.get("version_count", len(vers)), vers[-1].get("ts")


def _locate_version(art: dict, key: tuple[int, int]) -> int | None:
    """The CURRENT 1-based position of the version ``key`` names, or ``None`` once it has been
    trimmed away (or the store no longer agrees about it)."""
    n, ts = key
    vers = art.get("versions") or []
    pos = n - (art.get("version_count", len(vers)) - len(vers))
    if 1 <= pos <= len(vers) and vers[pos - 1].get("ts") == ts:
        return pos
    return None
