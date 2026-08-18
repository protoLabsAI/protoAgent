"""The agent-facing tools: show / save-file / update / rewrite / list / get / check / delete."""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from langchain_core.tools import tool

from . import _config, _preview, _render_status, _store

log = logging.getLogger("protoagent.plugins.artifact")

_KINDS = {"html", "svg", "mermaid", "react", "markdown"}

# ── full-body-write nudge (#2257) ────────────────────────────────────────────
# Iterating an artifact by re-SAVING it (save_file_artifact / rewrite_artifact)
# round-trips the entire body through the conversation every time — the field
# case was 11 saves in one turn. Once a short window sees repeated full-body
# writes to the SAME artifact, the tool result carries a nudge toward batching
# (or update_artifact, the cheap targeted path — which is deliberately exempt).
# A nudge, never a block: turns keep working.
_SAVE_NUDGE_AFTER = 3
_SAVE_NUDGE_WINDOW_MS = 10 * 60 * 1000
_recent_full_saves: dict[str, list[int]] = {}


def _save_nudge(art_id: str) -> str:
    """Record one full-body write to ``art_id``; the nudge string once the recent
    window crosses the threshold, else empty."""
    now = _store._now()
    stamps = [t for t in _recent_full_saves.get(art_id, []) if now - t <= _SAVE_NUDGE_WINDOW_MS]
    stamps.append(now)
    _recent_full_saves[art_id] = stamps
    if len(stamps) < _SAVE_NUDGE_AFTER:
        return ""
    return (
        f"\n\nNOTE: that's full-body write #{len(stamps)} to this artifact in a few minutes — "
        "each one round-trips the entire content through the conversation. Batch the remaining "
        "changes and write ONCE when the content is complete (for code artifacts, "
        "update_artifact makes targeted edits without resending the body)."
    )


@tool
def save_file_artifact(path: str, title: str = "", artifact_id: str = "") -> str:
    """Save a GENERATED FILE (a .docx / .xlsx / .pptx / .pdf / image / text file you already
    wrote to disk) into the Artifact panel as a VERSIONED download artifact — so the file gets
    the same edit-history + inspectable panel treatment as a rendered artifact, and the user
    can download it or diff it across versions.

    Use this right AFTER a skill writes a document to disk (e.g. cowork's docx/xlsx/pptx/pdf
    skills, a generated report or image): pass the file ``path``. The panel stores the bytes,
    shows a download card with a preview typed by content (csv/tsv → a real table, .md →
    rendered prose, .json → pretty-printed; docx→text, xlsx→sheet table, pptx→slide outline,
    pdf→text; images get a thumbnail; other text files → plain text), and offers a Download
    button.

    COMPOSE the file completely, then save ONCE — do not save revision after revision while
    you iterate in a single turn; every save round-trips the full file through the
    conversation. Batch your edits and save when the content is done.

    ``title`` is an optional label. To save a NEW revision of a file you saved before (so it
    becomes v2, v3… of the same artifact rather than a new panel entry), pass that artifact's
    ``artifact_id`` (see ``list_artifacts``). Returns the artifact id.

    This is for FILES on disk. To render HTML/SVG/React/Markdown/charts, use ``show_artifact``.
    """
    p = Path(os.path.expanduser(path or "")).resolve()
    if not p.exists() or not p.is_file():
        return f"No file at {path!r}. Write the file first, then pass its path."
    data = p.read_bytes()
    limit = _config._max_blob_bytes()
    if len(data) > limit:
        return (
            f"File too large ({len(data) // 1024} KB > {limit // 1024} KB). Raise the artifact "
            f"max_blob_kb setting if you really need to store a file this big."
        )
    mime = _preview._guess_mime(p)
    preview = _preview._extract_preview(p, data, mime)
    thumb = _preview._thumbnail(data, mime)
    ext = p.suffix.lower().lstrip(".") or "bin"

    store = _store._read_store()
    art = _store._find(store, artifact_id) if artifact_id else None
    if artifact_id and art is None:
        return f"No artifact {artifact_id!r} to revise. Use list_artifacts, or omit artifact_id for a new one."
    if art is not None and not _store._is_file(art):
        return (
            f"Artifact {artifact_id!r} is a {art['kind']} artifact, not a file — save_file_artifact "
            f"can only revise a file artifact. Omit artifact_id to create a new one."
        )

    art_id = art["id"] if art else _store._new_id()
    blob_name = f"{secrets.token_hex(8)}.{ext}"
    blob_file = _store._blob_path(art_id, blob_name)
    blob_file.parent.mkdir(parents=True, exist_ok=True)
    blob_file.write_bytes(data)

    file_meta = {
        "mime": mime,
        "filename": p.name,
        "size": len(data),
        "thumb": thumb or "",
    }
    if art is None:
        nv = _store._new_version(preview, extra={"file": file_meta, "blob": blob_name})
        art = {
            "id": art_id,
            "title": title or p.name,
            "kind": "file",
            "versions": [nv],
            "version_count": 1,
            "created": nv["ts"],
            "updated": nv["ts"],
        }
        store["artifacts"].insert(0, art)
        store["current"] = art_id
        _store._write_store(store)
        _store._emit("created", {"id": art_id, "kind": "file", "title": art["title"]})
        v = 1
    else:
        if title:
            art["title"] = title
        v = _store._commit_version(store, art, preview, extra={"file": file_meta, "blob": blob_name})
    kb = len(data) // 1024
    return (
        f"Saved file artifact {art_id} → v{v}: {p.name} ({mime}, {kb} KB) — now in the "
        f"Artifact panel with a preview and a Download button." + _save_nudge(art_id)
    )


