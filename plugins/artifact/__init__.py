"""Artifact plugin (ADR 0038) — generative UI on demand.

The agent calls ``show_artifact(kind, code)`` to render HTML / SVG / Mermaid / Markdown / React into the
console's Artifact panel, then iterates it with ``update_artifact`` (a targeted string-replace
edit) or ``rewrite_artifact`` (a full replacement) — the Claude "update vs rewrite" model, so an
artifact is a VERSION CHAIN you can step back through, not a flood of near-duplicates.
``list_artifacts`` / ``get_artifact`` (read the current source — how you take over an artifact you
didn't author) / ``delete_artifact`` manage them. The panel is a plugin-served shell page
(iframed by the console, ADR 0026) that renders the generated code in a **nested sandboxed
iframe** (``sandbox="allow-scripts"``, no same-origin) — the Claude Artifacts / Open WebUI
isolation model: generated code runs, but can't touch the console, its cookies, or its APIs.

State is persisted to a **file** (instance-scoped), not module memory — under the ACP runtime the
tool executes in the operator-MCP process while the route is served by the main process, so the
two only share state through disk.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.artifact")

_KINDS = {"html", "svg", "mermaid", "react", "markdown"}

# Vendored assets served same-origin so artifacts render fully offline (no cdnjs).
# Allowlist (no path traversal) — must match the files in vendor/. Two groups:
#  • UMD libs loaded via <script> + SRI (react/react-dom/babel/mermaid), pinned in the
#    shell's LIB map.
#  • ESM modules resolved by the `react` import map — curated offline libs, tiny React
#    shims (re-export the UMD globals → one shared React instance), and the authored
#    @pl/ui DS wrappers; same-origin + install-pinned (plugins.lock sha), not SRI.
_VENDOR_FILES = {
    # UMD (SRI-pinned in the shell's LIB map)
    "mermaid.min.js",
    "react.production.min.js",
    "react-dom.production.min.js",
    "babel.min.js",
    # ESM modules (the `react` import map): curated libs …
    "d3.mjs",
    "chartjs.mjs",
    "lucide.mjs",
    "marked.mjs",
    # … React shims + authored design-system wrappers
    "react.shim.mjs",
    "react-dom-client.shim.mjs",
    "pl-ui.mjs",
}


# ── config ───────────────────────────────────────────────────────────────────
# Read live from the host's plugin config (the manifest `settings:` block, ADR 0019 —
# editable in Settings ▸ Plugins, persisted, no restart) with an env-var override and
# a literal default. Precedence: explicit ENV > UI/config > default. config() reads the
# LIVE config each call, so a Settings save takes effect immediately; under ACP (no
# graph state in the tool process) it falls back to env/default.
_TRUE = {"1", "true", "yes", "on"}


def _plugin_cfg() -> dict:
    try:
        from graph.sdk import config

        return (getattr(config(), "plugin_config", {}) or {}).get("artifact", {}) or {}
    except Exception:  # noqa: BLE001 — no host (tests) / not yet loaded → env+default
        return {}


def _cfg_bool(key: str, env: str) -> bool:
    e = os.environ.get(env)
    if e:
        return e.strip().lower() in _TRUE
    v = _plugin_cfg().get(key)
    if isinstance(v, bool):
        return v
    return v not in (None, "") and str(v).strip().lower() in _TRUE


def _cfg_str(key: str, env: str, default: str = "") -> str:
    e = os.environ.get(env)
    if e:
        return e
    v = _plugin_cfg().get(key)
    return str(v) if v not in (None, "") else default


def _cfg_int(key: str, env: str, default: int, minimum: int = 1) -> int:
    for raw in (os.environ.get(env, ""), _plugin_cfg().get(key)):
        if raw not in (None, ""):
            try:
                return max(minimum, int(raw))
            except (TypeError, ValueError):
                pass  # bad value → try the next source, never crash
    return default


# History/version/size caps — Settings ▸ Plugins number fields (env override).
# Read live via functions so a config change applies at once.
def _max_history() -> int:
    return _cfg_int("history", "ARTIFACT_HISTORY", 20)


def _max_versions() -> int:
    return _cfg_int("max_versions", "ARTIFACT_MAX_VERSIONS", 50)


def _max_code_bytes() -> int:
    return _cfg_int("max_code_kb", "ARTIFACT_MAX_CODE_KB", 512) * 1024


# Interactive artifacts (window.protoArtifact.ask → the agent). OPT-IN: letting
# sandboxed artifact code trigger LLM calls is a cost surface. `ask_enabled` +
# `ask_system` are Settings ▸ Plugins fields (manifest `settings:`); ask_max_chars caps.
def _ask_enabled() -> bool:
    return _cfg_bool("ask_enabled", "ARTIFACT_ASK_ENABLED")


def _ask_system() -> str | None:
    return _cfg_str("ask_system", "ARTIFACT_ASK_SYSTEM") or None


def _ask_max_chars() -> int:
    return _cfg_int("ask_max_chars", "ARTIFACT_ASK_MAX_CHARS", 4000)


# ── the store ──────────────────────────────────────────────────────────────────
# An artifact is a VERSION CHAIN: {id, kind, title, versions:[{code, ts, by}], …}.
# show_artifact creates one; update_artifact/rewrite_artifact append a version (the
# proven Claude "update vs rewrite" model — iterate the same artifact, don't spam the
# panel with near-duplicates). The file is {"artifacts": [newest-first], "current": id}.


def _store_path() -> Path:
    base = Path(
        os.environ.get("ARTIFACT_DIR") or (Path.home() / ".protoagent" / "artifact")
    )
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        base = base / inst
    base.mkdir(parents=True, exist_ok=True)
    return base / "history.json"


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
        arts = [
            a for a in data["artifacts"] if isinstance(a, dict) and a.get("versions")
        ]
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
    max_versions = _max_versions()
    store["artifacts"] = store.get("artifacts", [])[: _max_history()]
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


def _find(store: dict, art_id: str | None) -> dict | None:
    return next((a for a in store["artifacts"] if a["id"] == art_id), None)


def _too_big(code: str) -> str | None:
    limit = _max_code_bytes()
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


def _new_version(code: str, by: str = "agent") -> dict:
    """A fresh version record. ``by`` is provenance: "agent" (a tool) or "user" (panel edit)."""
    return {"code": code, "ts": _now(), "by": by}


def _commit_version(store: dict, art: dict, code: str, by: str = "agent") -> int:
    """Append a version to ``art``, move it to the front, persist, broadcast ``updated``, and
    return the new 1-based version count. The shared tail of update/rewrite_artifact + the
    panel's user-edit PUT — one place owns append→touch→write→emit ordering."""
    nv = _new_version(code, by)
    art["versions"].append(nv)
    art["updated"] = nv["ts"]
    _touch(store, art)
    _write_store(store)  # may trim to _max_versions(), so count AFTER
    v = len(art["versions"])
    _emit("updated", {"id": art["id"], "version": v})
    return v


