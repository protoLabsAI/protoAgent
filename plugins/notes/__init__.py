"""Notes plugin (ADR 0034 S4) — the flagship reference plugin.

A single shared markdown note that BOTH the agent (via tools) and the operator
(via a console panel) read and write. The plugin owns its whole vertical: storage,
agent tools, and a sandboxed-iframe editor it serves itself (ADR 0038 — no host
build, git-installable). Still one note: no tabs, no versioning — deliberately the
basic notebook we actually want.

The editor renders markdown AS YOU TYPE (CodeMirror 6, vendored — see _VENDOR_FILES):
headings are sized, **bold** is bold, and the syntax chrome hides until your cursor
enters the line. There is no edit/preview toggle and nothing is ever rewritten on
disk — the file stays exactly the markdown you typed.

It models the VIEW-vs-DATA gating rule (the plugin-authoring guide): the editor
PAGE is served under the PUBLIC ``/plugins/notes`` prefix because a browser iframe
page-load can't carry an Authorization bearer — a gated page would 401-blank under
the token gate. Its DATA/action routes stay GATED under ``/api/plugins/notes`` and
are fetched from inside the loaded page with the postMessage handshake token.

It also models the FLEET-PROXY-SAFE fetch (ADR 0042): the editor derives its API
base from its own iframe path and prefixes every request, so a proxied
/agents/<slug>/ window talks to ITS agent — never the host's.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.notes")

# Serializes read-compare-write so the concurrency guard can't be raced. RE-entrant:
# the guarded PUT holds it across a _read() + _write(), and _write takes it too.
_LOCK = threading.RLock()

# The CodeMirror 6 closure, vendored and served same-origin: the editor works fully
# OFFLINE and the manifest's `network: []` is finally literal (it wasn't while the old
# preview pulled `marked` off cdnjs). Allowlisted — the route joins this name onto
# vendor/, so the set IS the path-traversal defence.
#
# WHY TEN FILES AND NOT ONE BUNDLE — the trap that makes CM6 look broken:
# CM6 packages carry IDENTITY. Facets, StateFields and EditorState are compared by
# REFERENCE, so two copies of @codemirror/state means markdown()'s extensions are
# rejected by the other copy's EditorView ("Unrecognized extension value"). esm.sh's
# default `?bundle` inlines a private copy of state into every package and fails exactly
# that way. These are built with `?external=` so each emits BARE specifiers, and the
# page's import map resolves every one to a single shared copy.
_VENDOR_FILES = {
    "state.mjs",
    "view.mjs",
    "language.mjs",
    "commands.mjs",  # history/undo + the default keymap
    "lang-markdown.mjs",
    "autocomplete.mjs",  # lang-markdown imports it
    "common.mjs",
    "highlight.mjs",
    "lr.mjs",
    "markdown.mjs",
    # Stands in for esm.sh's /node/process.mjs, which @lezer/lr's bundle imports for
    # `process.env.LOG`. Without it that import 404s, the module graph aborts, and the
    # editor silently renders as an empty box. See vendor/process.shim.mjs.
    "process.shim.mjs",
}


def _note_path() -> Path:
    """The single note file, instance-scoped (ADR 0004). ``NOTES_DIR`` overrides
    the base; ``PROTOAGENT_INSTANCE`` adds a per-instance subdir."""
    base = Path(os.environ.get("NOTES_DIR") or (Path.home() / ".protoagent" / "notes"))
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        base = base / inst
    base.mkdir(parents=True, exist_ok=True)
    return base / "note.md"


def _read() -> str:
    try:
        return _note_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write(content: str) -> None:
    """Atomic write so a crash mid-save never truncates the note."""
    with _LOCK:
        path = _note_path()
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def _updated_at() -> str | None:
    try:
        ts = _note_path().stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except FileNotFoundError:
        return None


def _version(content: str) -> str:
    """Opaque version token for the optimistic-concurrency guard (an ETag by another
    name). Content-hashed rather than mtime-derived: mtime collides under filesystem
    timestamp granularity when two writes land in the same tick, and it's clock-skew
    sensitive. A hash also means an idempotent rewrite never manufactures a conflict."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


@tool
def read_note() -> str:
    """Read the shared notes document (markdown). Use it to recall what you or the
    operator have written. Returns the full note text (empty string if blank)."""
    return _read()


