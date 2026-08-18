"""The artifact store: file-backed version chains + sidecar blobs (ADR 0092 D2).

State is persisted to a FILE (instance-scoped), not module memory — under the ACP
runtime the tool executes in the operator-MCP process while the route is served by
the main process, so the two only share state through disk.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path

from . import _config

log = logging.getLogger("protoagent.plugins.artifact")

# ── the store ──────────────────────────────────────────────────────────────────
# An artifact is a VERSION CHAIN: {id, kind, title, versions:[{code, ts, by}], …}.
# show_artifact creates one; update_artifact/rewrite_artifact append a version (the
# proven Claude "update vs rewrite" model — iterate the same artifact, don't spam the
# panel with near-duplicates). The file is {"artifacts": [newest-first], "current": id}.


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


def _gc_blobs(store: dict) -> None:
    """Delete sidecar blob files/dirs no longer referenced by a surviving version — the
    retention sweep that pairs with _write_store's version/history trim. Best-effort: a
    filesystem hiccup must never break a store write."""
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


def _write_store(store: dict) -> None:
    max_versions = _config._max_versions()
    store["artifacts"] = store.get("artifacts", [])[: _config._max_history()]
    for a in store["artifacts"]:
        if len(a.get("versions", [])) > max_versions:
            a["versions"] = a["versions"][-max_versions:]
    path = _store_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh)
        os.replace(tmp, path)
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