# ── Render feedback (#1458) ──────────────────────────────────────────────────
# The render happens ASYNC in the browser sandbox, AFTER the tool returns. The shell
# relays the sandbox's render result (ok / error) to POST /render-status, which stamps it
# onto the version. So show_/update_/rewrite_artifact can wait BRIEFLY for that result and
# report a render failure inline, closing the agent's code→render→fix loop. The wait only
# kicks in when a renderer is actually live (the panel polled recently) — headless / closed
# panel returns instantly, and the agent can still pull status later via check_artifact.
_LAST_POLL_TS = 0  # ms of the last panel poll (/history or /current); 0 = never seen a renderer
_RENDER_ERR_MAX = 2000  # cap a render-error string so a noisy stack can't bloat the store
_RENDER_ACTIVE_MS = 4000  # a poll within this window ⇒ a renderer is live and will report back
_RENDER_WAIT_MS = 3200  # max wait for a render result (≥ the sandbox's 3s no-mount guard)
_RENDER_POLL_MS = 120  # how often the wait re-reads the store


def _note_poll() -> None:
    global _LAST_POLL_TS
    _LAST_POLL_TS = _now()


def _renderer_live() -> bool:
    """True when the panel polled recently — i.e. a sandbox is mounted and WILL render the
    new version and report its result. Gates the inline wait so headless never blocks."""
    return _LAST_POLL_TS > 0 and (_now() - _LAST_POLL_TS) <= _RENDER_ACTIVE_MS


def _version_render(art: dict, version: int) -> dict | None:
    """The stored render result for 1-based ``version`` of ``art`` (or None)."""
    vers = art.get("versions") or []
    if 1 <= version <= len(vers):
        r = vers[version - 1].get("render")
        return r if isinstance(r, dict) else None
    return None


def _await_render(art_id: str, version: int) -> dict | None:
    """Block up to ``_RENDER_WAIT_MS`` for the sandbox to report version ``version``'s render
    result — but ONLY when a renderer is live, else return immediately. Checks the store
    BEFORE each sleep, so an already-recorded result returns instantly. Runs in the tool's
    worker thread, so the short sleep is safe (it doesn't block the event loop)."""
    if not art_id or not _renderer_live():
        return None
    deadline = _now() + _RENDER_WAIT_MS
    while True:
        r = _version_render(_find(_read_store(), art_id), version)
        if r is not None:
            return r
        if _now() >= deadline:
            return None
        time.sleep(_RENDER_POLL_MS / 1000)


def _render_suffix(art_id: str, version: int) -> str:
    """The inline render verdict appended to a create/edit reply, or '' when unknown."""
    r = _await_render(art_id, version)
    if r is None:
        return ""
    if r.get("ok"):
        return " It rendered cleanly."
    err = str(r.get("error") or "render failed").strip()
    return (
        f"\n\n⚠ But it FAILED to render:\n  {err}\n"
        "Fix it with update_artifact / rewrite_artifact (the artifact still exists; "
        "this error is advisory)."
    )


@tool
def show_artifact(kind: str, code: str, title: str = "") -> str:
    """CREATE a new generative-UI artifact in the console's Artifact panel.

    ``kind`` is one of: "html" (a full or partial HTML document), "svg" (inline SVG markup),
    "mermaid" (a Mermaid diagram definition), "markdown" (a Markdown document — rendered with
    design-system prose styling; ```mermaid fences become live diagrams), or "react" (a
    self-contained React component script that renders into ``#root``; React, ReactDOM and Babel
    are provided, and it can ``import`` from a curated offline set — ``d3``, ``chart.js``,
    ``lucide``, and ``@pl/ui`` design-system components like ``Button``/``Card``/``Stat``/
    ``Icon``). ``code`` is the source; ``title`` is an optional label. Runs sandboxed — it can't
    access the console.

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
        return (
            f"Unknown artifact kind {kind!r}. Use one of: {', '.join(sorted(_KINDS))}."
        )
    code = code or ""
    if err := _too_big(code):
        return err
    store = _read_store()
    nv = _new_version(code)
    art = {
        "id": _new_id(),
        "title": title or "",
        "kind": k,
        "versions": [nv],
        "created": nv["ts"],
        "updated": nv["ts"],
    }
    store["artifacts"].insert(0, art)
    store["current"] = art["id"]
    _write_store(store)
    _emit("created", {"id": art["id"], "kind": k, "title": title or ""})
    msg = (
        f"Created {k} artifact {art['id']} ({len(code)} chars) — now showing in the Artifact "
        f"panel. Edit it with update_artifact(old_string, new_string) or rewrite_artifact(code)."
    )
    return msg + _render_suffix(art["id"], 1)


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
    store = _read_store()
    art = _find(store, artifact_id or store["current"])
    if art is None:
        return "No artifact to update. Create one with show_artifact first."
    src = art["versions"][-1]["code"]
    n = src.count(old_string)
    if n == 0:
        return (
            "old_string not found in the current source — it must match exactly (whitespace "
            "included). Read the current source with get_artifact, then craft an exact old_string."
        )
    if n > 1:
        return (
            f"old_string matches {n} times — it must match exactly once. Add surrounding "
            f"context to make it unique."
        )
    new_code = src.replace(old_string, new_string, 1)
    if err := _too_big(new_code):
        return err
    v = _commit_version(store, art, new_code)
    return f"Updated artifact {art['id']} → version {v}." + _render_suffix(art["id"], v)


@tool
def rewrite_artifact(code: str, title: str = "", artifact_id: str = "") -> str:
    """Replace an artifact's ENTIRE source with ``code``, creating a new version (the kind is
    kept). Use this for a large change where a targeted ``update_artifact`` would be awkward;
    prefer ``update_artifact`` for small edits. Optionally update the ``title``. Defaults to the
    most-recent artifact; pass ``artifact_id`` to target another.
    """
    code = code or ""
    if err := _too_big(code):
        return err
    store = _read_store()
    art = _find(store, artifact_id or store["current"])
    if art is None:
        return "No artifact to rewrite. Create one with show_artifact first."
    if title:
        art["title"] = title
    v = _commit_version(store, art, code)
    return f"Rewrote artifact {art['id']} → version {v}." + _render_suffix(art["id"], v)


@tool
def list_artifacts() -> str:
    """List the artifacts in the panel (newest first) with id, kind, title and version count,
    so you can target ``update_artifact`` / ``rewrite_artifact`` / ``delete_artifact`` at a
    specific one. Read-only."""
    store = _read_store()
    if not store["artifacts"]:
        return "No artifacts yet. Create one with show_artifact."
    lines = []
    for a in store["artifacts"]:
        cur = "  · current" if a["id"] == store["current"] else ""
        lines.append(
            f"{a['id']}  [{a['kind']}]  {a['title'] or '(untitled)'}  · v{len(a['versions'])}{cur}"
        )
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
    store = _read_store()
    art = _find(store, artifact_id or store["current"])
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
    another. Read-only."""
    store = _read_store()
    art = _find(store, artifact_id or store["current"])
    if art is None:
        return "No artifact to check. Use list_artifacts to see the ids, or show_artifact to create one."
    v = len(art["versions"])
    r = _version_render(art, v)
    if r is None:
        return (
            f"Artifact {art['id']} v{v}: no render result yet — the Artifact panel may be "
            "closed or not showing this version. Open it to render."
        )
    if r.get("ok"):
        return f"Artifact {art['id']} v{v}: rendered cleanly."
    err = str(r.get("error") or "render failed").strip()
    return (
        f"Artifact {art['id']} v{v}: render FAILED —\n  {err}\n"
        "Fix it with update_artifact / rewrite_artifact."
    )