@tool
def write_note(content: str) -> str:
    """Replace the entire shared notes document with ``content`` (markdown). This
    OVERWRITES the note — read_note first if you mean to keep the existing text."""
    _write(content)
    return f"Note saved ({len(content)} chars)."


@tool
def append_note(text: str) -> str:
    """Append ``text`` to the shared notes document (markdown), on a new line.
    Use this to add an entry without disturbing what's already there."""
    cur = _read()
    sep = "" if (not cur or cur.endswith("\n")) else "\n"
    _write(f"{cur}{sep}{text}\n")
    return f"Appended {len(text)} chars to the note."


def _build_view_router():
    """The editor PAGE — served under the PUBLIC ``/plugins/notes`` prefix (UNGATED).
    A browser iframe page-load can't carry an Authorization bearer, so a gated page
    would 401-blank under the token gate; the page itself is public chrome. It's the
    editor the console iframes (ADR 0038 — the plugin self-serves its UI; no host
    build, git-installable). The page then fetches its DATA from the gated data
    router with the postMessage handshake token."""
    from fastapi import APIRouter
    from fastapi.responses import FileResponse, HTMLResponse, Response

    router = APIRouter()

    @router.get("/view")
    async def _view():
        return HTMLResponse(_EDITOR_HTML)

    # The vendored CodeMirror modules, on the PUBLIC prefix alongside the page that
    # imports them — an iframe's module fetches carry no bearer either, so gating these
    # would break the editor exactly the way gating the page would. They're third-party
    # library bytes, not operator data. No CORS header needed: unlike artifact's srcdoc
    # (an opaque origin), this iframe is allow-same-origin, so these load same-origin.
    @router.get("/vendor/{name}")
    async def _vendor(name: str):
        if name not in _VENDOR_FILES:  # allowlist — the only path-traversal defence
            return Response(status_code=404)
        f = Path(__file__).parent / "vendor" / name
        if not f.exists():
            return Response(status_code=404, content=f"{name} not vendored")
        return FileResponse(
            f,
            media_type="application/javascript",
            # Version-pinned bytes that only change when the plugin does — cache hard.
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # The compact "quick note" PAGE (ADR 0057) — the plugin's PALETTE view: the same
    # shared note + autosave, trimmed chrome (no preview toggle) to fit the ⌘K palette
    # body. Demonstrates a DISTINCT palette page vs the full rail editor — the manifest
    # points the palette morph here via `palette: { path: /plugins/notes/quick }`.
    @router.get("/quick")
    async def _quick():
        return HTMLResponse(_QUICK_HTML)

    return router


def _build_data_router():
    """The Notes DATA/action routes — mounted under ``/api/plugins/notes`` so they
    inherit the operator bearer gate (P0 auth). ``/note`` is fetched from inside the
    loaded view page with the postMessage handshake token (the view page is public,
    the data is gated — the rule the plugin-authoring guide teaches)."""
    from fastapi import APIRouter, Body
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.get("/note")
    async def _get() -> dict:
        content = _read()
        return {"content": content, "updated_at": _updated_at(), "version": _version(content)}

    @router.put("/note")
    async def _put(body: dict = Body(...)):
        """Optimistic concurrency: a caller that passes ``base_version`` is saying "I
        edited the note as of THIS version" — if the note moved on (the agent's
        write_note landed while the operator was typing), we 409 with the current
        content instead of clobbering it. The editor only polls-and-adopts while it's
        clean, so without this guard the operator's next autosave silently overwrites
        the agent's write.

        ``base_version`` is OPTIONAL and its absence means force-overwrite — that
        keeps the agent's write_note tool (a documented full overwrite) and any older
        client working unchanged."""
        content = str(body.get("content", ""))
        base = body.get("base_version")
        with _LOCK:
            current = _read()
            current_version = _version(current)
            if base is not None and str(base) != current_version:
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "conflict": True,
                        "content": current,
                        "version": current_version,
                        "updated_at": _updated_at(),
                    },
                )
            _write(content)
        return {"ok": True, "updated_at": _updated_at(), "version": _version(content)}

    return router


