"""The agent-facing tools: show / save-file / update / rewrite / list / get / check / pin / delete."""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from . import _config, _docx, _links, _pdfview, _preview, _ref, _render_status, _slides, _store

log = logging.getLogger("protoagent.plugins.artifact")

_KINDS = {"html", "svg", "mermaid", "react", "markdown", "vega-lite"}

# Kinds that can render in the inline frame (ADR 0118 D2). `markdown` (and files/decks on the
# save side) render text/pages, not through the sandboxed frame, so they stay in the panel.
_INLINE_KINDS = frozenset({"html", "svg", "mermaid", "react", "vega-lite"})


def _resolve_placement(placement: str | None, kind: str) -> tuple[bool, str]:
    """Whether this call emits an INLINE artifact-ref, plus a note for the tool result when an
    explicit inline request falls back to the panel (ADR 0118 D2; inline-by-default as of the
    2026-10-10 operator decision).

    Placement DEFAULTS to inline: with no explicit placement (``None``), the frame-renderable
    kinds (``_INLINE_KINDS``) render inline and every other kind (markdown, decks, PDFs, files)
    falls back to the panel SILENTLY — no note. The "not available inline" note is reserved for a
    caller who EXPLICITLY passed ``placement="inline"`` on an unsupported kind. An explicit
    ``placement="panel"`` always opens the panel."""
    norm = (placement or "").strip().lower()
    if norm not in ("", "inline"):
        # Any explicit placement other than "inline" — notably "panel" — opens the panel, silently.
        return False, ""
    if kind in _INLINE_KINDS:
        return True, ""
    # A non-frame kind can't render inline → panel. The fallback note is only for an EXPLICIT
    # inline request; under the default (``placement is None``) the panel is silent.
    if placement is not None:
        return False, (
            f"\n\nNote: {kind} can't render inline — showing it in the Artifact panel instead. "
            f"Inline placement is available for: {', '.join(sorted(_INLINE_KINDS))}."
        )
    return False, ""


# Bounds on the write-time spec walk. A real chart spec is a few levels deep; the rows in
# `data.values` are never walked (below), so the node budget counts structure, not data.
_SPEC_MAX_DEPTH = 64
_SPEC_MAX_NODES = 100_000
# Keys holding INLINE rows. A row is data, not a load: a column named `url` (a table of page
# hits) is a value, so the walk never descends into these.
_INLINE_ROWS = frozenset({"values", "datasets"})


def _remote_data(spec: dict) -> str | None:
    """"remote" when a `url` sits inside a data definition, "deep"/"large" past the walk's bounds,
    else None. ITERATIVE on purpose: a hostile spec nested hundreds deep must be a clean refusal,
    never a RecursionError out of a tool (or out of ``show_service``, which promises not to raise)."""
    stack: list[tuple[object, bool, int]] = [(spec, False, 0)]
    nodes = 0
    while stack:
        o, in_data, depth = stack.pop()
        nodes += 1
        if depth > _SPEC_MAX_DEPTH:
            return "deep"
        if nodes > _SPEC_MAX_NODES:
            return "large"
        if isinstance(o, dict):
            for k, v in o.items():
                if in_data and k == "url":
                    return "remote"
                if k in _INLINE_ROWS:
                    continue
                if isinstance(v, (dict, list)):
                    stack.append((v, in_data or k == "data", depth + 1))
        elif isinstance(o, list):
            stack.extend((v, in_data, depth + 1) for v in o if isinstance(v, (dict, list)))
    return None


def _vega_problem(code: str) -> str | None:
    """Why ``code`` can't be a ``vega-lite`` artifact, or None (ADR 0116).

    Checked when a version is WRITTEN, so a malformed spec is refused with a reason the model can
    act on instead of committing a version that only fails in the panel. The frame re-checks all of
    it (and its loader refuses every load regardless): this is feedback, the frame is the fence."""
    try:
        spec = json.loads(code)
    except RecursionError:  # json's own parser recurses: a spec nested ~1000 deep
        return f"A vega-lite spec may nest at most {_SPEC_MAX_DEPTH} levels — this one is far deeper."
    except ValueError as e:
        return f"A vega-lite artifact's code must be a JSON Vega-Lite spec — it isn't valid JSON ({e})."
    if not isinstance(spec, dict):
        return "A vega-lite artifact's code must be a JSON object (a Vega-Lite spec)."
    why = _remote_data(spec)
    if why == "deep":
        return f"A vega-lite spec may nest at most {_SPEC_MAX_DEPTH} levels — flatten it."
    if why == "large":
        return f"A vega-lite spec's structure (outside its inline rows) may have at most {_SPEC_MAX_NODES} nodes."
    if why == "remote":
        return (
            "Charts render INLINE data only — put the rows in `data.values` (the data plugin's "
            "data_chart does this for you); a `data.url` is never loaded."
        )
    return None


def _kind_problem(kind: str, code: str) -> str | None:
    """A per-kind content check for a version about to be written; None when it's fine."""
    return _vega_problem(code) if kind == "vega-lite" else None

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

# pin_artifact's at-the-cap refusal names the held pins so the agent can pick one to unpin —
# but only this many; past it the reply points at list_artifacts instead of growing unbounded.
_PIN_NAMES_SHOWN = 5


def _chat_session(state: Any) -> str:
    """The calling chat's session id, from the tool's injected graph state (#4138).
    ``current_session_id()`` alone reads EMPTY inside a tool body; ``_session_id_from`` prefers
    the state and keeps that contextvar only as a fallback. ``""`` when neither knows it — an
    ACP tool process, a direct call — and the artifact then records no chat (evictable as before)."""
    try:
        from tools.session import _session_id_from

        return _session_id_from(state)
    except Exception:  # noqa: BLE001 — no host: read the state directly
        return (state.get("session_id") or "").strip() if isinstance(state, dict) else ""


def _renders_ui(state: Any) -> bool:
    """Whether this turn's client can display an artifact at all (#4144) — the SDK's reading of
    the injected graph state's ``renders_ui``, so the plugin and every other caller agree."""
    from graph.sdk import renders_ui

    return renders_ui(state)


# The text-only client's tail (#4144): said once, on every create/edit, so the model answers in
# text instead of pointing at a chat view this client doesn't have — or looping check_artifact.
_TEXT_ONLY_NOTE = (
    " This client renders text only: it can't display the artifact, and its render can't be "
    "checked (don't call check_artifact). Answer in text, quote the artifact id above so the user "
    "can open it later in the console, and offer the source (get_artifact) if the user wants it. "
    "Don't describe its buttons or other interactive controls as usable from here — this client "
    "can't click them."
)