@tool
def delete_artifact(artifact_id: str) -> str:
    """Delete an artifact (all its versions) from the panel — for cleanup. The user can also
    delete from the panel's trash button. Pass the ``artifact_id`` (see ``list_artifacts``)."""
    store = _read_store()
    if _find(store, artifact_id) is None:
        return f"No artifact {artifact_id!r}. Use list_artifacts to see the ids."
    store["artifacts"] = [a for a in store["artifacts"] if a["id"] != artifact_id]
    if store["current"] == artifact_id:
        store["current"] = store["artifacts"][0]["id"] if store["artifacts"] else None
    _write_store(store)
    _emit("deleted", {"id": artifact_id})
    return f"Deleted artifact {artifact_id}."


def _build_view_router():
    """The shell PAGE — served under the PUBLIC ``/plugins/artifact`` prefix
    (plugin-view rule 2): a browser iframe page-load can't carry an Authorization
    bearer, so a gated page 401-blanks under the token gate. The page is also where
    the slug-aware base is derived (``location.pathname.split("/plugins/")[0]``), so
    it MUST live under ``/plugins/`` — a ``/api/plugins/`` page poisons the base to
    ``/api`` and the kit's ``/_ds/`` assets 404 (the bug this split fixes). The page
    fetches its DATA from the gated data router with the handshake token."""
    from fastapi import APIRouter
    from fastapi.responses import FileResponse, HTMLResponse, Response

    router = APIRouter()

    @router.get("/view")
    async def _view():
        return HTMLResponse(_SHELL_HTML)

    # Vendored JS libs (react/react-dom/babel/mermaid) served SAME-ORIGIN so the
    # react/mermaid kinds work fully OFFLINE — no cdnjs dependency, and the
    # `network: []` capability is now literally true. Allowlisted (no path
    # traversal); the sandboxed artifact iframe loads these by absolute URL.
    # Versioned bytes → cache hard; SRI in the artifact still pins them.
    @router.get("/vendor/{name}")
    async def _vendor(name: str):
        if name not in _VENDOR_FILES:
            return Response(status_code=404)
        f = Path(__file__).parent / "vendor" / name
        if not f.exists():
            return Response(status_code=404, content=f"{name} not vendored")
        return FileResponse(
            f,
            media_type="application/javascript",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                # The sandboxed artifact iframe is an opaque origin, so its load of
                # this lib is cross-origin → CORS + crossorigin="anonymous" are
                # needed for the SRI check to run.
                "Access-Control-Allow-Origin": "*",
            },
        )

    return router


