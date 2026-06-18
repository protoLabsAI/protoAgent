"""Notes plugin (ADR 0034 S4) — the flagship reference plugin.

A single shared markdown note that BOTH the agent (via tools) and the operator
(via a console panel) read and write. The plugin owns its whole vertical: storage,
agent tools, and a sandboxed-iframe editor it serves itself (ADR 0038 — no host
build, git-installable). No tabs, no undo, no versioning — deliberately the basic
notebook we actually want.

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

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.notes")


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
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/view")
    async def _view():
        return HTMLResponse(_EDITOR_HTML)

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

    router = APIRouter()

    @router.get("/note")
    async def _get() -> dict:
        return {"content": _read(), "updated_at": _updated_at()}

    @router.put("/note")
    async def _put(body: dict = Body(...)) -> dict:
        _write(str(body.get("content", "")))
        return {"ok": True, "updated_at": _updated_at()}

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


# The editor page the console iframes. A self-contained markdown editor: debounced
# autosave (PUT /note), an edit↔preview toggle (marked from CDN, degrades to raw text
# offline), and a poll that adopts the agent's writes. No host build — the plugin
# serves its own UI, so it works installed from a git URL (ADR 0038).
#
# FOUR-RULES COMPLIANT (docs/how-to/build-a-plugin-view.md, the chat_example pattern):
#   3. SLUG-AWARE — the kit's apiFetch derives the base (host vs /agents/<slug> proxy)
#      for every data call; only the kit's own <link>/<script> are base-prefixed by
#      hand (they load before the kit exists).
#   4. LINK THE KIT — <base>/_ds/plugin-kit.{css,js} replaces the hand-rolled hex
#      token map + bespoke protoagent:init listener + manual bearer headers this page
#      carried before: plugin-kit.js maps the operator's live theme onto --pl-*
#      (initial handshake AND protoagent:theme re-themes), and apiFetch attaches the
#      token. Local <style> is layout-only.
_EDITOR_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<script>
  "use strict";
  // Slug-aware base (ADR 0042), computed FIRST: the kit's own assets load before the
  // kit exists, so they're the one thing prefixed by hand (rule 4).
  window.__base = location.pathname.split("/plugins/")[0];
  document.write('<link rel="stylesheet" href="' + window.__base + '/_ds/plugin-kit.css">');
</script>
<style>
  /* Layout only — colors/typography come from plugin-kit.css's --pl-* tokens, which
     plugin-kit.js re-skins to the operator's live theme on the handshake. */
  html,body{margin:0;height:100%;background:var(--pl-color-bg);color:var(--pl-color-fg);
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif)}
  #wrap{display:flex;flex-direction:column;height:100%}
  #bar{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;
    border-bottom:var(--pl-border-width,1px) solid var(--pl-color-border);
    font-size:12px;color:var(--pl-color-fg-muted)}
  #ed{flex:1;min-height:0;resize:none;border:0;outline:none;padding:12px;background:transparent;
    color:var(--pl-color-fg);font-family:var(--pl-font-mono,ui-monospace,Menlo,monospace);
    font-size:13px;line-height:1.6}
  #pv{flex:1;min-height:0;overflow:auto;padding:12px;display:none}
  #pv :is(h1,h2,h3){color:var(--pl-color-fg)}
  #pv code{background:var(--pl-color-bg-raised);padding:2px 5px;border-radius:var(--pl-radius)}
</style></head><body>
<div id="wrap">
  <div id="bar"><span id="status">Notes</span>
    <button id="toggle" class="pl-btn pl-btn--sm" type="button">Preview</button></div>
  <textarea id="ed" placeholder="A shared note — you and the agent both write here." spellcheck="false"></textarea>
  <div id="pv"></div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script type="module">
  "use strict";
  // plugin-kit.js is an ES MODULE (it has export statements) — a classic
  // <script src> throws "Unexpected token 'export'" and never sets the
  // window.protoPluginView global. Dynamic import is the no-build way to load it
  // with a slug-aware URL (a static import specifier can't carry the base).
  // Older host with no /_ds route: fall back to a tokenless same-origin shim
  // (fine locally; gated instances always serve the kit).
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) { kit = { initPluginView(){}, apiFetch: (p, i) => fetch(window.__base + p, i) }; }
  var lastSynced="", dirty=false, preview=false, t=null;
  var ed=document.getElementById("ed"), pv=document.getElementById("pv"), st=document.getElementById("status"), tg=document.getElementById("toggle");
  // The kit owns the protoagent:init handshake (bearer + theme, incl. live re-themes)
  // and authed slug-aware fetches; on a token-gated instance the first token arrives
  // with the handshake, so re-load then (the immediate load() covers tokenless local).
  kit.initPluginView(function(){ if(!dirty) load(); });
  function renderPreview(){ pv.innerHTML = window.marked ? marked.parse(ed.value||"") : ed.value; }
  tg.onclick=function(){ preview=!preview; if(preview){renderPreview();pv.style.display="block";ed.style.display="none";tg.textContent="Edit";}
    else{pv.style.display="none";ed.style.display="block";tg.textContent="Preview";} };
  ed.addEventListener("input", function(){ dirty=true; st.textContent="Saving…"; clearTimeout(t); t=setTimeout(save,700); });
  async function save(){ try{
      var r=await kit.apiFetch("/api/plugins/notes/note",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({content:ed.value})});
      if(!r.ok)throw 0; lastSynced=ed.value; dirty=false; st.textContent="Saved ✓";
    }catch(e){ st.textContent="Save failed"; } }
  async function load(){ try{
      var a=await kit.apiFetch("/api/plugins/notes/note").then(function(r){return r.json();});
      if(typeof a.content==="string" && a.content!==lastSynced && !dirty){ ed.value=a.content; lastSynced=a.content; if(preview)renderPreview(); }
    }catch(e){} }
  load();
  // Be a good desktop citizen: don't poll while the window is hidden/minimized; refresh on return.
  setInterval(function(){ if(!document.hidden && !dirty) load(); }, 4000);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden && !dirty) load(); });
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
  html,body{margin:0;height:100%;background:var(--pl-color-bg);color:var(--pl-color-fg);
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
  <textarea id="ed" placeholder="Jot — saves to the shared note." spellcheck="false"></textarea>
</div>
<script type="module">
  "use strict";
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) { kit = { initPluginView(){}, apiFetch:(p,i)=>fetch(window.__base+p,i) }; }
  var lastSynced="", dirty=false, t=null;
  var ed=document.getElementById("ed"), st=document.getElementById("status");
  kit.initPluginView(function(){ if(!dirty) load(); });
  ed.addEventListener("input", function(){ dirty=true; st.textContent="Saving…"; clearTimeout(t); t=setTimeout(save,600); });
  async function save(){ try{
      var r=await kit.apiFetch("/api/plugins/notes/note",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({content:ed.value})});
      if(!r.ok)throw 0; lastSynced=ed.value; dirty=false; st.textContent="Saved ✓";
    }catch(e){ st.textContent="Save failed"; } }
  async function load(){ try{
      var a=await kit.apiFetch("/api/plugins/notes/note").then(function(r){return r.json();});
      if(typeof a.content==="string" && a.content!==lastSynced && !dirty){ ed.value=a.content; lastSynced=a.content; }
    }catch(e){} }
  load();
  setInterval(function(){ if(!document.hidden && !dirty) load(); }, 4000);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden && !dirty) load(); });
</script></body></html>"""