def register(registry) -> None:
    """Entry point — called once at load with a PluginRegistry.

    Two routers at DISTINCT prefixes (a same-prefix second router would be silently
    dropped by the host's de-dupe — see server.agent_init._mount_plugin_routers):
    the view PAGE under the public ``/plugins/notes`` (ungated, iframe-loadable) and
    the DATA routes under ``/api/plugins/notes`` (gated, fetched with the token)."""
    registry.register_tools([read_note, write_note, append_note])
    # View PAGE: public /plugins/notes (ungated) — iframe nav can't carry a bearer.
    registry.register_router(_build_view_router(), prefix="/plugins/notes")
    # DATA routes: gated /api/plugins/notes — fetched with the handshake token.
    registry.register_router(_build_data_router(), prefix="/api/plugins/notes")


# The editor page the console iframes: a CodeMirror 6 markdown editor that renders as
# you type — headings sized, **bold** actually bold, syntax chrome hidden until your
# cursor enters the line — plus debounced autosave (PUT /note), the conflict guard, and
# a poll that adopts the agent's writes. No host build and no CDN: CM6 is vendored and
# served from this plugin, so it still installs from a git URL (ADR 0038) and runs
# offline.
#
# FOUR-RULES COMPLIANT (docs/how-to/build-a-plugin-view.md, the chat_example pattern):
#   3. SLUG-AWARE — the kit's apiFetch derives the base (host vs /agents/<slug> proxy)
#      for every data call; only the kit's own <link>/<script> are base-prefixed by
#      hand (they load before the kit exists). The import map gets this for FREE by
#      being relative — see the note on it below.
#   4. LINK THE KIT — <base>/_ds/plugin-kit.{css,js} replaces the hand-rolled hex
#      token map + bespoke protoagent:init listener + manual bearer headers this page
#      carried before: plugin-kit.js maps the operator's live theme onto --pl-*
#      (initial handshake AND protoagent:theme re-themes), and apiFetch attaches the
#      token. CodeMirror ships NO stylesheet of its own — only stable .cm-* hooks — so
#      the editor is themed entirely from --pl-* tokens and re-skins with the operator.
_EDITOR_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<script>
  "use strict";
  // Slug-aware base (ADR 0042), computed FIRST: the kit's own assets load before the
  // kit exists, so they're the one thing prefixed by hand (rule 4).
  window.__base = location.pathname.split("/plugins/")[0];
  document.write('<link rel="stylesheet" href="' + window.__base + '/_ds/plugin-kit.css">');
</script>
<!-- The vendored CodeMirror closure. RELATIVE specifiers are slug-aware for FREE: this
     page is served at <base>/plugins/notes/view, so "./vendor/x.mjs" resolves against
     it to <base>/plugins/notes/vendor/x.mjs — behind a fleet proxy (/agents/<slug>/…)
     it follows the proxy with no hand-prefixing, which is why this needs no __base.
     ONE entry per package, each to ONE file: CM6 compares Facets and EditorState by
     REFERENCE, so a duplicate @codemirror/state makes markdown()'s extensions
     unrecognizable to the other copy's EditorView. -->
