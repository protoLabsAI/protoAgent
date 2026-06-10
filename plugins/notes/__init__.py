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


# The editor page the console iframes (ADR 0026 bridge → bearer + theme). A self-contained markdown
# editor: debounced autosave (PUT /note), an edit↔preview toggle (marked from CDN), and a poll that
# adopts the agent's writes. Dark by default, following the console theme. No host build — the
# plugin serves its own UI, so it works installed from a git URL (ADR 0038).
#
# FLEET-PROXY-SAFE FETCH (ADR 0042): the iframe (the PUBLIC page) loads at /plugins/notes/view on the
# host window, but at /agents/<slug>/plugins/notes/view when this agent is viewed through the fleet
# proxy. So it derives `base` from its own path (= "" on host, "/agents/<slug>" when proxied) and
# prefixes EVERY data fetch — never hardcode an absolute "/api/..." or the proxied window would hit
# the host agent. The data path stays the GATED /api/plugins/notes/note, fetched with the token.
_EDITOR_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
  :root{ --bg:#0a0a0c; --bg-raised:#161616; --fg:#ededed; --fg-muted:#9aa0aa; --border:#2a2a30; --brand:#a78bfa; }
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
    font-family:ui-sans-serif,system-ui,-apple-system,sans-serif}
  #wrap{display:flex;flex-direction:column;height:100%}
  #bar{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;border-bottom:1px solid var(--border);font-size:12px;color:var(--fg-muted)}
  #bar button{background:transparent;border:1px solid var(--border);color:var(--fg-muted);border-radius:6px;padding:3px 10px;cursor:pointer;font-size:12px}
  #ed{flex:1;min-height:0;resize:none;border:0;outline:none;padding:12px;background:transparent;color:var(--fg);
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;line-height:1.6}
  #pv{flex:1;min-height:0;overflow:auto;padding:12px;display:none}
  #pv :is(h1,h2,h3){color:var(--fg)} #pv code{background:var(--bg-raised);padding:2px 5px;border-radius:5px}
</style></head><body>
<div id="wrap">
  <div id="bar"><span id="status">Notes</span><button id="toggle" type="button">Preview</button></div>
  <textarea id="ed" placeholder="A shared note — you and the agent both write here." spellcheck="false"></textarea>
  <div id="pv"></div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script>
  var token=null, lastSynced="", dirty=false, preview=false, t=null;
  var ed=document.getElementById("ed"), pv=document.getElementById("pv"), st=document.getElementById("status"), tg=document.getElementById("toggle");
  // Slug-aware base (ADR 0042): the iframe (PUBLIC page) lives at /plugins/notes/view on the host
  // window or /agents/<slug>/plugins/notes/view when proxied. base = "" (host) or "/agents/<slug>"
  // (proxied); prefix EVERY data fetch so the proxied window talks to ITS agent, never the host's.
  var base = location.pathname.split("/plugins/")[0];
  window.addEventListener("message", function(e){
    var m=e.data||{}; if(m.type!=="protoagent:init") return; token=m.token||null;
    if(m.theme){ var r=document.documentElement.style;
      if(m.theme.bg)r.setProperty("--bg",m.theme.bg); if(m.theme.fg)r.setProperty("--fg",m.theme.fg);
      if(m.theme.fgMuted)r.setProperty("--fg-muted",m.theme.fgMuted); if(m.theme.border)r.setProperty("--border",m.theme.border); }
  });
  function hdr(extra){ var h=extra||{}; if(token)h["Authorization"]="Bearer "+token; return h; }
  function renderPreview(){ pv.innerHTML = window.marked ? marked.parse(ed.value||"") : ed.value; }
  tg.onclick=function(){ preview=!preview; if(preview){renderPreview();pv.style.display="block";ed.style.display="none";tg.textContent="Edit";}
    else{pv.style.display="none";ed.style.display="block";tg.textContent="Preview";} };
  ed.addEventListener("input", function(){ dirty=true; st.textContent="Saving…"; clearTimeout(t); t=setTimeout(save,700); });
  async function save(){ try{
      var r=await fetch(base+"/api/plugins/notes/note",{method:"PUT",headers:hdr({"Content-Type":"application/json"}),body:JSON.stringify({content:ed.value})});
      if(!r.ok)throw 0; lastSynced=ed.value; dirty=false; st.textContent="Saved ✓";
    }catch(e){ st.textContent="Save failed"; } }
  async function load(){ try{
      var a=await fetch(base+"/api/plugins/notes/note",{headers:hdr()}).then(function(r){return r.json();});
      if(typeof a.content==="string" && a.content!==lastSynced && !dirty){ ed.value=a.content; lastSynced=a.content; if(preview)renderPreview(); }
    }catch(e){} }
  load();
  // Be a good desktop citizen: don't poll while the window is hidden/minimized; refresh on return.
  setInterval(function(){ if(!document.hidden && !dirty) load(); }, 4000);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden && !dirty) load(); });
</script></body></html>"""