@tool
def show_artifact(kind: str, code: str, title: str = "") -> str:
    """CREATE a new generative-UI artifact in the console's Artifact panel.

    ``kind`` is one of: "html" (a full or partial HTML document), "svg" (inline SVG markup),
    "mermaid" (a Mermaid diagram definition), "markdown" (a Markdown document — rendered with
    design-system prose styling; ```mermaid fences become live diagrams), or "react" (a
    self-contained React component script; name your top-level component ``App`` and it
    AUTO-MOUNTS into ``#root`` (no manual ``createRoot(...).render`` needed — though an explicit
    render still works). React, ReactDOM and Babel are provided, and it can ``import`` from a
    curated offline set — ``d3``, ``chart.js``, ``lucide``, and ``@pl/ui`` design-system
    components like ``Button``/``Card``/``Stat``/``Icon``). ``code`` is the source; ``title`` is
    an optional label. Runs sandboxed — it can't access the console.

    After creating it, CHECK IT RENDERED: this reply carries the render verdict when the panel is
    open; otherwise call ``check_artifact``. Fix any reported error before moving on.

    To EDIT what you just made, use ``update_artifact`` (a small targeted change) or
    ``rewrite_artifact`` (a full replacement) — they iterate the SAME artifact as a new
    version instead of cluttering the panel with near-duplicates.

    Use this for free-form or custom-rendered visuals — a chart, a Mermaid diagram, bespoke
    HTML/React/SVG (it runs sandboxed, heavier). For plain STRUCTURED DATA — a table, a
    metrics block, a step/plan list — prefer ``show_component`` instead (it renders inline in
    the chat, data-only, no sandbox, lighter). Rule of thumb: a generated VISUAL → this tool;
    a data SHAPE → a component. Prefer either over writing files when the user just wants to
    SEE something rendered. Returns the artifact id.
    """
    k = (kind or "").strip().lower()
    if k not in _KINDS:
        return f"Unknown artifact kind {kind!r}. Use one of: {', '.join(sorted(_KINDS))}."
    code = code or ""
    if err := _store._too_big(code):
        return err
    store = _store._read_store()
    nv = _store._new_version(code)
    art = {
        "id": _store._new_id(),
        "title": title or "",
        "kind": k,
        "versions": [nv],
        "version_count": 1,
        "created": nv["ts"],
        "updated": nv["ts"],
    }
    store["artifacts"].insert(0, art)
    store["current"] = art["id"]
    _store._write_store(store)
    _store._emit("created", {"id": art["id"], "kind": k, "title": title or ""})
    msg = (
        f"Created {k} artifact {art['id']} ({len(code)} chars) — now showing in the Artifact "
        f"panel. Edit it with update_artifact(old_string, new_string) or rewrite_artifact(code)."
    )
    return msg + _render_status._render_suffix(art["id"], 1)


@tool
def update_artifact(old_string: str, new_string: str, artifact_id: str = "") -> str:
    """Make a TARGETED edit to an existing artifact: replace ``old_string`` with ``new_string``
    in its current source, creating a new version. ``old_string`` must match the current source
    EXACTLY ONCE (whitespace included) — add surrounding context to disambiguate if needed.
    Defaults to the most-recent artifact; pass ``artifact_id`` to target another (see
    ``list_artifacts``). Prefer this over ``rewrite_artifact`` for small changes — it's the fast
    path and keeps the version history clean.
    """
    if not old_string:
        return "old_string must not be empty."
    store = _store._read_store()
    art = _store._find(store, artifact_id or store["current"])
    if art is None:
        return "No artifact to update. Create one with show_artifact first."
    if _store._is_file(art):
        return _store._file_not_editable(art)
    src = art["versions"][-1]["code"]
    n = src.count(old_string)
    if n == 0:
        return (
            "old_string not found in the current source — it must match exactly (whitespace "
            "included). Read the current source with get_artifact, then craft an exact old_string."
        )
    if n > 1:
        return f"old_string matches {n} times — it must match exactly once. Add surrounding context to make it unique."
    new_code = src.replace(old_string, new_string, 1)
    if err := _store._too_big(new_code):
        return err
    v = _store._commit_version(store, art, new_code)
    return f"Updated artifact {art['id']} → version {v}." + _render_status._render_suffix(art["id"], v)