<script type="importmap">
{"imports":{
  "@codemirror/state":"./vendor/state.mjs",
  "@codemirror/view":"./vendor/view.mjs",
  "@codemirror/language":"./vendor/language.mjs",
  "@codemirror/commands":"./vendor/commands.mjs",
  "@codemirror/lang-markdown":"./vendor/lang-markdown.mjs",
  "@codemirror/autocomplete":"./vendor/autocomplete.mjs",
  "@lezer/common":"./vendor/common.mjs",
  "@lezer/highlight":"./vendor/highlight.mjs",
  "@lezer/lr":"./vendor/lr.mjs",
  "@lezer/markdown":"./vendor/markdown.mjs",
  "/node/process.mjs":"./vendor/process.shim.mjs"
}}
</script>
<style>
  /* Layout + the editor's skin. Every colour is a --pl-* token, which plugin-kit.js
     re-skins to the operator's live theme on the handshake — no hex here. */
  html,body{margin:0;height:100%;background:var(--pl-color-bg-raised);color:var(--pl-color-fg);
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif)}
  #wrap{display:flex;flex-direction:column;height:100%}
  #bar{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;
    border-bottom:var(--pl-border-width,1px) solid var(--pl-color-border);
    font-size:12px;color:var(--pl-color-fg-muted)}
  #actions{display:flex;gap:6px;align-items:center}
  #conflict[hidden]{display:none}
  #conflict{display:flex;gap:6px}
  #ed{flex:1;min-height:0;overflow:hidden}

  /* CodeMirror hooks. It ships no colours of its own, so these are the whole theme.
     EVERY rule below is scoped with `.cm-editor` ON PURPOSE, and it is not cosmetic:
     CM's baseTheme injects `.ͼ1 .cm-scroller{font-family:monospace}` — a GENERATED
     class, so two-class specificity (0,2,0). A bare `.cm-scroller` (0,1,0) loses to it
     no matter where our stylesheet sits, and the editor silently renders monospace with
     the DS font ignored. `.cm-editor .cm-scroller` matches its specificity and wins on
     order (CM inserts its sheet at the TOP of <head> precisely so authors can override). */
  .cm-editor{height:100%;background:transparent}
  .cm-editor.cm-focused{outline:none}
  .cm-editor .cm-scroller{overflow:auto;font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif);
    line-height:1.7}
  .cm-editor .cm-content{padding:14px 16px;caret-color:var(--pl-color-fg)}
  .cm-editor .cm-cursor,.cm-editor .cm-dropCursor{border-left-color:var(--pl-color-fg)}
  .cm-editor .cm-selectionBackground,.cm-editor .cm-content ::selection,.cm-editor .cm-line ::selection{
    background:color-mix(in srgb, var(--pl-color-accent) 28%, transparent)}
  .cm-editor .cm-placeholder{color:var(--pl-color-fg-subtle)}

  /* Live-preview decorations — the "rendering". Sizes are em-relative so the whole
     editor scales from one font-size. */
  .cm-editor .cm-md-h1{font-size:1.7em;font-weight:700;line-height:1.3}
  .cm-editor .cm-md-h2{font-size:1.4em;font-weight:700;line-height:1.35}
  .cm-editor .cm-md-h3{font-size:1.2em;font-weight:600}
  .cm-editor .cm-md-h4{font-size:1.05em;font-weight:600}
  .cm-editor .cm-md-h5,.cm-editor .cm-md-h6{font-size:1em;font-weight:600;color:var(--pl-color-fg-muted)}
  .cm-editor .cm-md-strong{font-weight:700}
  .cm-editor .cm-md-em{font-style:italic}
  .cm-editor .cm-md-strike{text-decoration:line-through;color:var(--pl-color-fg-muted)}
  .cm-editor .cm-md-code{font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);font-size:.9em;
    background:var(--pl-color-bg-inset);padding:.1em .35em;border-radius:var(--pl-radius)}
  .cm-editor .cm-md-link{color:var(--pl-color-accent);text-decoration:underline;text-underline-offset:2px}
  .cm-editor .cm-md-quote{border-left:3px solid var(--pl-color-border-strong);padding-left:10px;
    color:var(--pl-color-fg-muted)}
  .cm-editor .cm-md-fence{font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);font-size:.9em;
    background:var(--pl-color-bg-inset)}
</style></head><body>
<div id="wrap">
  <div id="bar"><span id="status">Notes</span>
    <span id="actions">
      <span id="conflict" hidden>
        <button id="mine" class="pl-btn pl-btn--sm" type="button">Keep mine</button>
        <button id="theirs" class="pl-btn pl-btn--sm" type="button">Take theirs</button>
      </span>
    </span>
  </div>
  <div id="ed"></div>
