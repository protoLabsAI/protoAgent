"""Docs plugin — let the agent answer questions about protoAgent from its own docs.

Ships a keyword FTS index over the bundled `docs/` tree (built in memory at load) plus two
tools — `docs_search` (find the right page) and `docs_read` (read its markdown) — and a
SKILL.md (`skills/answering-docs.md`, auto-discovered) that teaches search → read → cite.
First-party, `enabled: true`. A console Docs reader view + ⌘K search come in a follow-up.

No knowledge-store coupling and no embeddings: the index is self-contained and offline, so
docs Q&A works in the frozen desktop app and never pollutes the operator's knowledge store.
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.tools import tool

from .corpus import grouped_tree, read_doc
from .docs_index import DocsIndex
from .render import render_markdown

log = logging.getLogger("protoagent.plugins.docs")

_INDEX: DocsIndex | None = None


def _index() -> DocsIndex:
    """The process-wide docs index, built lazily on first use."""
    global _INDEX
    if _INDEX is None:
        idx = DocsIndex()
        try:
            n = idx.seed()
            log.info("[docs] indexed %d doc(s)", n)
        except Exception as exc:  # noqa: BLE001 — never let a bad corpus break the tools
            log.warning("[docs] index seed failed: %s", exc)
        _INDEX = idx
    return _INDEX


@tool
async def docs_search(query: str, k: int = 5) -> str:
    """Search the protoAgent project documentation for pages matching ``query``.

    Use this FIRST whenever the user asks how protoAgent works, or about a specific
    feature, configuration option, tool, plugin, API, or design decision (ADR) — anything
    answerable from the docs. Returns the top matches as ``[section] Title — path`` lines;
    then call ``docs_read(path)`` on the best one or two.
    """
    k = max(1, min(int(k), 10))
    # The index is defensive (serialized + degrades to [] on any DB error), but never let an
    # unexpected failure surface to the agent as a raw traceback / empty tool result — tell it
    # the search errored so it can retry or fall back, rather than silently "nothing came back".
    try:
        results = await asyncio.to_thread(_index().search, query, k)
    except Exception as exc:  # noqa: BLE001
        log.warning("[docs] docs_search failed: %s", exc)
        return f"Docs search failed ({type(exc).__name__}). Try again, or rephrase/narrow the query."
    if not results:
        return "No matching docs."
    return "\n".join(f"[{r.section}] {r.title} — {r.path}" for r in results)


@tool
async def docs_read(path: str) -> str:
    """Read the full markdown of a protoAgent doc by its ``path`` (e.g. ``guides/skills.md``,
    as returned by ``docs_search``). Answer from what you read and **cite the path**."""
    if not _index().has(path):
        return f"No such doc: {path!r}. Use docs_search to find the right path."
    text = await asyncio.to_thread(read_doc, path)
    return text or f"Could not read {path!r}."


def _title_from_md(md: str, fallback: str) -> str:
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return fallback


def _build_view_router():
    """The reader PAGE — served UNGATED under ``/plugins/docs`` (an iframe page-load can't
    carry a bearer). One mode-adaptive page: full tree+reader on the rail, search-first in
    the ⌘K palette (``?mode=search``). It fetches its data from the gated router below."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/view")
    async def _view():
        return HTMLResponse(_VIEW_HTML)

    return router


def _build_data_router():
    """The Docs DATA routes — GATED under ``/api/plugins/docs`` (fetched with the handshake
    token). ``/doc`` renders markdown → HTML server-side (offline; no JS markdown bundle)."""
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.get("/tree")
    async def _tree() -> dict:
        return {"sections": grouped_tree()}

    @router.get("/search")
    async def _search(q: str = "") -> dict:
        rows = _index().search(q, k=20)
        return {"results": [{"path": r.path, "title": r.title, "section": r.section, "preview": r.preview} for r in rows]}

    @router.get("/doc")
    async def _doc(path: str = ""):
        if not _index().has(path):
            return JSONResponse({"error": "not found"}, status_code=404)
        md = read_doc(path)
        if md is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"path": path, "title": _title_from_md(md, path), "html": render_markdown(md)}

    return router


def register(registry) -> None:
    """Entry point — build the index (so the first turn is fast), expose the tools, and
    serve the reader view (page ungated, data gated). ``skills/`` is auto-discovered."""
    try:
        _index()
    except Exception as exc:  # noqa: BLE001 — plugin load must never fail on this
        log.warning("[docs] index build at load failed: %s", exc)
    registry.register_tools([docs_search, docs_read])
    registry.register_router(_build_view_router(), prefix="/plugins/docs")
    registry.register_router(_build_data_router(), prefix="/api/plugins/docs")