def _stamp_ref(art: dict, session_id: str) -> None:
    """Record that this call's ``artifact-ref`` goes out in ``session_id``'s chat, so eviction
    keeps ``art`` while that chat exists (#4138). Only when the chip is emitted at all — on a
    host without the component seam no chat ever shows it."""
    if _ref.EMIT:
        _store._stamp_chat_session(art, session_id)


def _busy_reply(fn):
    """A mutating tool whose store stays locked past its bound (``_store.StoreLockTimeout`` —
    another process is wedged holding it) replies with why and that nothing changed, rather than
    raising out of the agent's tool call. The lock is taken before any change, so a retry is safe."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _store.StoreLockTimeout as e:
            return str(e)

    return wrapper


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


def _resolve_file(path: str, project: str = "") -> tuple[Path | None, str]:
    """``path`` (+ optional fs ``project``) → the file to save, or ``(None, why)``.

    Speaks the fs tools' path language, so a file the agent just found with
    ``find_files``/``read_file`` (a project + a relative path) saves as-is:

    * ``project`` given → resolved inside that project's fence (``..`` can't escape it);
    * an absolute or ``~`` path → used as-is (unchanged behaviour);
    * a relative path with no project → looked up in every registered project. Exactly one
      hit wins; more than one is ambiguous (the agent must name the project); none falls back
      to the old cwd-relative lookup before giving up.
    """
    raw = (path or "").strip()
    registry = _links._registry()
    if project:
        if registry is None:
            return None, f"project {project!r} given, but this agent has no filesystem projects — pass an absolute path."
        try:
            p = registry.resolve(project, raw)
        except ValueError as e:
            return None, f"{e}."
        if not p.is_file():
            return None, f"No file at {raw!r} in project {project!r}. Write the file first, then pass its path."
        return p, ""
    p = Path(os.path.expanduser(raw))
    if p.is_absolute():
        p = p.resolve()
        return (p, "") if p.is_file() else (None, f"No file at {raw!r}. Write the file first, then pass its path.")
    hits: list[tuple[str, Path]] = []
    names = registry.names() if registry is not None else []
    for name in names:
        try:
            cand = registry.resolve(name, raw)
        except ValueError:
            continue
        if cand.is_file():
            hits.append((name, cand))
    if len(hits) == 1:
        return hits[0][1], ""
    if len(hits) > 1:
        where = ", ".join(n for n, _ in hits)
        return None, f"{raw!r} exists in more than one project ({where}) — pass `project` to pick one."
    cwd = p.resolve()
    if cwd.is_file():
        return cwd, ""
    looked = f" (looked in project(s): {', '.join(names)})" if names else ""
    return None, f"No file at {raw!r}{looked}. Write the file first, then pass its path."


@tool
@_busy_reply
def save_file_artifact(
    path: str,
    title: str = "",
    artifact_id: str = "",
    project: str = "",
    state: Annotated[Any, InjectedState] = None,
) -> str:
    """Save a GENERATED FILE (a .docx / .xlsx / .pptx / .pdf / image / text file you already
    wrote to disk) into the Artifact panel as a VERSIONED download artifact — so the file gets
    the same edit-history + inspectable panel treatment as a rendered artifact, and the user
    can download it or diff it across versions.

    Use this right AFTER a skill writes a document to disk (e.g. cowork's docx/xlsx/pptx/pdf
    skills, a generated report or image): pass the file ``path``. The panel stores the bytes,
    shows a preview typed by content, and offers a Download button:
    .docx → the real rendered PAGES (Word styles, tables, images, headers/footers);
    .pdf → the real rendered PAGES; .pptx → the real rendered SLIDES (each keeps its
    extracted text beneath); .csv/.tsv → a table (delimiter sniffed, numbers right-aligned);
    .xlsx → one table per sheet; .md → rendered prose; .json → pretty-printed; images → a
    thumbnail; other text files → plain text.

    COMPOSE the file completely, then save ONCE — do not save revision after revision while
    you iterate in a single turn; every save round-trips the full file through the
    conversation. Batch your edits and save when the content is done.

    ``title`` is an optional label. To save a NEW revision of a file you saved before (so it
    becomes v2, v3… of the same artifact rather than a new panel entry), pass that artifact's
    ``artifact_id`` (see ``list_artifacts``). Returns the artifact id.

    ``path`` takes the same form as the fs tools: a path relative to a filesystem project
    (pass ``project``, or omit it when only one project has that file — the default
    ``workspace`` included), or an absolute path.

    This is for FILES on disk. To render HTML/SVG/React/Markdown/charts, use ``show_artifact``.
    """
    p, why = _resolve_file(path, project)
    if p is None:
        return why
    prepared = _prepare_file(p)
    if isinstance(prepared, str):
        return prepared
    msg, saved = _save_file(**prepared.fields(), title=title, artifact_id=artifact_id, session_id=_chat_session(state))
    return msg + _save_nudge(saved[0]) + saved[2] if saved else msg


def _too_large(size: int) -> str:
    limit = _config._max_blob_bytes()
    if size <= limit:
        return ""
    return (
        f"File too large ({size // 1024} KB > {limit // 1024} KB). Raise the artifact "
        f"max_blob_kb setting (Settings → Artifact → max blob size, or ARTIFACT_MAX_BLOB_KB) "
        f"if you really need to store a file this big."
    )


@dataclasses.dataclass(frozen=True)
class _PreparedFile:
    """Everything a file version stores, built OUTSIDE the store lock (see ``_prepare_file``)."""

    filename: str
    data: bytes
    mime: str
    preview: str
    thumb: str | None
    ext: str
    slides: dict | None
    pdf: dict | None
    docx: dict | None

    def fields(self) -> dict:
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}


def _prepare_file(p: Path) -> _PreparedFile | str:
    """Read ``p`` and build everything a file version stores, or the refusal text. Shared by the
    ``save_file_artifact`` tool and the ``artifact.save_file`` service (#4253), so both get the
    same blob cap, mime guess, preview, thumbnail and per-type preflights.

    The size is checked on the STAT before reading, so an oversize file is refused without
    pulling it into memory, and again on the bytes read (the file may have grown since)."""
    try:
        if err := _too_large(p.stat().st_size):
            return err
        data = p.read_bytes()
    except OSError as e:
        return f"Couldn't read {p.name!r}: {e}."
    if err := _too_large(len(data)):
        return err
    # The slow part — reading the file, extracting its preview (up to 50 PDF/DOCX pages),
    # thumbnailing — happens BEFORE the store lock: it touches no store state, and the lock is
    # cross-process, so holding it here would stall every other writer in every process (the
    # panel's /render-status and edits) for as long as a big file takes to parse.
    mime = _preview._guess_mime(p)
    preview = _preview._extract_preview(p, data, mime)
    thumb = _preview._thumbnail(data, mime)
    # A deck gets the slide renderer's safety preflight (caps on size / entries / inflation /
    # image pixels) — the panel only parses it in the browser when this says it may.
    slides = _slides.preflight(data) if _slides.is_slides(p.name, mime) else None
    # A PDF gets the page renderer's safety preflight (size / page count / decoded stream bytes /
    # image pixels) — the panel only draws its pages when this says it may.
    pdf = _pdfview.preflight(data) if _pdfview.is_pdf(p.name, mime) else None
    # A Word document gets the docx renderer's preflight (zip inflation + image pixel budget).
    docx = _docx.preflight(data) if _docx.is_docx(p.name, mime) else None
    ext = p.suffix.lower().lstrip(".") or "bin"
    return _PreparedFile(p.name, data, mime, preview, thumb, ext, slides, pdf, docx)


@_store.serialized
def _save_file(
    filename: str,
    data: bytes,
    mime: str,
    preview: str,
    thumb: str | None,
    ext: str,
    title: str,
    artifact_id: str,
    slides: dict | None = None,
    pdf: dict | None = None,
    docx: dict | None = None,
    session_id: str = "",
) -> tuple[str, tuple[str, int, str] | None]:
    """save_file_artifact's store read-modify-write → ``(msg, None)`` for a refusal, or
    ``(msg, (art_id, version, ref_tail))`` for a write (the chip tail built inside the lock, from
    the art this call committed — the caller appends it last). The blob is written INSIDE the lock on
    purpose: the blob sweep runs under it too, so it can never see (and delete) a blob that
    is on disk but not yet referenced by the store."""
    store = _store._read_store()
    art = _store._find(store, artifact_id) if artifact_id else None
    if artifact_id and art is None:
        return f"No artifact {artifact_id!r} to revise. Use list_artifacts, or omit artifact_id for a new one.", None
    if art is not None and not _store._is_file(art):
        return (
            f"Artifact {artifact_id!r} is a {art['kind']} artifact, not a file — save_file_artifact "
            f"can only revise a file artifact. Omit artifact_id to create a new one."
        ), None

    art_id = art["id"] if art else _store._new_id()
    blob_name = f"{secrets.token_hex(8)}.{ext}"
    blob_file = _store._blob_path(art_id, blob_name)
    blob_file.parent.mkdir(parents=True, exist_ok=True)
    blob_file.write_bytes(data)

    file_meta = {
        "mime": mime,
        "filename": filename,
        "size": len(data),
        "thumb": thumb or "",
    }
    if slides is not None:
        file_meta["slides"] = slides
    if pdf is not None:
        file_meta["pdf"] = pdf
    if docx is not None:
        file_meta["docx"] = docx
    if art is None:
        nv = _store._new_version(preview, extra={"file": file_meta, "blob": blob_name})
        art = {
            "id": art_id,
            "title": title or filename,
            "kind": "file",
            "versions": [nv],
            "version_count": 1,
            "created": nv["ts"],
            "updated": nv["ts"],
        }
        _stamp_ref(art, session_id)
        store["artifacts"].insert(0, art)
        store["current"] = art_id
        _store._write_store(store)
        _store._emit("created", {"id": art_id, "kind": "file", "title": art["title"]})
        v = 1
    else:
        if title:
            art["title"] = title
        _stamp_ref(art, session_id)
        v = _store._commit_version(store, art, preview, extra={"file": file_meta, "blob": blob_name})
    kb = len(data) // 1024
    return (
        f"Saved file artifact {art_id} → v{v}: {filename} ({mime}, {kb} KB) — now in the "
        f"Artifact panel with a preview and a Download button."
    ), (art_id, v, _ref.ref_tail(art))


def _service_roots(plugin_id: str, roots: Any) -> tuple[list[Path], str]:
    """The directories ``artifact.save_file`` may read from for ``plugin_id``: its
    ``sdk.plugin_store`` plus each EXTRA root the caller names, or ``([], why)``.

    An extra root must be an existing absolute directory, and never a filesystem root or the
    home directory itself — those would make the containment check meaningless."""
    out: list[Path] = []
    pid = (plugin_id or "").strip()
    if not pid:
        return [], "plugin_id is required — pass your plugin's id (registry.plugin_id)."
    try:
        from graph import sdk

        out.append(sdk.plugin_store(plugin_id=pid).resolve())
    except ValueError as e:
        return [], f"Bad plugin_id {pid!r}: {e}"
    except Exception as e:  # noqa: BLE001 — a host without plugin_store: extra roots only
        log.debug("[artifact] save_file: no plugin_store for %s (%s)", pid, e)
    if isinstance(roots, (str, os.PathLike)):
        roots = [roots]
    too_broad = {Path.home().resolve()}
    for r in roots or ():
        rp = Path(os.path.expanduser(str(r)))
        if not rp.is_absolute():
            return [], f"Allowed root {str(r)!r} must be an absolute path."
        rp = rp.resolve()
        if rp == Path(rp.anchor) or rp in too_broad:
            return [], f"Allowed root {str(r)!r} is too broad — name the directory your files live in."
        if not rp.is_dir():
            return [], f"Allowed root {str(r)!r} is not a directory."
        out.append(rp)
    return out, ""


def _contained_file(path: str, roots: list[Path]) -> tuple[Path | None, str]:
    """``path`` resolved (symlinks followed) to a regular file inside one of ``roots``, or
    ``(None, why)``. A relative path is taken against the FIRST root (the plugin store)."""
    raw = (path or "").strip()
    if not raw:
        return None, "path is required."
    if not roots:
        return None, "No allowed directory to read from (no plugin store and no roots given)."
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = roots[0] / p
    p = p.resolve()
    if not any(p.is_relative_to(r) for r in roots):
        return None, (
            f"{raw!r} is outside this plugin's store and the allowed roots — artifact.save_file "
            f"only registers files your plugin wrote under them."
        )
    if not p.is_file():
        return None, f"No file at {raw!r}. Write the file first, then pass its path."
    return p, ""


def save_file_service(
    path: str,
    title: str = "",
    artifact_id: str = "",
    session_id: str = "",
    *,
    plugin_id: str,
    roots: Any = (),
) -> dict:
    """The ``artifact.save_file`` plugin service (#4253): register a FILE artifact from plugin
    code — e.g. a background job's finished video — without an agent turn.

    It is ``save_file_artifact`` for code that isn't a tool call: the same blob cap
    (``max_blob_kb``), mime guess, preview / thumbnail, per-type preflights (.pptx/.pdf/.docx)
    and version chain. The file shows up in the Artifact panel like one the agent saved::

        save = sdk.service("artifact.save_file")   # None when the artifact plugin is off
        if save is not None:
            r = save(path=str(mp4), title="Overview walkthrough", session_id=job_session,
                     plugin_id=registry.plugin_id)
            if not r["ok"]:
                log.warning("artifact refused: %s", r["message"])

    ``path`` must resolve (symlinks followed) to a regular file INSIDE the calling plugin's
    ``sdk.plugin_store(plugin_id=plugin_id)`` directory or one of ``roots`` — extra absolute
    directories the caller names explicitly (e.g. an operator-configured data dir); a relative
    ``path`` is taken against the plugin store. A filesystem root or the home directory itself
    is refused as a root. ``plugin_id`` is REQUIRED (keyword-only): the SDK has no ambient
    plugin identity, so pass ``registry.plugin_id``.

    ``artifact_id`` revises an existing FILE artifact as its next version (a new one when
    omitted). ``session_id`` is the chat the file belongs to (the one that asked for the work):
    eviction keeps the artifact while that chat exists (#4138); omitted, it is evictable
    oldest-first like any other.

    Returns a dict and never raises for a refusal:

    - ``ok`` — False when nothing was saved (``message`` says why — e.g. the file is over the
      blob cap, which names the ``max_blob_kb`` setting that raises it);
    - ``id`` / ``version`` — the artifact and the version written (``""`` / ``0`` on a refusal);
    - ``message`` — a human/model-facing summary line;
    - ``ref`` — the ``artifact-ref`` chat-chip tail ("" when the host can't lift it). Append it
      LAST to a tool reply when one is going out; a background job with no turn can ignore it.

    SYNCHRONOUS: it reads the file, extracts its preview, and takes the store's cross-process
    file lock. Call it from a worker thread or via ``asyncio.to_thread`` — never on an event loop."""

    def refused(msg: str) -> dict:
        return {"ok": False, "id": "", "version": 0, "message": msg, "ref": ""}

    try:
        allowed, why = _service_roots(plugin_id, roots)
        if why:
            return refused(why)
        p, why = _contained_file(path, allowed)
        if p is None:
            return refused(why)
        prepared = _prepare_file(p)
        if isinstance(prepared, str):
            return refused(prepared)
        msg, saved = _save_file(**prepared.fields(), title=title, artifact_id=artifact_id, session_id=session_id)
    except _store.StoreLockTimeout as e:
        return refused(str(e))
    except Exception as e:  # noqa: BLE001 — a service reports, it doesn't raise into a job
        log.warning("[artifact] save_file service failed for %r", path, exc_info=True)
        return refused(f"Couldn't save {path!r}: {e}")
    if saved is None:
        return refused(msg)
    return {"ok": True, "id": saved[0], "version": saved[1], "message": msg, "ref": saved[2]}


# What a locked create/edit hands _then_render: (art_id, reported version, version key). The key
# (_store._version_key) pins the verdict to the version THIS call wrote — its reported number is a
# position, and a later commit at the version cap shifts it onto the next edit.
_RenderTarget = tuple[str, int, tuple[int, int]]
# A locked create/edit's result: ``(msg, None)`` for a refusal, ``(msg, target, ref_tail)`` for
# a write — the tail being the ``artifact-ref`` chip for the version it wrote (#3617).
_LockedResult = tuple[str, None] | tuple[str, _RenderTarget, str]


def _then_render(result: _LockedResult, renders_ui: bool = True) -> str:
    """A locked create/edit's reply plus the inline render verdict for the version it wrote,
    then the ``artifact-ref`` chip pointing at that version (#3617).

    ``result`` is ``(msg, None)`` for a refusal, or ``(msg, target, ref_tail)`` for a write —
    the tail is built INSIDE the lock (from the art dict the call just committed), so it names
    exactly the version this call wrote even if another edit lands right after.

    Called AFTER the store lock is released, on purpose: the verdict is written by the panel's
    /render-status route, which takes that same lock — so waiting for it while still holding
    the lock could only ever time out (and would stall every other writer, in any process,
    for the whole wait). The chip tail goes LAST: the server lifts everything from its
    sentinel on into the component frame and keeps the text before it as the tool card."""
    msg, target, *rest = result
    if target is None:
        return msg
    if not renders_ui:
        # A text-only client (#4144): no viewer will ever post a verdict for it, and no chip can
        # reach it — so neither the render wait nor the component tail, just the honest note.
        return msg + _TEXT_ONLY_NOTE
    return msg + _render_status._render_suffix(*target) + (rest[0] if rest else "")


@tool
@_busy_reply
def show_artifact(
    kind: str,
    code: str,
    title: str = "",
    links: dict | str | None = None,
    placement: str | None = None,
    state: Annotated[Any, InjectedState] = None,
) -> str:
    """CREATE a new generative-UI artifact in the console's Artifact panel.

    ``kind`` is one of: "html" (a full or partial HTML document), "svg" (inline SVG markup),
    "mermaid" (a Mermaid diagram definition), "markdown" (a Markdown document — rendered with
    design-system prose styling; ```mermaid fences become live diagrams), "vega-lite" (a CHART: a
    JSON Vega-Lite spec with its rows inline in ``data.values`` — themed to the console
    automatically, so leave colours/background out, and omit ``$schema`` (the renderer is always
    Vega-Lite v6); ``data.url`` is never loaded — EVERY standard
    bar/line/area/point/arc/heat-map chart is one, even one "added" to an existing tool: show it as
    its own inline vega-lite artifact beside the tool, never bars/lines hand-built from divs, CSS
    or SVG), or "react" (a
    self-contained React component script; name your top-level component ``App`` and it
    AUTO-MOUNTS into ``#root`` (no manual ``createRoot(...).render`` needed — though an explicit
    render still works). React, ReactDOM and Babel are provided, and it can ``import`` from a
    curated offline set — ``three`` (3D: ``import * as THREE from "three"``), ``d3``
    (``import * as d3 from "d3"``), ``animejs`` (animation: ``import { animate } from "animejs"``,
    then ``animate(el, { translateX: 100, duration: 400 })``), ``chart.js``, ``lucide``, and
    ``@pl/ui`` design-system components like ``Button``/``Card``/``Stat``/``Icon``). An ``html``
    artifact can import ``three`` / ``d3`` / ``animejs`` / ``chart.js`` / ``lucide`` the same way
    from a ``<script type="module">``; its own import map, if it ships one, is merged with these
    (its entries win). ``code`` is the source; ``title`` is
    an optional label. Runs sandboxed — it can't access the console.

    After creating it, CHECK IT RENDERED: this reply carries the render verdict when the panel is
    open; otherwise — always for an INLINE artifact, whose chat frame mounts only after this reply —
    call ``check_artifact(<id>)`` before telling the user it works. Fix any reported error.

    A STANDALONE DOCUMENT the user will keep or edit (a README, spec, report, letter) is
    ``kind="markdown"`` here — it opens in the panel — with a short summary in chat, not the whole
    document pasted into the reply. Short answers stay text. A DIAGRAM is ``kind="mermaid"``
    (inline by default): a ```mermaid fence in chat text shows as plain code, not a drawing.

    To EDIT what you just made, use ``update_artifact`` (a small targeted change) or
    ``rewrite_artifact`` (a full replacement) — they iterate the SAME artifact as a new
    version instead of cluttering the panel with near-duplicates.

    Use this for free-form or custom-rendered visuals — a chart, a Mermaid diagram, bespoke
    HTML/React/SVG (it runs sandboxed, heavier). For plain STRUCTURED DATA — a table, a
    metrics block, a step/plan list — prefer ``show_component`` instead (it renders inline in
    the chat, data-only, no sandbox, lighter). Rule of thumb: a generated VISUAL → this tool;
    a data SHAPE → a component. Prefer either over writing files when the user just wants to
    SEE something rendered. Returns the artifact id.

    ``placement`` DEFAULTS to ``"inline"``; pass ``"panel"`` to override it. Inline renders the
    SAME artifact directly in the conversation — the same sandbox, versions and "Open in panel" —
    for an answer the user interacts with in place (a calculator, an explainer, a chart, a small
    tool). Inline placement covers html, svg, mermaid, react and vega-lite; any other kind
    (markdown, decks, PDFs, files) renders in the panel instead — SILENTLY under the default, with
    a note only when you EXPLICITLY pass ``placement="inline"`` on such a kind. Pass
    ``placement="panel"`` for a work product you iterate on over many turns (a document, a deck, a
    large app) — that still opens the panel.

    CODE-LINKED DIAGRAMS (mermaid only): ``links`` maps diagram elements to code, so the
    operator can click a node or a message and land on that code in the console's code pane.
    ``{"<key>": {"project", "path", "line", "end_line"?, "note"?}}`` — keys are a flowchart /
    class / state node id or subgraph id exactly as written (bare — ``participant:`` is for
    sequence diagrams only), ``participant:<name>`` for a sequence participant, and ``msg:<n>`` for the n-th sequence message (1-based, source
    order) or ``msg:<exact label>``. Every target must be a REAL location you read — take
    ``line`` from ``search_files`` (file:line) or a ``read_file`` offset, never a guess — and
    ALWAYS pass ``anchor``: a short exact snippet of that line copied verbatim from the tool
    output (``"runTool("``, ``"function textFrom"``, ≤ 120 chars). The server snaps the link to
    the anchor's nearest occurrence (so a miscounted line still lands) and drops a link whose
    anchor isn't in the file. The reply echoes each target's first line and every snap. Bad
    links are dropped with a reason (the diagram still renders). ``note`` is one sentence
    (≤ 280 chars).

    Style ``html``/``react`` with the injected design system, which is required: colours ONLY from
    the theme tokens (``var(--pl-color-fg)``, ``var(--pl-color-bg-raised)``, ``var(--pl-color-accent)`` …) (never ``#fff``/``white``/hex/rgb; the operator's theme may be dark
    or light), and the ``.pl-*`` classes (``pl-card``, ``pl-field`` + ``pl-input``, ``pl-btn
    pl-btn--primary``, ``pl-stats``, ``pl-table``). See the ``rendering-artifacts`` skill.
    """
    checked = _links.check(links)
    ui = _renders_ui(state)
    return _then_render(
        _show(kind, code, title, checked, placement, session_id=_chat_session(state), renders_ui=ui), ui
    )


def show_service(
    kind: str,
    code: str,
    title: str = "",
    placement: str | None = None,
    session_id: str = "",
    renders_ui: bool = True,
) -> dict:
    """The ``artifact.show`` plugin service (ADR 0116): create an artifact for ANOTHER plugin.

    How a plugin puts something in the Artifact panel without importing this one — e.g. the data
    plugin's ``data_chart`` runs a query and hands the rows, inlined into a Vega-Lite spec, here::

        show = sdk.service("artifact.show")   # None when the artifact plugin is off
        r = show(kind="vega-lite", code=spec_json, title="Best weekdays")

    It is exactly ``show_artifact`` minus the code ``links``: the same kinds, size cap, per-kind
    checks, version chain and render-verdict wait. ``placement`` DEFAULTS to ``"inline"`` (pass
    ``"panel"`` to override) — inline renders the artifact in the conversation (html/svg/mermaid/
    react/vega-lite); any other kind renders in the panel, SILENTLY under the default and noted in
    ``message`` only when ``placement="inline"`` is explicit. ``session_id`` is the chat the
    caller's reply goes out in (``tools.session._session_id_from`` of the caller's injected
    state): pass it when you append ``ref``, so eviction keeps the artifact while that chat
    exists (#4138); omitted, the artifact is evictable oldest-first. ``renders_ui`` is whether
    the caller's turn reaches a client that can display the artifact (#4144) — pass
    ``graph.sdk.renders_ui(state)`` from the caller's injected state. ``False`` (a plain API
    client: ``/v1``, ``/api/chat``, a relay plugin's turn) returns the honest text-only reply —
    saved, with its id, but this client can't display it — with no render-verdict wait and an
    empty ``ref``. Defaults to ``True``: the console reply, as before. Returns a dict —
    never raises for a refusal:

    - ``ok`` — False when nothing was created (``message`` says why);
    - ``id`` / ``version`` — the new artifact and its version (``""`` / ``0`` on a refusal);
    - ``message`` — the model-facing reply, render verdict included when a panel is open;
    - ``ref`` — the ``artifact-ref`` chat-chip tail ("" when the host can't lift it, or the
      client renders text only). Append it
      LAST, verbatim, to your tool's return string: the server lifts it into a chip that opens
      the panel on this version.

    It is SYNCHRONOUS and can BLOCK: when a panel is open it waits (``time.sleep`` polling, up to
    ~3.2 s) for the frame's render verdict, and it takes the store's cross-process file lock. Call
    it from a sync tool body, or from async code via ``asyncio.to_thread`` — never directly on an
    event loop."""
    try:
        result = _show(kind, code, title, placement=placement, session_id=session_id, renders_ui=renders_ui)
    except _store.StoreLockTimeout as e:
        return {"ok": False, "id": "", "version": 0, "message": str(e), "ref": ""}
    msg, target, *rest = result
    if target is None:
        return {"ok": False, "id": "", "version": 0, "message": msg, "ref": ""}
    if not renders_ui:
        # Same as show_artifact on a text-only client (#4144): no viewer, so no wait and no chip.
        return {"ok": True, "id": target[0], "version": target[1], "message": msg + _TEXT_ONLY_NOTE, "ref": ""}
    return {
        "ok": True,
        "id": target[0],
        "version": target[1],
        "message": msg + _render_status._render_suffix(*target),
        "ref": rest[0] if rest else "",
    }


@_store.serialized
def _show(
    kind: str,
    code: str,
    title: str,
    checked: _links.Checked | None = None,
    placement: str | None = None,
    session_id: str = "",
    renders_ui: bool = True,
) -> _LockedResult:
    k = (kind or "").strip().lower()
    if k not in _KINDS:
        return f"Unknown artifact kind {kind!r}. Use one of: {', '.join(sorted(_KINDS))}.", None
    code = code or ""
    if err := _store._too_big(code):
        return err, None
    if err := _kind_problem(k, code):
        return err, None
    inline, placement_note = _resolve_placement(placement, k)
    kept, report = _links.finish(checked, k, code) if checked else ({}, "")
    store = _store._read_store()
    nv = _store._new_version(code, extra={"links": kept} if kept else None)
    art = {
        "id": _store._new_id(),
        "title": title or "",
        "kind": k,
        "versions": [nv],
        "version_count": 1,
        "created": nv["ts"],
        "updated": nv["ts"],
    }
    if inline:
        # Persist placement on the ARTIFACT, not just this one chip: update_artifact and
        # rewrite_artifact read it back (``_store._is_inline``) so the next version's chip stays
        # inline too (ADR 0118 D2). ABSENT on a panel artifact, so the default store is unchanged.
        art["placement"] = "inline"
    _stamp_ref(art, session_id)
    store["artifacts"].insert(0, art)
    store["current"] = art["id"]
    _store._write_store(store)
    _store._emit("created", {"id": art["id"], "kind": k, "title": title or ""})
    # Say where it ACTUALLY shows: an inline artifact renders in the chat, not the panel (#4124) —
    # a "now showing in the Artifact panel" reply sends the model telling the user to open it.
    where = "now showing inline in the chat" if inline else "now showing in the Artifact panel"
    if not renders_ui:
        # A text-only client (#4144) has no chat view and no panel: say it was SAVED, with its id
        # and what it is, and no placement note (inline vs panel means nothing to this client).
        label = f' "{title}"' if title else ""
        return (
            f"Saved {k} artifact {art['id']}{label} ({len(code)} chars) — this client can't display it."
            + report,
            (art["id"], 1, _store._version_key(art)),
            "",
        )
    msg = (
        f"Created {k} artifact {art['id']} ({len(code)} chars) — {where}. "
        f"Edit it with update_artifact(old_string, new_string) or rewrite_artifact(code)."
    ) + placement_note + report
    return msg, (art["id"], 1, _store._version_key(art)), _ref.ref_tail(art, inline=inline)


def _carry_links(art: dict, checked: _links.Checked | None, new_code: str) -> tuple[dict, str]:
    """The links for a targeted edit's new version: the passed map when there is one, else the
    previous version's links carried over (a small edit keeps the same diagram) — re-checked
    against the new source so a renamed node or a removed message is reported, not silent."""
    if checked is not None and checked.given and not checked.error:
        kept, report = _links.finish(checked, art["kind"], new_code)
        # A passed map REPLACES the stored one — it never merges. Say which previously linked
        # keys that leaves unlinked, so "add one link" doesn't silently strip the rest.
        lost = [k for k in (art["versions"][-1].get("links") or {}) if k not in kept]
        if lost and kept:
            report += (
                f"\nThe `links` you passed REPLACED the stored map (it doesn't merge): {len(lost)} "
                f"previously linked element(s) are now unlinked ({', '.join(lost[:12])}"
                f"{'…' if len(lost) > 12 else ''}). To keep them, pass the complete map."
            )
        return kept, report
    # Not passed — or passed but unreadable (malformed JSON): a bad argument never deletes the
    # links already stored; its error is reported and the previous links carry over.
    err = ("\n" + checked.error) if checked is not None and checked.error else ""
    old = art["versions"][-1].get("links") or {}
    if not old or art["kind"] != "mermaid":
        return {}, err
    report = err + f"\nCarried over {len(old)} code link(s) from the previous version."
    stale = _links.unmatched_keys(old, new_code)
    if stale:
        report += (
            " These no longer match anything in the diagram: "
            + ", ".join(stale[:20])
            + " — pass `links` with the corrected map."
        )
    return dict(old), report


@tool
@_busy_reply
def update_artifact(
    old_string: str,
    new_string: str,
    artifact_id: str = "",
    links: dict | str | None = None,
    state: Annotated[Any, InjectedState] = None,
) -> str:
    """Make a TARGETED edit to an existing artifact: replace ``old_string`` with ``new_string``
    in its current source, creating a new version. ``old_string`` must match the current source
    EXACTLY ONCE (whitespace included) — add surrounding context to disambiguate if needed.
    Defaults to the most-recent artifact; pass ``artifact_id`` to target another (see
    ``list_artifacts``). Prefer this over ``rewrite_artifact`` for small changes — it's the fast
    path and keeps the version history clean.

    Asked to ADD A CHART to an existing tool? Don't edit bars/lines into it — a standard chart is
    its own inline ``vega-lite`` artifact (``show_artifact(kind="vega-lite", …)``) beside the tool.

    A mermaid diagram's code ``links`` (see ``show_artifact``) CARRY OVER to the new version
    unless you pass ``links`` — the full new map, replacing the old one (``{}`` clears them).
    If your edit renumbers sequence messages (msg:<n>), pass the corrected map.
    """
    checked = _links.check(links)
    return _then_render(
        _update(old_string, new_string, artifact_id, checked, session_id=_chat_session(state)), _renders_ui(state)
    )


@_store.serialized
def _update(
    old_string: str,
    new_string: str,
    artifact_id: str,
    checked: _links.Checked | None = None,
    session_id: str = "",
) -> _LockedResult:
    if not old_string:
        return "old_string must not be empty.", None
    store = _store._read_store()
    art = _store._find(store, artifact_id or store["current"])
    if art is None:
        return "No artifact to update. Create one with show_artifact first.", None
    if _store._is_file(art):
        return _store._file_not_editable(art), None
    src = art["versions"][-1]["code"]
    n = src.count(old_string)
    if n == 0:
        return (
            "old_string not found in the current source — it must match exactly (whitespace "
            "included). Read the current source with get_artifact, then craft an exact old_string."
        ), None
    if n > 1:
        return (
            f"old_string matches {n} times — it must match exactly once. Add surrounding context to make it unique."
        ), None
    new_code = src.replace(old_string, new_string, 1)
    if err := _store._too_big(new_code):
        return err, None
    if err := _kind_problem(art["kind"], new_code):
        return err, None
    kept, report = _carry_links(art, checked, new_code)
    _stamp_ref(art, session_id)
    v = _store._commit_version(store, art, new_code, extra={"links": kept} if kept else None)
    return (
        f"Updated artifact {art['id']} → version {v}." + report,
        (art["id"], v, _store._version_key(art)),
        # An inline artifact keeps its inline chip across edits (ADR 0118 D2) — placement is read
        # from the artifact, not re-specified per edit, so an update never silently panels it.
        _ref.ref_tail(art, inline=_store._is_inline(art)),
    )


@tool
@_busy_reply
def rewrite_artifact(
    code: str,
    title: str = "",
    artifact_id: str = "",
    links: dict | str | None = None,
    state: Annotated[Any, InjectedState] = None,
) -> str:
    """Replace an artifact's ENTIRE source with ``code``, creating a new version (the kind is
    kept). Use this for a large change where a targeted ``update_artifact`` would be awkward;
    prefer ``update_artifact`` for small edits — a rewrite round-trips the full body through
    the conversation every time, so batch your changes into one rewrite rather than iterating
    rewrite-by-rewrite. Optionally update the ``title``. Defaults to the most-recent artifact;
    pass ``artifact_id`` to target another.

    A mermaid diagram's code ``links`` (see ``show_artifact``) do NOT carry over a rewrite —
    the new source is a new diagram, so pass the ``links`` for it (the old version keeps its own).
    """
    checked = _links.check(links)
    return _then_render(
        _rewrite(code, title, artifact_id, checked, session_id=_chat_session(state)), _renders_ui(state)
    )


@_store.serialized
def _rewrite(
    code: str, title: str, artifact_id: str, checked: _links.Checked | None = None, session_id: str = ""
) -> _LockedResult:
    code = code or ""
    if err := _store._too_big(code):
        return err, None
    store = _store._read_store()
    art = _store._find(store, artifact_id or store["current"])
    if art is None:
        return "No artifact to rewrite. Create one with show_artifact first.", None
    if _store._is_file(art):
        return _store._file_not_editable(art), None
    if err := _kind_problem(art["kind"], code):
        return err, None
    if title:
        art["title"] = title
    kept, report = _links.finish(checked, art["kind"], code) if checked else ({}, "")
    if not checked or not checked.given:
        old = art["versions"][-1].get("links") or {}
        if old:
            report += (
                f"\nThe previous version's {len(old)} code link(s) were not carried over (a rewrite is a "
                "new diagram) — pass `links` to link this one."
            )
    _stamp_ref(art, session_id)
    v = _store._commit_version(store, art, code, extra={"links": kept} if kept else None)
    return (
        f"Rewrote artifact {art['id']} → version {v}." + report + _save_nudge(art["id"]),
        (art["id"], v, _store._version_key(art)),
        # Same as update: a rewrite of an inline artifact stays inline (ADR 0118 D2).
        _ref.ref_tail(art, inline=_store._is_inline(art)),
    )


# The write tools declare the string argument the server streams as a live inline preview
# (ADR 0118 D3: tool metadata ``{"stream_args": "code"}`` → `tool_args` frames). Harmless on a
# host without the streaming seam — it's read, never required.
show_artifact.metadata = {"stream_args": "code"}
update_artifact.metadata = {"stream_args": "code"}
rewrite_artifact.metadata = {"stream_args": "code"}


def _pin_mark(art: dict) -> str:
    return "  · pinned" if _store._is_pinned(art) else ""


@tool
def list_artifacts() -> str:
    """List the artifacts in the panel (pinned first, then newest first) with id, kind, title,
    version count and whether each is pinned, so you can target ``update_artifact`` /
    ``rewrite_artifact`` / ``pin_artifact`` / ``delete_artifact`` at a specific one. Read-only."""
    store = _store._read_store()
    if not store["artifacts"]:
        return "No artifacts yet. Create one with show_artifact."
    lines = []
    for a in store["artifacts"]:
        cur = "  · current" if a["id"] == store["current"] else ""
        lines.append(
            f"{a['id']}  [{a['kind']}]  {a['title'] or '(untitled)'}  · v{len(a['versions'])}{_pin_mark(a)}{cur}"
        )
    cap = _config._max_history()
    unpinned = len(store["artifacts"]) - len(_store._pinned(store))
    retention = (
        f"Retention: up to {cap} unpinned artifacts are kept; past that the oldest one no surviving "
        f"chat still shows is evicted (an artifact shown in a chat stays until that chat is deleted); "
        f"pinned artifacts ({len(_store._pinned(store))}/{_config._max_pinned()} pins used) are never evicted."
    )
    if unpinned > cap:
        # The all-referenced case (#4138): the store keeps live answers over its cap rather than
        # delete one — say so where the agent (and the operator through it) looks.
        retention += f" The store is {unpinned - cap} over that cap: every remaining artifact is still shown in a chat."
    return "Artifacts (pinned first, then newest first):\n" + "\n".join(lines) + "\n\n" + retention


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
    links = _links.describe(art["versions"][-1].get("links"))
    return f"Artifact {art['id']}  [{art['kind']}]  {title}  · v{v}{_pin_mark(art)} — current source:\n\n{code}{links}"


@tool
def check_artifact(artifact_id: str = "", state: Annotated[Any, InjectedState] = None) -> str:
    """Check whether an artifact's latest version actually RENDERED — the feedback channel for
    the code→render→fix loop. Rendering happens async in the browser, so a create/edit can
    return before the result is known; call this (or just iterate) to see how it went.

    Returns the render verdict: rendered cleanly, FAILED with the captured error message, or
    "no result yet" — for a panel artifact, the panel is closed / not showing this version (open
    the Artifact panel); for an INLINE artifact, the operator's chat isn't showing it yet, so the
    render can't be confirmed — don't keep re-checking, and never open the panel for it. On a
    client that renders text only, there is no viewer and nothing to check.
    Defaults to the current artifact; pass ``artifact_id`` (see ``list_artifacts``) to target
    another. Read-only.

    Safe to call right after creating/editing: if a result isn't in yet, it waits briefly for the
    verdict rather than returning "no result yet" — for an INLINE artifact always (its chat frame
    mounts after the create reply), for a panel artifact while the panel is live."""
    store = _store._read_store()
    art = _store._find(store, artifact_id or store["current"])
    if art is None:
        return "No artifact to check. Use list_artifacts to see the ids, or show_artifact to create one."
    v = len(art["versions"])
    # An already-recorded verdict reads instantly; otherwise wait briefly IFF a renderer is live
    # (so an immediate post-render check returns the real result, not a premature "no result yet").
    # By identity, not position: an edit committed during the wait must not lend v its verdict.
    key = _store._version_key(art)
    r = _render_status._version_render(art, v, key)
    if r is None and not _renders_ui(state):
        # A text-only client (#4144): no viewer will ever post a verdict, so waiting or
        # re-checking can only burn turns — answer at once, and say so.
        return (
            f"Artifact {art['id']} v{v}: no viewer on this client (it renders text only), so the "
            "render can't be checked. Don't re-check — answer in text."
        )
    # An INLINE artifact waits the full window whatever the panel is doing (#4076): this client
    # renders UI (the text-only case returned above), so its chat frame WILL mount during the turn
    # — the hidden-tab eager mount (S22) covers a backgrounded tab — but after a reload nothing has
    # polled yet, and the panel-poll gate alone answered "no result yet" before the frame loaded.
    r = r or _render_status._await_render(art["id"], v, key, inline_viewer=_store._is_inline(art))
    if r is None:
        if _store._is_inline(art):
            # An inline artifact renders in the chat's own frame (ADR 0118 D2), mounted lazily as
            # it scrolls into view — the panel has nothing to do with it, so never point there
            # (#4124). And no "check again": a frame that isn't on screen posts nothing, so a
            # re-check loop only burns context (#4144/#4147).
            return (
                f"Artifact {art['id']} v{v}: no render result yet — it's inline in the chat, but the "
                "operator's chat isn't showing it yet, so the render can't be confirmed. Don't keep "
                "re-checking; carry on, and fix it if the operator reports a problem."
            )
        return (
            f"Artifact {art['id']} v{v}: no render result yet — the Artifact panel may be "
            "closed or not showing this version. Open it to render."
        )
    if r.get("ok"):
        return f"Artifact {art['id']} v{v}: rendered cleanly."
    err = str(r.get("error") or "render failed").strip()
    return f"Artifact {art['id']} v{v}: render FAILED —\n  {err}\nFix it with update_artifact / rewrite_artifact."


@tool
@_busy_reply
@_store.serialized
def pin_artifact(artifact_id: str, pinned: bool = True) -> str:
    """PIN an artifact so it is never evicted — for a LONG-LIVED artifact you'll come back to
    across sessions (a master resume, a reference doc, a running plan), especially one whose
    id you've recorded somewhere else. Without a pin, artifacts are evicted oldest-first once
    the panel holds more than its limit (default 20) — counting EVERY artifact on this agent,
    so a long-lived one silently disappears after enough unrelated renders and a recorded id
    then points at nothing.

    A pin keeps the artifact and its LATEST versions: edits still trim to the per-artifact
    version limit (default 50), so old revisions are not kept forever. The number of pins is
    capped; at the cap this REFUSES and names the pinned artifacts — unpin one first.
    ``pinned=False`` unpins: the artifact counts toward the limit again, as if just touched.
    ``list_artifacts`` / ``get_artifact`` show which artifacts are pinned. Pass the
    ``artifact_id`` (see ``list_artifacts``)."""
    store = _store._read_store()
    art = _store._find(store, artifact_id)
    if art is None:
        return f"No artifact {artifact_id!r}. Use list_artifacts to see the ids."
    label = f"{art['id']} ({art['title'] or 'untitled'})"
    if not pinned:
        if not _store._is_pinned(art):
            return f"Artifact {label} isn't pinned — nothing to do."
        art.pop("pinned", None)  # absent, not false: the pre-pin store shape
        # Unpinning re-enters the recency order. Left where it sat, a long-pinned artifact behind
        # `history` newer ones would be evicted by THIS write — so it takes the most-recent slot
        # (a full window before eviction), without becoming the panel's current artifact.
        store["artifacts"] = [art] + [a for a in store["artifacts"] if a["id"] != art["id"]]
        _store._write_store(store)
        return (
            f"Unpinned artifact {label}. It now counts toward the {_config._max_history()}-artifact "
            f"limit again and will be evicted once that many newer artifacts are touched."
        )
    if _store._is_pinned(art):
        return f"Artifact {label} is already pinned."
    cap = _config._max_pinned()
    held = _store._pinned(store)
    if cap == 0:
        return (
            f"Can't pin {label}: new pins are off on this agent (the artifact max_pinned setting "
            f"is 0). Existing pins stay protected until unpinned."
        )
    if len(held) >= cap:
        names = ", ".join(f"{a['id']} ({a['title'] or 'untitled'})" for a in held[:_PIN_NAMES_SHOWN])
        if len(held) > _PIN_NAMES_SHOWN:
            names += f", …and {len(held) - _PIN_NAMES_SHOWN} more (see list_artifacts)"
        return (
            f"Can't pin {label}: {len(held)}/{cap} artifacts are already pinned — {names}. "
            f"Unpin one with pin_artifact(artifact_id, pinned=False) first, or raise the "
            f"artifact max_pinned setting."
        )
    art["pinned"] = True
    # A pin counts as a touch for ORDER (not for the panel's current artifact): the pinned group
    # is stored first and most-recently-touched first, so the new pin leads it — the same rule the
    # unpin path follows for the unpinned group.
    store["artifacts"] = [art] + [a for a in store["artifacts"] if a["id"] != art["id"]]
    _store._write_store(store)
    return (
        f"Pinned artifact {label} ({len(held) + 1}/{cap} pins used). It won't be evicted; its "
        f"latest {_config._max_versions()} versions are kept."
    )


@tool
@_busy_reply
@_store.serialized
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