</div>
<script type="module">
  "use strict";
  var st=document.getElementById("status");
  var cf=document.getElementById("conflict"), bMine=document.getElementById("mine"), bTheirs=document.getElementById("theirs");
  // plugin-kit.js is an ES MODULE (it has export statements) — a classic
  // <script src> throws "Unexpected token 'export'" and never sets the
  // window.protoPluginView global. Dynamic import is the no-build way to load it
  // with a slug-aware URL (a static import specifier can't carry the base).
  // If the kit can't load, FAIL LOUDLY. The old fallback here fetched tokenlessly,
  // which quietly 401s every save on a gated instance and looks like data loss; an
  // unthemed, unauthed editor is not a degraded editor, it's a broken one.
  import {EditorView, Decoration, ViewPlugin, keymap, placeholder} from "@codemirror/view";
  import {EditorState, Annotation} from "@codemirror/state";
  import {markdown, markdownLanguage} from "@codemirror/lang-markdown";
  import {syntaxTree} from "@codemirror/language";
  import {defaultKeymap, history, historyKeymap} from "@codemirror/commands";

  // plugin-kit.js is an ES MODULE (it has export statements) — a classic
  // <script src> throws "Unexpected token 'export'" and never sets the
  // window.protoPluginView global. Dynamic import is the no-build way to load it
  // with a slug-aware URL (a static import specifier can't carry the base).
  // If the kit can't load, FAIL LOUDLY. The old fallback here fetched tokenlessly,
  // which quietly 401s every save on a gated instance and looks like data loss; an
  // unthemed, unauthed editor is not a degraded editor, it's a broken one.
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) {
    st.textContent = "Editor unavailable — the console plugin kit failed to load";
    throw e;
  }

  // ── live preview ──────────────────────────────────────────────────────────────
  // What makes this "rendering" rather than syntax highlighting: the markdown chrome
  // (`##`, `**`, backticks, `[]()`) is REPLACED with nothing while your cursor is on
  // another line, so you read formatted text — then reappears the moment you enter the
  // line, so it stays editable as plain markdown. Nothing is ever rewritten on disk;
  // the document is always exactly the markdown you typed.
  const LINE_CLASS = {
    ATXHeading1:"cm-md-h1", ATXHeading2:"cm-md-h2", ATXHeading3:"cm-md-h3",
    ATXHeading4:"cm-md-h4", ATXHeading5:"cm-md-h5", ATXHeading6:"cm-md-h6",
    SetextHeading1:"cm-md-h1", SetextHeading2:"cm-md-h2",
    Blockquote:"cm-md-quote", FencedCode:"cm-md-fence", CodeBlock:"cm-md-fence",
  };
  const MARK_CLASS = {
    Emphasis:"cm-md-em", StrongEmphasis:"cm-md-strong", InlineCode:"cm-md-code",
    Strikethrough:"cm-md-strike", Link:"cm-md-link",
  };
  // NB: EmphasisMark covers BOTH * and ** — @lezer/markdown has no StrongEmphasisMark.
  // ListMark is deliberately absent: the bullet IS the rendering, so it stays.
  const HIDE = new Set(["HeaderMark","EmphasisMark","StrikethroughMark","LinkMark","URL","LinkTitle","CodeMark","QuoteMark"]);

  function hideable(node){
    const parent = node.node.parent && node.node.parent.name;
    // Inline code's backticks go; a fenced block's ``` stay — hiding them collapses the
    // fence to a blank line, which reads as a rendering bug.
    if (node.name === "CodeMark") return parent === "InlineCode";
    // A link's chrome goes, an image's stays: we don't draw images, so reducing
    // ![alt](url) to "alt" would look like prose that lost its picture.
    if (node.name === "LinkMark" || node.name === "URL" || node.name === "LinkTitle") return parent === "Link";
    return true;
  }

  function decorate(view){
    const state = view.state, decos = [], seen = new Set();
    // Every line touched by a cursor or selection stays in source form — but only while
    // we HAVE focus. An unfocused editor has a caret parked at position 0, so without
    // this the first line would sit there showing its raw `#` to someone who is just
    // reading the note. Not editing ⇒ fully rendered.
    const live = new Set();
    if (view.hasFocus){
      for (const r of state.selection.ranges){
        const a = state.doc.lineAt(r.from).number, b = state.doc.lineAt(r.to).number;
        for (let n = a; n <= b; n++) live.add(n);
      }
    }
    for (const range of view.visibleRanges){
      syntaxTree(state).iterate({from: range.from, to: range.to, enter(node){
        const lineClass = LINE_CLASS[node.name];
        if (lineClass){
          // Blockquotes and fences span lines; headings don't. Walk either way.
          let pos = node.from;
          for (;;){
            const line = state.doc.lineAt(pos), key = line.from + "|" + lineClass;
            if (!seen.has(key)){ seen.add(key); decos.push(Decoration.line({class: lineClass}).range(line.from)); }
            if (line.to >= node.to) break;
            pos = line.to + 1;
          }
        }
        const markClass = MARK_CLASS[node.name];
        if (markClass && node.to > node.from) decos.push(Decoration.mark({class: markClass}).range(node.from, node.to));
        if (HIDE.has(node.name) && !live.has(state.doc.lineAt(node.from).number) && hideable(node)){
          let to = node.to;
          // Swallow the space after `## ` / `> ` too, or the text keeps a phantom
          // indent once the marker is gone.
          if ((node.name === "HeaderMark" || node.name === "QuoteMark") && state.doc.sliceString(node.to, node.to + 1) === " ") to = node.to + 1;
          if (to > node.from) decos.push(Decoration.replace({}).range(node.from, to));
        }
      }});
    }
    // sort=true: line and replace decorations interleave, and RangeSet demands order.
    return Decoration.set(decos, true);
  }

  const livePreview = ViewPlugin.fromClass(class {
    constructor(view){ this.decorations = decorate(view); }
    update(u){
      // selectionSet matters as much as docChanged — moving the caret is what reveals
      // and re-hides the markers; focusChanged re-renders the line you left behind.
      if (u.docChanged || u.viewportChanged || u.selectionSet || u.focusChanged) this.decorations = decorate(u.view);
    }
  }, {decorations: v => v.decorations});

  // ── wiring ────────────────────────────────────────────────────────────────────
  var lastSynced="", baseVersion=null, remote=null, dirty=false, conflicted=false, loaded=false, t=null;
  // Marks transactions WE dispatch (adopting the agent's text), so the update listener
  // can tell them from the operator's typing and not flag the note dirty.
  const Remote = Annotation.define();

  const view = new EditorView({
    parent: document.getElementById("ed"),
    state: EditorState.create({doc: "", extensions: [
      history(),
      keymap.of([...defaultKeymap, ...historyKeymap]),
      EditorView.lineWrapping,
      markdown({base: markdownLanguage}),  // markdownLanguage = GFM (strikethrough, tasks)
      livePreview,
      placeholder("A shared note — you and the agent both write here."),
      EditorView.updateListener.of(function(u){
        if (!u.docChanged) return;
        if (u.transactions.some(function(tr){ return tr.annotation(Remote); })) return;
        dirty = true; st.textContent = "Saving…"; clearTimeout(t); t = setTimeout(save, 700);
      }),
    ]}),
  });
  view.focus();

  function docText(){ return view.state.doc.toString(); }
  function setDocText(text){
    view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: text}, annotations: Remote.of(true)});
  }

  // The kit owns the protoagent:init handshake (bearer + theme, incl. live re-themes)
  // and authed slug-aware fetches; on a token-gated instance the first token arrives
  // with the handshake, so re-load then (the immediate load() covers tokenless local).
  kit.initPluginView(function(){ if(!dirty) load(); });

  // A 409 means the note moved under us (the agent wrote while we were typing). Park
  // BOTH versions — ours on screen, theirs on disk — and let the operator pick.
  // Auto-resolving either way is data loss with extra steps.
  function onConflict(c){ conflicted=true; remote=c; clearTimeout(t); cf.hidden=false;
    st.textContent="⚠ The agent changed this note — your edits are unsaved"; }
  function clearConflict(){ conflicted=false; remote=null; cf.hidden=true; }
  bMine.onclick=function(){ baseVersion=remote?remote.version:null; clearConflict(); save(); };
  bTheirs.onclick=function(){ if(!remote)return; var text=remote.content; setDocText(text);
    lastSynced=text; baseVersion=remote.version; dirty=false; clearConflict(); st.textContent="Reloaded"; };

  async function save(){
    // Never save what we never loaded: if the initial GET failed we hold no
    // base_version, and saving would force-overwrite the real note with whatever is
    // in this (probably empty) editor.
    if(!loaded){ st.textContent="Not loaded — not saving"; return; }
    try{
      var body={content:docText()};
      // Omitting base_version force-overwrites; we send it whenever we know it, so a
      // save that would clobber an unseen agent write comes back 409 instead.
      if(baseVersion!==null) body.base_version=baseVersion;
      var r=await kit.apiFetch("/api/plugins/notes/note",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      if(r.status===409){ onConflict(await r.json()); return; }
      if(!r.ok)throw 0;
      var a=await r.json(); lastSynced=body.content; baseVersion=a.version; dirty=false; st.textContent="Saved ✓";
    }catch(e){ st.textContent="Save failed"; } }
  async function load(){ try{
      var a=await kit.apiFetch("/api/plugins/notes/note").then(function(r){return r.json();});
      if(typeof a.content==="string" && a.content!==lastSynced && !dirty){ setDocText(a.content); lastSynced=a.content; }
      if(!dirty && typeof a.version==="string") baseVersion=a.version;
      loaded=true;
    }catch(e){} }
  load();
  // Be a good desktop citizen: don't poll while the window is hidden/minimized; refresh on return.
  setInterval(function(){ if(!document.hidden && !dirty && !conflicted) load(); }, 4000);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden && !dirty && !conflicted) load(); });