# The reader page the console iframes (rail "Docs" + the ⌘K "Docs" palette morph). One
# mode-adaptive page (full tree+reader; `?mode=search` = search-first for the palette).
# FOUR-RULES COMPLIANT (docs/guides/building-react-plugin-views.md): base-prefixed kit
# css+js loaded slug-aware (rule 4), gated data via the kit's authed apiFetch (rules 2+3).
# Markdown is rendered SERVER-SIDE (/doc returns HTML) — no CDN, offline/frozen-safe.
_VIEW_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<script>window.__base = location.pathname.split("/plugins/")[0];
document.write('<link rel="stylesheet" href="'+window.__base+'/_ds/plugin-kit.css">');</script>
<style>
  *{box-sizing:border-box} html,body{margin:0;height:100%;color:var(--pl-color-fg);background:var(--pl-color-bg-raised);font:14px/1.55 system-ui,-apple-system,sans-serif}
  #app{display:flex;height:100vh}
  #nav{width:280px;flex:none;border-right:1px solid var(--pl-color-border);overflow:auto;padding:8px}
  #reader{flex:1;overflow:auto;padding:16px 26px}
  .q{width:100%;padding:6px 8px;border:1px solid var(--pl-color-border);border-radius:6px;background:var(--pl-color-bg-inset);color:inherit;margin-bottom:8px;font:inherit}
  .sec{font-weight:600;font-size:11px;letter-spacing:.04em;text-transform:uppercase;opacity:.6;margin:12px 4px 4px}
  .grp{font-weight:600;font-size:12px;opacity:.8;margin:7px 4px 2px}
  .item{display:block;padding:3px 8px 3px 14px;border-radius:4px;cursor:pointer;color:inherit;text-decoration:none;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .item:hover,.item.active{background:var(--pl-color-bg-inset)}
  .res{padding:6px 8px;border-radius:6px;cursor:pointer} .res:hover{background:var(--pl-color-bg-inset)}
  .res .t{font-weight:600} .res .p{font-size:12px;opacity:.65}
  .empty{opacity:.6;padding:24px}
  .md{max-width:760px} .md h1{font-size:1.6em} .md h2{font-size:1.3em;border-bottom:1px solid var(--pl-color-border);padding-bottom:.2em} .md h3{font-size:1.1em}
  .md code{background:var(--pl-color-bg-inset);padding:1px 5px;border-radius:4px;font-size:.9em}
  .md pre{background:var(--pl-color-bg-inset);padding:12px;border-radius:8px;overflow:auto} .md pre code{background:none;padding:0}
  .md table{border-collapse:collapse;margin:8px 0} .md th,.md td{border:1px solid var(--pl-color-border);padding:5px 9px;text-align:left}
  .md a{color:var(--pl-color-accent,#818cf8)} .md blockquote{border-left:3px solid var(--pl-color-border);margin:8px 0;padding:2px 12px;opacity:.85}
  body.mode-search #app{flex-direction:column} body.mode-search #nav{width:auto;border-right:none;border-bottom:1px solid var(--pl-color-border);max-height:48%}
</style></head>
<body>
<div id="app">
  <aside id="nav"><input id="q" class="q" placeholder="Search docs…" autofocus><div id="list"></div></aside>
  <main id="reader"><div class="empty">Select a doc from the list.</div></main>
</div>
<script type="module">
const base = window.__base;
let kit; try { kit = await import(base+"/_ds/plugin-kit.js"); } catch(e){ kit = { initPluginView(){}, apiFetch:(p,i)=>fetch(base+p,i) }; }
kit.initPluginView(()=>{});
if (new URLSearchParams(location.search).get("mode")==="search") document.body.classList.add("mode-search");
const $list=document.getElementById("list"), $reader=document.getElementById("reader"), $q=document.getElementById("q");
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function api(p){ try{ const r=await kit.apiFetch("/api/plugins/docs"+p); return r.ok?await r.json():null; }catch(e){ return null; } }
const LIVE_DOCS="https://agent.protolabs.studio/docs/";  // Cloudflare build folds the docs in here (config.mts)
const slugify=s=>String(s).normalize("NFKD").replace(/[\u0300-\u036f]/g,"").replace(/[^\w\- ]+/g,"").trim().replace(/\s+/g,"-").toLowerCase();
let currentPath=null;
// Resolve a doc link — relative (`./x.md`, `../sec/y.md`) or VitePress abs-rooted (`/sec/z`,
// `/sec/`) — against the current doc's dir into ORDERED corpus rel-path candidates to try.
// Clean links (no .md) may map to `z.md` OR a section index `z/index.md`; a trailing slash
// is always a section index. Returns [] for an unresolvable link (→ live-docs fallback).
function candidatePaths(fromPath, rawPath){
  let parts;
  if(rawPath.startsWith("/")) parts=[];
  else { const dir=fromPath.includes("/")?fromPath.slice(0,fromPath.lastIndexOf("/")):""; parts=dir?dir.split("/"):[]; }
  const trailingSlash=/\/$/.test(rawPath);
  for(const seg of rawPath.split("/")){ if(seg===""||seg===".")continue; if(seg==="..")parts.pop(); else parts.push(seg); }
  const joined=parts.join("/");
  if(!joined) return [];
  if(trailingSlash) return [joined+"/index.md"];
  if(/\.md$/i.test(joined)) return [joined];
  return [joined+".md", joined+"/index.md"];
}
function liveDocsUrl(fromPath, rawPath, anchor){
  const c=candidatePaths(fromPath, rawPath);
  const p=(c[0]||rawPath.replace(/^\/+/,"")).replace(/\/index\.md$/i,"/").replace(/\.md$/i,"");
  return LIVE_DOCS+p+(anchor?("#"+anchor):"");
}
function addHeadingIds(){ $reader.querySelectorAll(".md h1,.md h2,.md h3,.md h4,.md h5,.md h6").forEach(h=>{ if(!h.id) h.id=slugify(h.textContent||""); }); }
function scrollToAnchor(anchor){ if(!anchor){ $reader.scrollTop=0; return; } const a=decodeURIComponent(anchor); const el=document.getElementById(a)||document.getElementById(slugify(a)); if(el)el.scrollIntoView({block:"start"}); else $reader.scrollTop=0; }
async function fetchDoc(path){ return api("/doc?path="+encodeURIComponent(path)); }
function renderDoc(path, d, anchor){
  currentPath=path;
  $reader.innerHTML='<article class="md">'+d.html+'</article>';
  addHeadingIds();
  [...document.querySelectorAll(".item")].forEach(a=>a.classList.toggle("active", a.dataset.path===path));
  scrollToAnchor(anchor);
}
async function openDoc(path){ const d=await fetchDoc(path); if(d) renderDoc(path,d); else $reader.innerHTML='<div class="empty">Could not load.</div>'; }
// In-content cross-reference links: don't let the click reload the target INSIDE this iframe
// (cramped, loses the docs chrome). Resolve to a corpus doc → open in-panel (preferred);
// else open the live docs site in a new tab (fallback). In-page anchors scroll the reader;
// external/scheme links open a new tab (browser: a tab; desktop: the system browser, #1450).
$reader.addEventListener("click", async (e)=>{
  const a=e.target.closest("a[href]"); if(!a||!$reader.contains(a)) return;
  const href=a.getAttribute("href")||""; if(!href) return;
  if(href.startsWith("#")){ e.preventDefault(); scrollToAnchor(href.slice(1)); return; }
  if(/^[a-z][\w+.-]*:/i.test(href)||href.startsWith("//")){ e.preventDefault(); window.open(href,"_blank","noopener,noreferrer"); return; }
  e.preventDefault();
  const hi=href.indexOf("#"); const rawPath=hi>=0?href.slice(0,hi):href; const anchor=hi>=0?href.slice(hi+1):"";
  if(!rawPath){ scrollToAnchor(anchor); return; }
  for(const c of candidatePaths(currentPath||"", rawPath)){ const d=await fetchDoc(c); if(d){ renderDoc(c,d,anchor); return; } }
  window.open(liveDocsUrl(currentPath||"", rawPath, anchor), "_blank", "noopener,noreferrer");
});
function renderTree(sections){
  const item=it=>'<a class="item" data-path="'+esc(it.path)+'" title="'+esc(it.path)+'">'+esc(it.title)+'</a>';
  $list.innerHTML = sections.map(s=>'<div class="sec">'+esc(s.label)+'</div>'+
    s.groups.map(g=>(g.label?'<div class="grp">'+esc(g.label)+'</div>':'')+g.items.map(item).join("")).join("")
  ).join("") || '<div class="empty">No docs.</div>';
  [...$list.querySelectorAll(".item")].forEach(a=>a.onclick=()=>openDoc(a.dataset.path));
}
function renderResults(rows){
  $list.innerHTML = rows.length ? rows.map(r=>'<div class="res" data-path="'+esc(r.path)+'"><div class="t">'+esc(r.title)+'</div><div class="p">'+esc(r.section)+' · '+esc(r.path)+'</div></div>').join("") : '<div class="empty">No matches.</div>';
  [...$list.querySelectorAll(".res")].forEach(d=>d.onclick=()=>openDoc(d.dataset.path));
}
let tree=null;
async function showTree(){ if(!tree){ const t=await api("/tree"); tree=(t&&t.sections)||[]; } renderTree(tree); }
let timer;
$q.oninput=()=>{ clearTimeout(timer); timer=setTimeout(async()=>{ const q=$q.value.trim(); if(!q){ showTree(); return; } const r=await api("/search?q="+encodeURIComponent(q)); renderResults((r&&r.results)||[]); }, 180); };
showTree();
</script></body></html>
"""