@tool
def rewrite_artifact(code: str, title: str = "", artifact_id: str = "") -> str:
    """Replace an artifact's ENTIRE source with ``code``, creating a new version (the kind is
    kept). Use this for a large change where a targeted ``update_artifact`` would be awkward;
    prefer ``update_artifact`` for small edits — a rewrite round-trips the full body through
    the conversation every time, so batch your changes into one rewrite rather than iterating
    rewrite-by-rewrite. Optionally update the ``title``. Defaults to the most-recent artifact;
    pass ``artifact_id`` to target another.
    """
    code = code or ""
    if err := _store._too_big(code):
        return err
    store = _store._read_store()
    art = _store._find(store, artifact_id or store["current"])
    if art is None:
        return "No artifact to rewrite. Create one with show_artifact first."
    if _store._is_file(art):
        return _store._file_not_editable(art)
    if title:
        art["title"] = title
    v = _store._commit_version(store, art, code)
    return (
        f"Rewrote artifact {art['id']} → version {v}."
        + _save_nudge(art["id"])
        + _render_status._render_suffix(art["id"], v)
    )


@tool
def list_artifacts() -> str:
    """List the artifacts in the panel (newest first) with id, kind, title and version count,
    so you can target ``update_artifact`` / ``rewrite_artifact`` / ``delete_artifact`` at a
    specific one. Read-only."""
    store = _store._read_store()
    if not store["artifacts"]:
        return "No artifacts yet. Create one with show_artifact."
    lines = []
    for a in store["artifacts"]:
        cur = "  · current" if a["id"] == store["current"] else ""
        lines.append(f"{a['id']}  [{a['kind']}]  {a['title'] or '(untitled)'}  · v{len(a['versions'])}{cur}")
    return "Artifacts (newest first):\n" + "\n".join(lines)


@tool
def get_artifact(artifact_id: str = "") -> str:
    """Return the CURRENT source code of an artifact (with its kind, title and version).

    This is how you TAKE OVER an artifact you didn't create — e.g. one from an earlier
    session or another agent: read the source here, then iterate it with ``update_artifact``
    (craft an exact ``old_string`` from what you read) or ``rewrite_artifact``. ``list_artifacts``
    only shows metadata; this returns the actual code. Defaults to the current artifact; pass
    ``artifact_id`` (see ``list_artifacts``) to target another. Read-only.
    """
    store = _store._read_store()
    art = _store._find(store, artifact_id or store["current"])
    if art is None:
        return "No artifact to read. Use list_artifacts to see the ids, or show_artifact to create one."
    code = art["versions"][-1]["code"]
    title = art["title"] or "(untitled)"
    v = len(art["versions"])
    return f"Artifact {art['id']}  [{art['kind']}]  {title}  · v{v} — current source:\n\n{code}"


@tool
def check_artifact(artifact_id: str = "") -> str:
    """Check whether an artifact's latest version actually RENDERED — the feedback channel for
    the code→render→fix loop. Rendering happens async in the browser, so a create/edit can
    return before the result is known; call this (or just iterate) to see how it went.

    Returns the render verdict: rendered cleanly, FAILED with the captured error message, or
    "no result yet" (the panel is closed / not showing this version — open the Artifact panel).
    Defaults to the current artifact; pass ``artifact_id`` (see ``list_artifacts``) to target
    another. Read-only.

    Safe to call right after creating/editing: if a result isn't in yet but the panel is live,
    it waits briefly for the verdict rather than returning "no result yet"."""
    store = _store._read_store()
    art = _store._find(store, artifact_id or store["current"])
    if art is None:
        return "No artifact to check. Use list_artifacts to see the ids, or show_artifact to create one."
    v = len(art["versions"])
    # An already-recorded verdict reads instantly; otherwise wait briefly IFF a renderer is live
    # (so an immediate post-render check returns the real result, not a premature "no result yet").
    r = _render_status._version_render(art, v) or _render_status._await_render(art["id"], v)
    if r is None:
        return (
            f"Artifact {art['id']} v{v}: no render result yet — the Artifact panel may be "
            "closed or not showing this version. Open it to render."
        )
    if r.get("ok"):
        return f"Artifact {art['id']} v{v}: rendered cleanly."
    err = str(r.get("error") or "render failed").strip()
    return f"Artifact {art['id']} v{v}: render FAILED —\n  {err}\nFix it with update_artifact / rewrite_artifact."


@tool
def delete_artifact(artifact_id: str) -> str:
    """Delete an artifact (all its versions) from the panel — for cleanup. The user can also
    delete from the panel's trash button. Pass the ``artifact_id`` (see ``list_artifacts``)."""
    store = _store._read_store()
    if _store._find(store, artifact_id) is None:
        return f"No artifact {artifact_id!r}. Use list_artifacts to see the ids."
    store["artifacts"] = [a for a in store["artifacts"] if a["id"] != artifact_id]
    if store["current"] == artifact_id:
        store["current"] = store["artifacts"][0]["id"] if store["artifacts"] else None
    _store._write_store(store)
    _store._emit("deleted", {"id": artifact_id})
    return f"Deleted artifact {artifact_id}."