def _build_data_router():
    """The DATA routes — mounted under ``/api/plugins/artifact`` so they inherit the
    operator bearer gate (plugin-view rule 2). The shell page reads them with the
    handshake token; DELETE is the panel's user-driven cleanup."""
    from fastapi import APIRouter, Body, HTTPException

    router = APIRouter()

    @router.get("/current")
    async def _current_artifact() -> dict:
        """The focused artifact's latest version (back-compat shape + version info)."""
        _note_poll()  # a poll ⇒ a renderer is live (gates the inline render-error wait, #1458)
        store = _read_store()
        art = _find(store, store["current"])
        if art is None:
            return {
                "id": "",
                "kind": "",
                "code": "",
                "title": "",
                "ts": 0,
                "version": 0,
            }
        v = art["versions"][-1]
        return {
            "id": art["id"],
            "kind": art["kind"],
            "code": v["code"],
            "title": art["title"],
            "ts": v["ts"],
            "version": len(art["versions"]),
        }

    @router.get("/history")
    async def _history() -> dict:
        """The full store — every artifact with its version chain — for the panel's
        artifact picker + version navigation."""
        _note_poll()  # a poll ⇒ a renderer is live (gates the inline render-error wait, #1458)
        return _read_store()

    @router.post("/render-status")
    async def _render_status(body: dict = Body(...)) -> dict:
        """The sandbox's render verdict for a version, relayed by the shell (#1458): the
        nested artifact frame reports ``{ok}`` once it mounts or ``{ok:false, error}`` when
        it throws / never mounts. Stamped onto the version so check_artifact + the create/edit
        tools can surface render failures back to the agent. Best-effort: unknown id/version
        is a no-op (the panel may be a version behind), never an error."""
        art_id = str(body.get("id") or "")
        try:
            version = int(body.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        store = _read_store()
        art = _find(store, art_id)
        if art is None or not (1 <= version <= len(art.get("versions") or [])):
            return {"ok": True, "recorded": False}
        art["versions"][version - 1]["render"] = {
            "ok": bool(body.get("ok")),
            "error": str(body.get("error") or "")[:_RENDER_ERR_MAX],
            "ts": _now(),
        }
        _write_store(store)
        return {"ok": True, "recorded": True}

    @router.post("/ask")
    async def _ask(body: dict = Body(...)) -> dict:
        """Interactive bridge: a sandboxed artifact's ``window.protoArtifact.ask(prompt)``
        reaches the agent here (the ``window.claude.complete`` analog). OPT-IN
        (``ARTIFACT_ASK_ENABLED``) — letting artifact code trigger LLM calls is a
        cost/abuse surface. Gated by the operator bearer like the rest. Runs a BARE
        completion (no tools/agent loop) via the consumption SDK."""
        if not _ask_enabled():
            raise HTTPException(
                403,
                "Artifact 'ask' is disabled — set ARTIFACT_ASK_ENABLED=1 to let "
                "artifacts call the agent.",
            )
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            raise HTTPException(400, "prompt required")
        cap = _ask_max_chars()
        if len(prompt) > cap:
            raise HTTPException(413, f"prompt too long (> {cap} chars)")
        try:
            from graph.sdk import complete  # ADR 0043 consumption SDK
        except Exception:  # noqa: BLE001
            raise HTTPException(
                501,
                "This protoAgent build doesn't support artifact ask "
                "(needs graph.sdk.complete — upgrade the host).",
            ) from None
        try:
            text = await complete(prompt, system=_ask_system())
        except Exception as e:  # noqa: BLE001
            log.warning("[artifact] ask completion failed", exc_info=True)
            raise HTTPException(502, f"completion failed: {e}") from None
        return {"text": text}

    @router.put("/artifact/{art_id}")
    async def _save_edit(art_id: str, body: dict = Body(...)) -> dict:
        """Save a USER edit (the panel's in-panel code editor) as a new version. Like the
        agent's rewrite, but tagged ``by: user`` so the provenance is visible — and, like
        every edit, it APPENDS a version rather than overwriting (no silent clobber)."""
        code = str(body.get("code", ""))
        if err := _too_big(code):
            raise HTTPException(413, err)
        store = _read_store()
        art = _find(store, art_id)
        if art is None:
            raise HTTPException(404, f"unknown artifact {art_id}")
        v = _commit_version(store, art, code, by="user")
        return {"ok": True, "id": art_id, "version": v}

    @router.delete("/artifact/{art_id}")
    async def _delete(art_id: str) -> dict:
        """Delete an artifact (the panel's trash button). Gated like the rest."""
        store = _read_store()
        if _find(store, art_id) is None:
            raise HTTPException(404, f"unknown artifact {art_id}")
        store["artifacts"] = [a for a in store["artifacts"] if a["id"] != art_id]
        if store["current"] == art_id:
            store["current"] = (
                store["artifacts"][0]["id"] if store["artifacts"] else None
            )
        _write_store(store)
        _emit("deleted", {"id": art_id})
        return {"ok": True, "deleted": art_id}

    return router


def register(registry) -> None:
    global _REGISTRY
    _REGISTRY = registry
    for t in (
        show_artifact,
        update_artifact,
        rewrite_artifact,
        list_artifacts,
        get_artifact,
        check_artifact,
        delete_artifact,
    ):
        registry.register_tool(t)
    registry.register_skill_dir(
        "skills"
    )  # teaches: render with show_artifact, edit with update/rewrite, don't write files
    # TWO routers at DISTINCT prefixes (a same-prefix second router is silently
    # de-duped by the host): the PAGE on public /plugins/artifact (iframe-loadable,
    # base-derivation-safe) and the DATA routes on gated /api/plugins/artifact.
    registry.register_router(_build_view_router(), prefix="/plugins/artifact")
    registry.register_router(_build_data_router(), prefix="/api/plugins/artifact")


# The shell page (ADR 0026 iframe). It takes the operator bearer via the console's postMessage
# handshake, polls /history, and renders the selected artifact+version into a NESTED sandboxed iframe. The
# nested frame is sandbox="allow-scripts" with NO allow-same-origin — generated code is isolated.
_SHELL_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<script>
  // Slug-aware base (protoAgent ADR 0042, plugin-view rule 3) computed FIRST — the
  // kit's own <link> loads before the kit exists, so it's base-prefixed by hand.
  window.__base = location.pathname.split("/plugins/")[0];
  document.write('<link rel="stylesheet" href="' + window.__base + '/_ds/plugin-kit.css">');
</script>
<style>
  /* Layout only — colors/typography come from plugin-kit.css's --pl-* tokens, which
     plugin-kit.js re-skins to the operator's live theme (dark fallbacks, no flash). */
  html,body{margin:0;height:100%;background:var(--pl-color-bg,#0a0a0c);color:var(--pl-color-fg-muted,#9aa0aa);
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif)}
  #wrap{display:flex;flex-direction:column;height:100%}
  /* Toolbar: artifact picker + version nav + download/delete. Hidden until there's one. */
  #bar{display:none;align-items:center;gap:6px;padding:6px 10px;
    border-bottom:var(--pl-border-width,1px) solid var(--pl-color-border,#2a2a30);font-size:12px}
  #art{flex:1;min-width:0}
  #vnav{display:flex;align-items:center;gap:2px}
  #vlabel{min-width:48px;text-align:center;color:var(--pl-color-fg-muted);font-variant-numeric:tabular-nums}
  #vnav button[disabled]{opacity:.4;cursor:default}
  #stage{flex:1;min-height:0;position:relative}
  #empty{display:flex;align-items:center;justify-content:center;height:100%;text-align:center;padding:24px;font-size:14px}
  /* No white flash — the artifact frame defaults to the console's ground (ADR 0038). */
  #frame{border:0;width:100%;height:100%;display:none;background:var(--pl-color-bg,#0a0a0c)}
  /* In-panel code editor (direct user editing → a new version). */
  #editor{position:absolute;inset:0;display:none;flex-direction:column;background:var(--pl-color-bg,#0a0a0c)}
  #code{flex:1;min-height:0;resize:none;border:0;outline:none;padding:10px;background:transparent;
    color:var(--pl-color-fg,#ededed);font-family:var(--pl-font-mono,ui-monospace,SFMono-Regular,Menlo,monospace);
    font-size:12px;line-height:1.5;tab-size:2}
  #ebar{display:flex;align-items:center;gap:6px;padding:6px 10px;
    border-top:var(--pl-border-width,1px) solid var(--pl-color-border,#2a2a30)}
  #estat{color:var(--pl-color-fg-muted,#9aa0aa);font-size:12px}
  #ebar .grow{flex:1}
</style></head><body>
<div id="wrap">
  <div id="bar">
    <select id="art" class="pl-input" title="Artifact"></select>
    <span id="vnav">
      <button id="vprev" class="pl-btn pl-btn--sm" type="button" title="Previous version">‹</button>
      <span id="vlabel"></span>
      <button id="vnext" class="pl-btn pl-btn--sm" type="button" title="Next version">›</button>
    </span>
    <button id="edit" class="pl-btn pl-btn--sm" type="button" title="Edit the source">Edit</button>
    <button id="dl" class="pl-btn pl-btn--sm" type="button" title="Download this version">Download</button>
    <button id="del" class="pl-btn pl-btn--sm" type="button" title="Delete this artifact">Delete</button>
  </div>
  <div id="stage">
    <div id="empty">No artifact yet. Ask the agent to render one — a chart, diagram, or widget.</div>
    <iframe id="frame" sandbox="allow-scripts allow-pointer-lock" referrerpolicy="no-referrer"></iframe>
    <div id="editor">
      <textarea id="code" class="pl-input" spellcheck="false" placeholder="Edit the artifact source…"></textarea>
      <div id="ebar">
        <span id="estat"></span><span class="grow"></span>
        <button id="cancel" class="pl-btn pl-btn--sm" type="button">Cancel</button>
        <button id="run" class="pl-btn pl-btn--primary pl-btn--sm" type="button">Run &amp; save</button>
      </div>
    </div>
  </div>
</div>
<script type="module">
  // The DS plugin-kit owns the protoagent:init handshake (bearer + theme, incl. live
  // re-themes onto the --pl-* tokens) and slug-aware authed fetches — replacing the
  // hand-rolled listener/theme map this page carried. plugin-kit.js is an ES MODULE,
  // so it loads via dynamic import (a classic <script src> throws on its exports;
  // see protoAgent docs/how-to/build-a-plugin-view.md). Older host without /_ds:
  // fall back to a tokenless same-origin shim.
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) { kit = { initPluginView(){}, apiFetch: (p, i) => fetch(window.__base + p, i) }; }
  // Store mirror: arts = [{id,kind,title,versions:[{code,ts,by}]}], curId = focused.
  // selId/selVer = the artifact + version the USER is viewing (selVer null = latest, so
  // it auto-follows new versions). followNewest jumps to the newest artifact on create
  // unless the user navigated to an older one.
  var arts = [], curId = null, selId = null, selVer = null, followNewest = true, lastRendered = "";
  var renderingId = null, renderingVer = 0;  // the (id, 1-based version) currently in the frame — for render-status (#1458)
  var EXT = { html: "html", svg: "svg", mermaid: "mmd", react: "jsx" };
  function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;"); }
  // The NESTED artifact iframe (sandboxed, no stylesheet access) gets the live theme
  // injected as literal colors — read the kit-managed tokens at render time.
  // Injected into EVERY artifact (the window.claude.complete analog): the artifact
  // calls window.protoArtifact.ask(prompt) → a Promise that round-trips via the shell
  // (postMessage) to the gated /ask endpoint → the agent → back. parent.postMessage
  // works from the sandbox; the shell validates e.source and calls the bearer-gated
  // endpoint. ask() rejects if the operator hasn't enabled it (ARTIFACT_ASK_ENABLED).
  var SHIM = '<script>(function(){var s=0,w={};'
    + 'window.addEventListener("message",function(e){var m=e.data||{};if(m.type!=="protoArtifact:result")return;'
    + 'var p=w[m.id];if(!p)return;delete w[m.id];m.error?p.reject(new Error(m.error)):p.resolve(m.text);});'
    + 'window.protoArtifact={ask:function(prompt){return new Promise(function(res,rej){var id=++s;w[id]={resolve:res,reject:rej};'
    + 'parent.postMessage({type:"protoArtifact:ask",id:id,prompt:String(prompt)},"*");'
    + 'setTimeout(function(){if(w[id]){delete w[id];rej(new Error("ask timed out"));}},60000);});}};'
    + '})();<\/script>';
  // Design-system surface: link the same-origin DS plugin-kit stylesheet (host-served at
  // /_ds/, min_protoagent_version 0.34.0) into html/react/markdown artifacts so they can use
  // the `.pl-*` component classes + `--pl-*` tokens and match the console. A cross-origin
  // <link> applies without CORS (only CSSOM access is gated), so the opaque sandbox can load it.
  function dsLink(){ return '<link rel="stylesheet" href="' + ORIGIN + '/_ds/plugin-kit.css">'; }
  // Error surfacing (injected into EVERY artifact via base()): register global error /
  // unhandledrejection handlers that lazily drop a fixed bottom overlay into the frame — so a
  // broken artifact shows WHY instead of a silent blank. Exposes window.__artErr(msg) for the
  // harness's own guards (e.g. the React no-mount check) to reuse.
  // Also REPORTS the render result up to the shell (#1458): rep(false,msg) on any error,
  // rep(true) once it's confirmed rendered — the shell relays it to /render-status so the
  // agent's create/edit reply (and check_artifact) can surface a render failure. Once-only
  // (__artRep) so the first verdict wins. KIND (set by base) gates the on-load OK: react
  // confirms via the no-mount guard's firstChild check instead (mount is async, post-load).
  var ERRBOOT = '<script>(function(){var W=window;'
    + 'function rep(ok,err){if(W.__artRep)return;W.__artRep=1;'
    + 'try{parent.postMessage({type:"protoArtifact:render",ok:!!ok,error:err?String(err).slice(0,2000):""},"*");}catch(_){}}'
    + 'function show(m){var d=document.getElementById("__arterr");'
    + 'if(!d){d=document.createElement("div");d.id="__arterr";'
    + 'd.style.cssText="position:fixed;left:0;right:0;bottom:0;max-height:60%;overflow:auto;margin:0;padding:10px 13px;background:#2a0f12;color:#ffb4b4;font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;border-top:2px solid #f87171;z-index:2147483647";'
    + '(document.body||document.documentElement).appendChild(d);}d.textContent=String(m);rep(false,m);}'
    + 'W.__artErr=show;W.__artOk=function(){rep(true,"");};'
    + 'addEventListener("error",function(e){show("⚠ "+(e.message||(e.error&&e.error.message)||"Script error")+(e.lineno?" (line "+e.lineno+")":""));},true);'
    + 'addEventListener("unhandledrejection",function(e){show("⚠ "+((e.reason&&e.reason.message)||e.reason));});'
    + 'addEventListener("load",function(){if(W.__artKind!=="react")setTimeout(function(){if(!W.__artRep)W.__artOk();},80);});'
    + '})();<\/script>';
  function base(kind){
    var cs = getComputedStyle(document.documentElement);
    function tok(n,d){ return (cs.getPropertyValue(n) || d).trim(); }
    var bg=tok("--pl-color-bg","#0a0a0c"), fg=tok("--pl-color-fg","#ededed"),
        accent=tok("--pl-color-accent","#9b87f2"), border=tok("--pl-color-border","rgba(255,255,255,.08)");
    // Carry the live theme's key tokens into the nested frame (plugin-kit.css ships only the
    // DEFAULT palette); inline bg = no white flash; SHIM = the protoArtifact.ask bridge.
    // __artKind lets ERRBOOT decide how to confirm a clean render (on-load vs react mount).
    return '<style>:root{--pl-color-bg:'+bg+';--pl-color-fg:'+fg+';--pl-color-accent:'+accent+';--pl-color-border:'+border+'}'
      + 'html,body{margin:0;background:'+bg+';color:'+fg+'}</style>'
      + '<script>window.__artKind=' + JSON.stringify(kind||"") + ';<\/script>' + SHIM + ERRBOOT;
  }
  // Artifact libs are VENDORED + served same-origin (/plugins/artifact/vendor/…), so
  // react/mermaid renders work fully OFFLINE — no cdnjs dependency. Still pinned with
  // Subresource Integrity (sha512 of the exact vendored bytes), so a tampered served
  // file won't execute. Absolute URL (origin + base) because an srcdoc iframe has no
  // own URL to resolve a relative path against. Bump the file AND the hash together.
  var ORIGIN = location.origin + window.__base;  // "" + base, slug-aware
  var LIB = {
    mermaid: ["mermaid.min.js",
      "sha512-6a80OTZVmEJhqYJUmYd5z8yHUCDlYnj6q9XwB/gKOEyNQV/Q8u+XeSG59a2ZKFEHGTYzgfOQKYEBtrZV7vBr+Q=="],
    react: ["react.production.min.js",
      "sha512-QVs8Lo43F9lSuBykadDb0oSXDL/BbZ588urWVCRwSIoewQv/Ewg1f84mK3U790bZ0FfhFa1YSQUmIhG+pIRKeg=="],
    reactDom: ["react-dom.production.min.js",
      "sha512-6a1107rTlA4gYpgHAqbwLAtxmWipBdJFcq8y5S/aTge3Bp+VAklABm2LO+Kg51vOWR9JMZq1Ovjl5tpluNpTeQ=="],
    babel: ["babel.min.js",
      "sha512-bAHF//mCdqGSgyUBqhtDgaGLxsraipURsQRGG+3uNncZdsFA6/283u21SOwB6rzINUXSATUMoZaXm4IaV2Lw2Q=="],
  };
  // crossorigin="anonymous" is REQUIRED even though the lib is same-origin to the
  // shell: the artifact runs in a no-same-origin sandbox (opaque origin), so its
  // subresource loads are cross-origin — SRI on a cross-origin script without
  // crossorigin can't validate and the browser blocks it. The vendor route sends
  // Access-Control-Allow-Origin:* to satisfy the CORS fetch.
  function cdn(name){ var c = LIB[name];
    return '<script crossorigin="anonymous" integrity="' + c[1] + '" src="' + ORIGIN + '/plugins/artifact/vendor/' + c[0] + '"><\/script>'; }
  // Curated ESM import map for `react` artifacts (offline-vendored, served same-origin with
  // CORS). Bare specifiers resolve to the vendored modules: react/react-dom via tiny shims that
  // re-export the UMD globals (so the artifact, the @pl/ui wrappers, and any lib share ONE
  // React instance), plus d3 / chart.js / lucide and the authored @pl/ui DS wrappers.
  var V = ORIGIN + "/plugins/artifact/vendor/";
  var IMPORTMAP = JSON.stringify({ imports: {
    "react": V + "react.shim.mjs",
    "react-dom": V + "react-dom-client.shim.mjs",
    "react-dom/client": V + "react-dom-client.shim.mjs",
    "@pl/ui": V + "pl-ui.mjs",
    "d3": V + "d3.mjs",
    "chart.js": V + "chartjs.mjs",
    "chart.js/auto": V + "chartjs.mjs",
    "lucide": V + "lucide.mjs"
  }});
  // Prose styling for markdown, keyed to --pl-* tokens (the DS link supplies component classes).
  var MD_CSS = '#md{max-width:50rem;margin:0 auto;padding:20px;line-height:1.6}'
    + '#md h1,#md h2,#md h3,#md h4{line-height:1.25;margin:1.4em 0 .5em}#md h1{font-size:1.7em}#md h2{font-size:1.35em}#md h3{font-size:1.12em}'
    + '#md a{color:var(--pl-color-accent,#9b87f2)}'
    + '#md code{font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);font-size:.9em;background:rgba(127,127,127,.16);padding:.15em .35em;border-radius:4px}'
    + '#md pre{background:rgba(127,127,127,.12);padding:12px;border-radius:6px;overflow:auto}#md pre code{background:none;padding:0}'
    + '#md table{border-collapse:collapse}#md th,#md td{border:1px solid var(--pl-color-border,rgba(255,255,255,.14));padding:6px 10px}'
    + '#md blockquote{margin:1em 0;padding-left:1em;border-left:3px solid var(--pl-color-border,rgba(255,255,255,.2));color:var(--pl-color-fg-muted,#9aa0aa)}'
    + '#md img{max-width:100%}#md .mermaid{background:none;border:0;padding:0}';

  function srcdoc(kind, code) {
    if (kind === "html") return dsLink() + base(kind) + code;
    if (kind === "svg") return '<!doctype html>' + base(kind) + '<body style="display:grid;place-items:center;min-height:100vh">' + code + '</body>';
    if (kind === "mermaid") return '<!doctype html>' + base(kind) + '<body><pre class="mermaid">' + esc(code) + '</pre>' +
      cdn("mermaid") +
      '<script>mermaid.initialize({startOnLoad:false,theme:"dark"});mermaid.run();<\/script></body>';
    if (kind === "markdown") return mdDoc(code);
    // `react`: import map + UMD react/react-dom/babel, compiled as a MODULE so `import` works
    // (no-import artifacts still run — they use the UMD React/ReactDOM globals as before).
    if (kind === "react") return '<!doctype html>' + dsLink() + base(kind) + '<body><div id="root"></div>' +
      '<script type="importmap">' + IMPORTMAP + '<\/script>' +
      cdn("react") + cdn("reactDom") + cdn("babel") +
      '<script type="text/babel" data-type="module" data-presets="react">' + code + '<\/script>' +
      // No-mount guard: a babel module that defines a component but never calls render() leaves
      // #root empty with NO thrown error — the silent blank that reads as "stuck". Poll briefly;
      // mount → report a clean render (#1458); if #root never gets a child and nothing else
      // errored, surface an actionable message (which also reports the failure up).
      '<script>(function(){var n=0,t=setInterval(function(){var r=document.getElementById("root");'
      + 'if(r&&r.firstChild){clearInterval(t);if(window.__artOk)window.__artOk();return;}'
      + 'if(++n>=30){clearInterval(t);if(window.__artErr&&!document.getElementById("__arterr"))'
      + 'window.__artErr("Nothing rendered into #root — a React artifact must MOUNT itself, e.g. createRoot(document.getElementById(\'root\')).render(<App/>). Defining a component is not enough; you must call render().");'
      + '}},100);})();<\/script></body>';
    return '<!doctype html>' + base(kind) + '<body style="font-family:sans-serif;padding:16px">unsupported artifact kind</body>';
  }
  // markdown → HTML via the vendored `marked` ESM. The source is base64'd into the module
  // (unicode-safe; sidesteps quote / newline / closing-tag escaping pitfalls). A fenced
  // mermaid block also pulls mermaid in and upgrades those code blocks to live diagrams.
  // DS classes/tokens via dsLink + MD_CSS.
  function mdDoc(code){
    var b64 = btoa(unescape(encodeURIComponent(code)));
    var hasMermaid = code.indexOf("```mermaid") >= 0;
    var mmRun = hasMermaid
      ? 'document.querySelectorAll("#md pre>code.language-mermaid").forEach(function(c){var d=document.createElement("pre");d.className="mermaid";d.textContent=c.textContent;c.parentNode.replaceWith(d);});'
        + 'if(window.mermaid){mermaid.initialize({startOnLoad:false,theme:"dark"});mermaid.run();}'
      : "";
    return '<!doctype html>' + dsLink() + base("markdown") + '<style>' + MD_CSS + '</style>' +
      '<body><div id="md" class="pl-prose"></div>' +
      '<script type="importmap">{"imports":{"marked":"' + V + 'marked.mjs"}}<\/script>' +
      (hasMermaid ? cdn("mermaid") : "") +
      '<script type="module">import { marked } from "marked";' +
      'document.getElementById("md").innerHTML = marked.parse(decodeURIComponent(escape(atob("' + b64 + '"))));' +
      mmRun + '<\/script></body>';
  }
  var $art=document.getElementById("art"), $vprev=document.getElementById("vprev"),
      $vnext=document.getElementById("vnext"), $vlabel=document.getElementById("vlabel"),
      $dl=document.getElementById("dl"), $del=document.getElementById("del"),
      $bar=document.getElementById("bar"), $empty=document.getElementById("empty"),
      $frame=document.getElementById("frame"), $edit=document.getElementById("edit"),
      $editor=document.getElementById("editor"), $code=document.getElementById("code"),
      $run=document.getElementById("run"), $cancel=document.getElementById("cancel"),
      $estat=document.getElementById("estat");
  var editing=false;

  function selArt(){ for(var i=0;i<arts.length;i++) if(arts[i].id===selId) return arts[i]; return arts[0]||null; }
  function verIdx(a){ // selVer clamped to a's range; null/out-of-range → latest (auto-follow)
    if(!a) return 0; var n=a.versions.length;
    return (selVer===null||selVer<0||selVer>n-1) ? n-1 : selVer;
  }
  function rebuildArtSelect(){
    $art.innerHTML="";
    arts.forEach(function(a){
      var o=document.createElement("option"); o.value=a.id;
      o.textContent=(a.id===curId?"● ":"")+(a.title||(a.kind+" artifact"))+"  ·  "+a.kind+"  · v"+a.versions.length;
      $art.appendChild(o);
    });
    var a=selArt(); if(a) $art.value=a.id;
  }
  function render(){
    $bar.style.display = arts.length ? "flex" : "none";
    var a=selArt();
    if(!a){ $empty.style.display="flex"; $frame.style.display="none"; lastRendered=""; return; }
    var vi=verIdx(a), v=a.versions[vi];
    $vlabel.textContent="v"+(vi+1)+"/"+a.versions.length;
    $vprev.disabled = vi<=0; $vnext.disabled = vi>=a.versions.length-1;
    $empty.style.display="none";
    var key=a.id+"@"+vi;  // re-srcdoc only when the shown version actually changes
    if(key!==lastRendered){ lastRendered=key; renderingId=a.id; renderingVer=vi+1; $frame.srcdoc=srcdoc(a.kind, v.code); $frame.style.display="block"; }
  }

  $art.addEventListener("change", function(e){
    selId=e.target.value; selVer=null; followNewest=(selId===(curId||(arts[0]&&arts[0].id)));
    render();
  });
  $vprev.addEventListener("click", function(){ var a=selArt(); if(!a)return; var vi=verIdx(a);
    if(vi>0){ selVer=vi-1; followNewest=false; render(); } });
  $vnext.addEventListener("click", function(){ var a=selArt(); if(!a)return; var vi=verIdx(a);
    if(vi<a.versions.length-1){ selVer=vi+1; if(selVer===a.versions.length-1) selVer=null; render(); } });

  $dl.addEventListener("click", function(){
    var a=selArt(); if(!a)return; var vi=verIdx(a), v=a.versions[vi];
    var blob=new Blob([v.code],{type:"text/plain"}); var u=URL.createObjectURL(blob);
    var el=document.createElement("a"); el.href=u; el.download="artifact-"+a.id+"-v"+(vi+1)+"."+(EXT[a.kind]||"txt");
    document.body.appendChild(el); el.click(); el.remove(); setTimeout(function(){URL.revokeObjectURL(u);},1000);
  });

  // Inline two-click confirm (no confirm() — a sandboxed plugin iframe may block modals).
  var delArm=null, delT=null;
  $del.addEventListener("click", async function(){
    var a=selArt(); if(!a)return;
    if(delArm!==a.id){ delArm=a.id; $del.textContent="Confirm?";
      clearTimeout(delT); delT=setTimeout(function(){ if(delArm===a.id){delArm=null;$del.textContent="Delete";} },3000); return; }
    clearTimeout(delT); delArm=null; $del.textContent="Delete";
    try{ await kit.apiFetch("/api/plugins/artifact/artifact/"+encodeURIComponent(a.id),{method:"DELETE"}); }catch(e){}
    selId=null; selVer=null; followNewest=true; poll();
  });

  // In-panel code editor — edit the SELECTED version's source and save it as a NEW
  // version (by:"user"), so direct editing never clobbers the agent's versions.
  // The editor is an OPAQUE overlay (#editor is position:absolute, inset:0) that sits
  // ABOVE the artifact frame — so we never hide or re-srcdoc the frame to edit. Tearing
  // it down and re-rendering on EXIT raced the display:block reflow: mermaid then
  // measured its text at 0 size and emitted `transform: translate(undefined, NaN)`,
  // leaving a blank (black) panel that the version-keyed `lastRendered` cache never
  // repainted (→ "went black, needed a page refresh"). Keeping the frame laid out the
  // whole time means any re-render only happens while it's visible and sized.
  function enterEdit(){
    var a=selArt(); if(!a) return; var vi=verIdx(a);
    $code.value=a.versions[vi].code; $estat.textContent="";
    editing=true; $edit.textContent="Editing"; $editor.style.display="flex";
    $empty.style.display="none"; $code.focus();
  }
  function exitEdit(){ editing=false; $edit.textContent="Edit"; $editor.style.display="none"; render(); }
  $edit.addEventListener("click", function(){ editing ? exitEdit() : enterEdit(); });
  $cancel.addEventListener("click", exitEdit);
  $run.addEventListener("click", async function(){
    var a=selArt(); if(!a) return;
    $estat.textContent="Saving…"; $run.disabled=true;
    try{
      var r=await kit.apiFetch("/api/plugins/artifact/artifact/"+encodeURIComponent(a.id),
        {method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:$code.value})});
      if(!r.ok) throw 0;
      followNewest=true; await poll(); exitEdit();   // show the just-saved new version
    }catch(e){ $estat.textContent="Save failed"; }
    $run.disabled=false;
  });

  // Agent-callback bridge: an artifact's window.protoArtifact.ask(prompt) posts here;
  // we call the bearer-gated /ask endpoint (a bare agent completion) and post the
  // answer back INTO the artifact frame. e.source-guarded to only our artifact frame —
  // the kit's own protoagent:init handshake messages are ignored here.
  window.addEventListener("message", async function(e){
    if(!$frame || e.source!==$frame.contentWindow) return;
    var m=e.data||{};
    // Render verdict from the sandbox (#1458) → relay to /render-status so the agent's
    // create/edit reply + check_artifact can surface a render failure. Best-effort POST.
    if(m.type==="protoArtifact:render"){
      if(renderingId){ try{ kit.apiFetch("/api/plugins/artifact/render-status",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:renderingId,version:renderingVer,ok:!!m.ok,error:String(m.error||"").slice(0,2000)})}); }catch(_){} }
      return;
    }
    if(m.type!=="protoArtifact:ask") return;
    function reply(p){ try{ $frame.contentWindow.postMessage(Object.assign({type:"protoArtifact:result",id:m.id},p),"*"); }catch(_){} }
    try{
      var r=await kit.apiFetch("/api/plugins/artifact/ask",
        {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt:m.prompt})});
      if(!r.ok){ var t=""; try{ t=await r.text(); }catch(_){} reply({error:("ask failed ("+r.status+") "+t).slice(0,300)}); return; }
      var d=await r.json(); reply({text:(d&&d.text)||""});
    }catch(err){ reply({error:String(err).slice(0,300)}); }
  });

  async function poll() {
    if (document.hidden) return;  // don't poll while the window is hidden/minimized (desktop perf)
    try {
      var r = await kit.apiFetch("/api/plugins/artifact/history");
      var d = await r.json(); arts = (d && d.artifacts) || []; curId = (d && d.current) || null;
      if (followNewest) { selId = curId || (arts[0] && arts[0].id) || null; selVer = null; }
      rebuildArtSelect(); render();
    } catch (e) { /* transient */ }
  }
  // Boot ONCE, on whichever fires first: the handshake (the bearer arrives with
  // protoagent:init, so the gated history poll authenticates) or a short timer
  // for the no-handshake case (standalone page / older host).
  var booted = false;
  function boot(){ if (booted) return; booted = true; poll(); setInterval(poll, 1500); }
  kit.initPluginView(boot);
  setTimeout(boot, 800);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden && booted) poll(); }); // refresh on return
</script></body></html>"""
