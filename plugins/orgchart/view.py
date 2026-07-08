"""orgChart console view — a live fleet delegation diagram.

The PAGE (self-contained HTML + inline SVG, no build step) is served on the PUBLIC
``/plugins/orgchart`` prefix; the TOPOLOGY JSON it renders is served on the GATED
``/api/plugins/orgchart`` prefix (operator bearer, attached by the DS kit's apiFetch).

Topology is assembled by a server-side crawl (``_crawl``): read this agent's own
delegates from config (raw — so we keep ``credentialsEnv`` and can resolve each peer's
token from ``os.environ``), then for every A2A peer we hold a token for, fetch its
public agent-card + ``/healthz`` (identity + liveness) and its ``/api/delegates`` (its
own outbound edges). Peers we can't authenticate to still appear — as leaf nodes with
identity from the public card — we just can't see their outbound edges. Tokens are
resolved and used entirely server-side; they never reach the browser.
"""

from __future__ import annotations

# ── the page (self-contained; DS kit loaded same-origin at runtime) ─────────────────
VIEW_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Org Chart</title>
<style>
  html,body{margin:0;height:100%;background:var(--pl-color-bg,#0b0d10);color:var(--pl-color-fg,#e6e6e6);
    font-family:var(--pl-font-sans,system-ui,sans-serif);font-size:13px}
  .wrap{max-width:1400px;margin:0 auto;padding:var(--pl-space-4,16px) var(--pl-space-6,24px)}
  header{display:flex;align-items:baseline;gap:var(--pl-space-4,16px);margin-bottom:10px}
  header h1{font-size:18px;margin:0;font-weight:600}
  .sub{color:var(--pl-color-fg-muted,#8a8f98);font-size:12px}
  .sub b{color:var(--pl-color-fg,#e6e6e6);font-weight:600}
  .err{color:var(--pl-color-danger,#f85149);font-size:12px;margin:8px 0}
  .legend{display:flex;gap:16px;font-size:11px;color:var(--pl-color-fg-muted,#8a8f98);margin:6px 2px 12px}
  .legend i{display:inline-block;width:9px;height:9px;border-radius:50%;vertical-align:middle;margin-right:5px}
  .gwrap{overflow:auto;border:1px solid var(--pl-color-border,#272b33);border-radius:var(--pl-radius-md,8px);
    background:var(--pl-color-bg-subtle,#15181d);padding:10px}
  .empty{color:var(--pl-color-fg-muted,#8a8f98);padding:48px 0;text-align:center}
  .empty code{font-family:var(--pl-font-mono,ui-monospace,monospace);color:var(--pl-color-fg,#e6e6e6)}
  text{user-select:none}
</style>
<script>
  var BASE = location.pathname.split("/plugins/")[0];   // slug-aware (fleet proxy)
  (function(){ var l=document.createElement("link"); l.rel="stylesheet";
    l.href=BASE+"/_ds/plugin-kit.css"; document.head.appendChild(l); })();
</script>
</head><body>
<div class="wrap">
  <header><h1>Org Chart</h1><div class="sub" id="summary"></div></header>
  <div class="legend">
    <span><i style="background:var(--pl-color-accent,#6cb6ff)"></i>this agent</span>
    <span><i style="background:var(--pl-color-success,#3fb950)"></i>up</span>
    <span><i style="background:var(--pl-color-danger,#f85149)"></i>down / unreachable</span>
    <span>· arrow = can delegate to</span>
  </div>
  <div id="err" class="err" hidden></div>
  <div id="graph" class="gwrap"></div>
</div>
<script type="module">
let kit;
try { kit = await import(BASE + "/_ds/plugin-kit.js"); }
catch (e) { kit = { initPluginView(){}, apiFetch: (p, i) => fetch(BASE + p, i) }; }
const api = async (p, init) => {
  const r = await kit.apiFetch(p, init);
  const d = await r.json().catch(() => { throw new Error("HTTP " + r.status + " (non-JSON response)"); });
  if (!r.ok) throw new Error((d && (d.detail || d.error)) || "HTTP " + r.status);
  return d;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// ── layered SVG graph (BFS distance from self → cycle-safe) ─────────────────────────
function draw(topo){
  const nodes = topo.nodes || [], edges = topo.edges || [], self = topo.self;
  if (!nodes.length) return `<div class="empty">No delegates configured. Add A2A delegates and they'll appear here.</div>`;
  const byId = new Map(nodes.map(n => [n.id, n]));
  // BFS layer from self; nodes not reachable via edges land one column past the deepest.
  const layer = new Map(); if (byId.has(self)) layer.set(self, 0);
  let frontier = byId.has(self) ? [self] : [];
  while (frontier.length){
    const next = [];
    for (const u of frontier) for (const e of edges)
      if (e.from === u && byId.has(e.to) && !layer.has(e.to)){ layer.set(e.to, layer.get(u)+1); next.push(e.to); }
    frontier = next;
  }
  let maxL = 0; layer.forEach(v => { if (v > maxL) maxL = v; });
  nodes.forEach(n => { if (!layer.has(n.id)) layer.set(n.id, maxL+1); });
  maxL = 0; layer.forEach(v => { if (v > maxL) maxL = v; });
  // columns by layer, stacked vertically
  const cols = {};
  nodes.forEach(n => { const L = layer.get(n.id); (cols[L] = cols[L] || []).push(n); });
  const NW=180, NH=48, COLW=248, RH=70, PX=18, PY=18;
  let maxRows = 0;
  Object.keys(cols).forEach(L => {
    maxRows = Math.max(maxRows, cols[L].length);
    cols[L].forEach((n,i) => { n._x = PX + Number(L)*COLW; n._y = PY + i*RH; });
  });
  const pos = new Map(nodes.map(n => [n.id, n]));
  const W = PX*2 + maxL*COLW + NW, H = Math.max(PY*2 + maxRows*RH, 120);
  // edges: owner right-edge → target left-edge, bezier + arrowhead (handles back-edges too)
  const paths = edges.map(e => {
    const a = pos.get(e.from), b = pos.get(e.to); if (!a || !b) return "";
    const x1=a._x+NW, y1=a._y+NH/2, x2=b._x-3, y2=b._y+NH/2, mx=(x1+x2)/2;
    const back = b._x <= a._x;   // cycle / same-column edge → dim it
    return `<path d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" fill="none"
      stroke="var(--pl-color-fg-muted,#8a8f98)" stroke-width="1.4" stroke-opacity="${back?0.35:0.8}"
      marker-end="url(#arw)"/>`;
  }).join("");
  const rects = nodes.map(n => {
    const up = !!n.up, self0 = n.id === self;
    const stroke = self0 ? "var(--pl-color-accent,#6cb6ff)" : (up ? "var(--pl-color-border,#272b33)" : "var(--pl-color-danger,#f85149)");
    const sw = self0 ? 2 : 1;
    const dot = self0 ? "var(--pl-color-accent,#6cb6ff)" : (up ? "var(--pl-color-success,#3fb950)" : "var(--pl-color-danger,#f85149)");
    const op = up || self0 ? 1 : 0.6;
    const ver = n.version ? ` v${esc(n.version)}` : "";
    return `<g opacity="${op}">
      <rect x="${n._x}" y="${n._y}" width="${NW}" height="${NH}" rx="8"
        fill="var(--pl-color-bg,#0b0d10)" stroke="${stroke}" stroke-width="${sw}"/>
      <circle cx="${n._x+13}" cy="${n._y+16}" r="4" fill="${dot}"/>
      <text x="${n._x+24}" y="${n._y+19} " font-size="12.5" font-weight="600" fill="var(--pl-color-fg,#e6e6e6)">${esc(clip(n.name,20))}</text>
      <text x="${n._x+11}" y="${n._y+37}" font-size="10" fill="var(--pl-color-fg-muted,#8a8f98)">${esc(clip(n.role,30))}${ver}</text>
    </g>`;
  }).join("");
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
    <defs><marker id="arw" markerWidth="9" markerHeight="9" refX="6.5" refY="3" orient="auto">
      <path d="M0,0 L6,3 L0,6 Z" fill="var(--pl-color-fg-muted,#8a8f98)"/></marker></defs>
    ${paths}${rects}</svg>`;
}
function clip(s, n){ s = String(s || ""); return s.length > n ? s.slice(0, n-1) + "…" : s; }

async function load(){
  try {
    document.getElementById("err").hidden = true;
    const topo = await api("/api/plugins/orgchart/topology");
    const nodes = topo.nodes || [], edges = topo.edges || [];
    const up = nodes.filter(n => n.up).length;
    document.getElementById("summary").innerHTML =
      `<b>${nodes.length}</b> agent${nodes.length===1?"":"s"} · <b>${up}</b> up · <b>${edges.length}</b> delegation edge${edges.length===1?"":"s"}`;
    document.getElementById("graph").innerHTML = draw(topo);
  } catch(e){
    document.getElementById("err").hidden = false;
    document.getElementById("err").textContent = "Could not load topology: " + e;
  }
}

let booted = false;
function boot(){ if (booted) return; booted = true; load(); setInterval(load, 8000); }
kit.initPluginView(boot);
setTimeout(boot, 800);
</script></body></html>"""


def build_view_router():
    """The PUBLIC page router (mounted at /plugins/orgchart)."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/view")
    async def _view() -> HTMLResponse:  # served at /plugins/orgchart/view
        return HTMLResponse(VIEW_PAGE)

    return router


def build_data_router():
    """The GATED data router (mounted at /api/plugins/orgchart). One endpoint: the
    crawled fleet topology. Bearer-gated like all /api routes."""
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/topology")
    async def _topology() -> dict:
        return await _crawl()

    return router


# ── the crawl ───────────────────────────────────────────────────────────────────────
def _norm(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if u.endswith("/a2a"):
        u = u[:-4]
    return u.rstrip("/")


def _short(s: str, cap: int = 34) -> str:
    """A compact role label from a card/config description: strip a leading "name — "
    label, keep the first clause, cap the length."""
    s = " ".join((s or "").split())
    for sep in (" — ", " - ", ": "):
        i = s.find(sep)
        if 0 < i < 22:
            s = s[i + len(sep):]
            break
    for stop in (". ", " — ", "; "):
        j = s.find(stop)
        if 0 < j:
            s = s[:j]
            break
    if len(s) > cap:
        s = s[: cap - 1].rstrip() + "…"
    return s


def _token_for(raw: dict) -> str:
    import os

    auth = raw.get("auth") or {}
    env = auth.get("credentialsEnv")
    if env:
        return os.environ.get(env, "") or ""
    return auth.get("token") or ""  # inline token (rare)


def _a2a_edges(dlist) -> list:
    """[{name, base, desc, token}] for the a2a-type delegates in a raw delegate list.
    Peer-reported lists are redacted (no credentialsEnv/token) → token='' → leaf."""
    out = []
    for d in dlist or []:
        if not isinstance(d, dict) or str(d.get("type")) != "a2a":
            continue
        url = d.get("url")
        if not url:
            continue
        out.append(
            {
                "name": d.get("name") or "",
                "base": _norm(url),
                "desc": (d.get("description") or "").strip(),
                "token": _token_for(d),
            }
        )
    return out


async def _probe(client, d: dict) -> dict:
    """Node identity + liveness from a peer's PUBLIC surfaces (card, else /healthz)."""
    b = d["base"]
    node = {"id": b, "name": d["name"] or b, "role": _short(d["desc"]), "up": False, "version": "", "kind": "agent", "url": b}
    try:
        r = await client.get(b + "/.well-known/agent-card.json")
        if r.status_code == 200:
            c = r.json()
            node["up"] = True
            node["name"] = c.get("name") or node["name"]
            node["version"] = c.get("version") or ""
            desc = (c.get("description") or "").strip()
            if desc:
                node["role"] = _short(desc)
            return node
    except Exception:  # noqa: BLE001
        pass
    try:
        h = await client.get(b + "/healthz")
        node["up"] = h.status_code == 200
    except Exception:  # noqa: BLE001
        node["up"] = False
    return node


async def _peer_delegates(client, base: str, token: str):
    """A peer's own delegate list (redacted view — url/type/name/desc, no secrets).
    Requires the peer's bearer, which we only hold for THIS agent's direct delegates."""
    try:
        r = await client.get(base + "/api/delegates", headers={"Authorization": "Bearer " + token})
        if r.status_code == 200:
            return (r.json() or {}).get("delegates") or []
    except Exception:  # noqa: BLE001
        pass
    return None


def _self_role(doc: dict) -> str:
    a2a = doc.get("a2a") or {}
    return _short((a2a.get("description") or "").strip()) or "orchestrator"


async def _crawl() -> dict:
    """Assemble the fleet delegation graph seen from this agent. BFS over A2A edges;
    fetch a peer's own edges only where we hold its token (its /api/delegates is gated),
    so the graph reaches this agent's direct delegates + their delegates (as leaves)."""
    import asyncio
    import os

    try:
        import httpx
    except Exception:  # noqa: BLE001
        return {"self": "self", "nodes": [], "edges": [], "error": "httpx unavailable"}

    from graph.config_io import load_yaml_doc

    doc = load_yaml_doc() or {}
    self_name = (doc.get("identity") or {}).get("name") or os.environ.get("AGENT_NAME") or "self"
    self_base = _norm(os.environ.get("A2A_PUBLIC_URL") or "") or "self"

    seed = _a2a_edges(doc.get("delegates"))
    tokens = {d["base"]: d["token"] for d in seed if d["token"]}  # only our own delegates carry usable tokens

    nodes: dict = {
        self_base: {"id": self_base, "name": self_name, "role": _self_role(doc), "up": True, "version": "", "kind": "self", "url": self_base}
    }
    edges: list = []
    seen_edges: set = set()

    def add_edge(a: str, b: str) -> None:
        if a != b and (a, b) not in seen_edges:
            seen_edges.add((a, b))
            edges.append({"from": a, "to": b})

    MAXNODES = 80
    async with httpx.AsyncClient(timeout=6.0, verify=False, follow_redirects=False) as client:
        queue: list = [(self_base, seed)]
        while queue and len(nodes) < MAXNODES:
            owner, dels = queue.pop(0)
            fresh = [d for d in dels if d["base"] not in nodes]
            probed = await asyncio.gather(*[_probe(client, d) for d in fresh], return_exceptions=True)
            pmap = {}
            for d, p in zip(fresh, probed):
                pmap[d["base"]] = p if not isinstance(p, Exception) else None
            for d in dels:
                b = d["base"]
                add_edge(owner, b)
                if b in nodes:
                    continue
                node = pmap.get(b) or {"id": b, "name": d["name"] or b, "role": _short(d["desc"]), "up": False, "version": "", "kind": "agent", "url": b}
                nodes[b] = node
                tok = tokens.get(b)
                if tok and node.get("up"):
                    peer = await _peer_delegates(client, b, tok)
                    if peer:
                        queue.append((b, _a2a_edges(peer)))
    return {"self": self_base, "nodes": list(nodes.values()), "edges": edges, "count": len(nodes)}