</script></body></html>"""


# The compact PALETTE page (ADR 0057) — same shared note + autosave + adopt-the-agent's
# writes, but trimmed to a single textarea (no preview toggle / marked) so it fits the
# ⌘K palette body. Four-rules compliant like the full editor (slug-aware base, the DS
# kit owns theme + authed fetch).
_QUICK_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<script>
  "use strict";
  window.__base = location.pathname.split("/plugins/")[0];
  document.write('<link rel="stylesheet" href="' + window.__base + '/_ds/plugin-kit.css">');
</script>
<style>
  html,body{margin:0;height:100%;background:var(--pl-color-bg-raised);color:var(--pl-color-fg);
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif)}
  #wrap{display:flex;flex-direction:column;height:100%}
  #bar{padding:6px 10px;border-bottom:var(--pl-border-width,1px) solid var(--pl-color-border);
    font-size:11px;color:var(--pl-color-fg-muted)}
  #ed{flex:1;min-height:0;resize:none;border:0;outline:none;padding:10px 12px;background:transparent;
    color:var(--pl-color-fg);font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);
    font-size:13px;line-height:1.55}
</style></head><body>
<div id="wrap">
  <div id="bar"><span id="status">Quick note</span></div>
  <textarea id="ed" placeholder="Jot — saves to the shared note." spellcheck="false" autofocus></textarea>
</div>
<script type="module">
  "use strict";
  var ed=document.getElementById("ed"), st=document.getElementById("status");
  // Fail loudly rather than fall back to a tokenless fetch — see the rail editor.
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) { st.textContent = "Unavailable — plugin kit failed to load"; ed.disabled = true; throw e; }
  var lastSynced="", baseVersion=null, dirty=false, conflicted=false, loaded=false, t=null;
  kit.initPluginView(function(){ if(!dirty) load(); });
  ed.addEventListener("input", function(){ dirty=true; st.textContent="Saving…"; clearTimeout(t); t=setTimeout(save,600); });
  async function save(){
    // Never save what we never loaded — see the rail editor.
    if(!loaded){ st.textContent="Not loaded — not saving"; return; }
    try{
      var body={content:ed.value};
      if(baseVersion!==null) body.base_version=baseVersion;
      var r=await kit.apiFetch("/api/plugins/notes/note",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      // The palette has no room for a resolve UI, so it fails SAFE: keep the operator's
      // text on screen, leave the agent's version on disk, and point at the rail editor
      // (which has Keep mine / Take theirs) rather than silently picking a winner.
      if(r.status===409){ conflicted=true; clearTimeout(t);
        st.textContent="⚠ Changed by the agent — open Notes to resolve"; return; }
      if(!r.ok)throw 0;
      var a=await r.json(); lastSynced=ed.value; baseVersion=a.version; dirty=false; st.textContent="Saved ✓";
    }catch(e){ st.textContent="Save failed"; } }
  async function load(){ try{
      var a=await kit.apiFetch("/api/plugins/notes/note").then(function(r){return r.json();});
      if(typeof a.content==="string" && a.content!==lastSynced && !dirty){ ed.value=a.content; lastSynced=a.content; }
      if(!dirty && typeof a.version==="string") baseVersion=a.version;
      loaded=true;
    }catch(e){} }
  load();
  setInterval(function(){ if(!document.hidden && !dirty && !conflicted) load(); }, 4000);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden && !dirty && !conflicted) load(); });
</script></body></html>"""
