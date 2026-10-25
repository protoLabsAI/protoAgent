// Org Chart — who can delegate to whom, and the work flowing along those edges right now.
//
// Three feeds, one picture:
//   * the TOPOLOGY (GET /api/plugins/orgchart/topology, polled): nodes + capability edges,
//     with each target's lifetime work stats from the ledger;
//   * the LIVE edges (GET /api/ledger/live once, then the bus): `delegation.started` →
//     `.progress` → `.finished`, so a node lights up the moment work reaches it and the
//     drawer shows the coder's plan / current tool / narration as it happens. A slow
//     reconcile poll covers a missed frame or a relay that isn't there;
//   * the RECENT ledger rows (GET /api/ledger): the activity feed and the drawer history.
//
// Rendering is keyed (one <g> per node id, created once, updated in place) and every
// position change is tweened in a single rAF loop that also drives the edge paths and the
// sparks travelling along live edges — so a node never jumps and an edge never detaches.
// The loop stops when nothing moves. prefers-reduced-motion snaps instead.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const lc = (s) => String(s ?? "").trim().toLowerCase();
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
const SVGNS = "http://www.w3.org/2000/svg";

let kit;
try {
  kit = await import(BASE + "/_ds/plugin-kit.js");
} catch (e) {
  // Fail loudly and name the real cause (#2392): without the kit there is no bearer.
  showError("The DS plugin kit failed to load — the console bundle (/_ds) is missing or stale. This view cannot authenticate without it.");
  throw e;
}
async function api(path) {
  const r = await kit.apiFetch(path);
  const d = await r.json().catch(() => { throw new Error("HTTP " + r.status + " (non-JSON response)"); });
  if (!r.ok) throw new Error((d && (d.detail || d.error)) || "HTTP " + r.status);
  return d;
}
function showError(msg) {
  const el = $("err");
  el.hidden = !msg;
  el.textContent = msg || "";
}

// ── geometry ────────────────────────────────────────────────────────────────────────
const NODE_W = 228, NODE_H = 64, GAP_X = 104, GAP_Y = 20, PAD = 36;
const MOVE_MS = 460;
const FINISH_LINGER_MS = 2600;

// Chart kind → the ledger's to_kind for the same delegate (topology.LEDGER_KIND).
const LEDGER_KIND = { self: "a2a", agent: "a2a", coder: "acp", model: "openai", subagent: "subagent" };
const CHART_KIND = { a2a: "agent", acp: "coder", openai: "model", subagent: "subagent" };
const KINDS = [
  { kind: "agent", label: "Agents" },
  { kind: "subagent", label: "Subagents" },
  { kind: "coder", label: "Coders" },
  { kind: "model", label: "Models" },
];
const TAG = { self: "YOU", agent: "AGENT", coder: "ACP", model: "MODEL", subagent: "SUBAGENT", fold: "" };
// Lucide-style glyphs, drawn at 16px around (0,0).
const GLYPH = {
  self: "M-2 -7h4M0 -7v3M-6 -4h12a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-12a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM-3 1v1M3 1v1",
  agent: "M-2 -7h4M0 -7v3M-6 -4h12a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-12a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM-3 1v1M3 1v1",
  coder: "M-7 -6h14a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-14a1 1 0 0 1-1-1v-10a1 1 0 0 1 1-1zM-4 -2l3 2.5-3 2.5M1 3h3",
  model: "M0 -8l2 5 5 2-5 2-2 5-2-5-5-2 5-2z",
  subagent: "M-6 -6h4v4h-4zM2 2h4v4h-4zM-4 -2v4a2 2 0 0 0 2 2h4",
  fold: "M-7 -5h14M-7 0h14M-7 5h9",
};
const TOOL_VERB = { read: "Reading", edit: "Editing", execute: "Running", search: "Searching", fetch: "Fetching",
  delete: "Deleting", move: "Moving", think: "Thinking" };

// ── state ───────────────────────────────────────────────────────────────────────────
let TOPO = null;            // last topology payload
let SELF = "";              // self node id
const LIVE = new Map();     // edge id → live edge (+ status, endedAt, progress)
let RECENT = [];            // ledger rows, newest first
let SEL = null;             // selected node id (drawer)
// Nodes that appeared for live work the topology hasn't drawn yet (a subagent's first
// run). Kept once seen — a node that vanished when its run ended, then reappeared on the
// next topology poll, made the whole layout jump twice.
const STICKY = new Map();
let POLL_S = 8;
let lastSeq = -1;
let PEERS = 0;
let staleRetry = 0;              // readable fleet peers: their work arrives by poll, not bus
const HIDE_KEY = "orgchart.hidden";
const store = {
  get() { try { return new Set(JSON.parse(localStorage.getItem(HIDE_KEY) || "[]")); } catch { return new Set(); } },
  set(v) { try { localStorage.setItem(HIDE_KEY, JSON.stringify([...v])); } catch { /* not fatal */ } },
};
let HIDDEN = store.get();
// Agents whose coders and subagents are unfolded (folded is the default), remembered.
const OPEN_KEY = "orgchart.unfolded";
const openStore = {
  get() { try { return new Set(JSON.parse(localStorage.getItem(OPEN_KEY) || "[]")); } catch { return new Set(); } },
  set(v) { try { localStorage.setItem(OPEN_KEY, JSON.stringify([...v])); } catch { /* not fatal */ } },
};
let UNFOLDED = openStore.get();
// More leaves than this under one agent fold into a chip; this many or fewer just draw.
const FOLD_OVER = 2;

// ── time ────────────────────────────────────────────────────────────────────────────
// The server stamps live edges with ITS clock (epoch seconds); `skew` maps it onto ours
// so an elapsed clock reads right on a box whose time differs from the browser's.
let skew = 0;
const now = () => Date.now() / 1000;
const serverNow = () => now() + skew;
function dur(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + "s";
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + "m " + String(s).padStart(2, "0") + "s";
  return Math.floor(m / 60) + "h " + (m % 60) + "m";
}
function ago(iso) {
  const t = typeof iso === "number" ? iso : Date.parse(iso) / 1000;
  if (!isFinite(t)) return "";
  const s = now() - t;
  if (s < 45) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
const money = (v) => {
  v = v == null ? NaN : Number(v);
  return !isFinite(v) ? "" : v < 0.01 ? "<$0.01" : "$" + v.toFixed(v < 10 ? 2 : 0);
};
const clip = (s, n) => { s = String(s ?? "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; };

// ── the merged graph ────────────────────────────────────────────────────────────────
function nodeKey(kind, name) { return LEDGER_KIND[kind] + ":" + lc(name); }

/** The topology node a live edge or ledger row lands on — or a synthetic one, so work
 *  reaching a delegate the crawl hasn't drawn (a subagent's first run) still shows.
 *  `owner` is the node that dispatched it: a member's `coder` is a different process from
 *  this agent's `coder`, so a peer's work is matched among THAT peer's delegates first. */
function targetOf(toKind, toName, index, owner) {
  const key = toKind + ":" + lc(toName);
  const mine = !owner || owner === SELF;
  const hit = mine ? index.get(key) : index.children?.get(owner)?.get(key);
  if (hit) return hit;
  const kind = CHART_KIND[toKind] || "agent";
  if (!mine) {
    // A member delegating to an a2a agent the chart already draws (the hub, a peer).
    const anyAgent = toKind === "a2a" ? index.get(key) : null;
    if (anyAgent) return anyAgent;
    return { id: "live:" + owner + ":" + key, name: toName, kind, up: null, synthetic: true, owner };
  }
  return { id: (kind === "subagent" ? "subagent:" : "live:" + toKind + ":") + lc(toName), name: toName, kind, up: null, synthetic: true };
}

// Runs that finished since the topology snapshot was taken (it is cached server-side for
// a few seconds): counted on their node at once, and dropped once a snapshot's run count
// has caught up with them — so a node reads "1 run · 100% ok" the moment it lands.
const LANDED = new Map();   // live edge id → {node, outcome, ms, cost, at, upTo}
function withLanded(n) {
  let w = n.work;
  const runs = w?.dispatches || 0;
  for (const [k, l] of LANDED) {
    if (l.node !== n.id) continue;
    if (runs >= l.upTo) { LANDED.delete(k); continue; }
    w = { dispatches: 0, ok: 0, failed: 0, cancelled: 0, cost_usd: null, priced: 0, duration_ms: 0, ...w };
    w.dispatches += 1;
    const o = l.outcome === "failed" || l.outcome === "cancelled" ? l.outcome : "ok";
    w[o] = (w[o] || 0) + 1;
    w.duration_ms = (w.duration_ms || 0) + (Number(l.ms) || 0);
    if (l.cost != null && isFinite(Number(l.cost))) { w.cost_usd = (w.cost_usd || 0) + Number(l.cost); w.priced = (w.priced || 0) + 1; }
    w.last_at = new Date((l.at - skew) * 1000).toISOString();
  }
  return w === n.work ? n : { ...n, work: w };
}

function buildGraph() {
  const nodes = new Map(), edges = new Map(), index = new Map();
  for (const n of TOPO?.nodes || []) {
    if (n.kind !== "self" && HIDDEN.has(n.kind)) continue;
    const m = withLanded(n);
    nodes.set(n.id, m);
    if (n.kind !== "self") index.set(nodeKey(n.kind, n.name), m);
  }
  index.children = new Map();   // owner node id → (ledger key → its delegate node)
  for (const e of TOPO?.edges || []) {
    if (nodes.has(e.from) && nodes.has(e.to)) edges.set(e.from + ">" + e.to, { ...e });
    const child = nodes.get(e.to);
    if (child && child.kind !== "self") {
      if (!index.children.has(e.from)) index.children.set(e.from, new Map());
      index.children.get(e.from).set(nodeKey(child.kind, child.name), child);
    }
  }
  // Every member may run a `claude-code`; this agent's own work lands on ITS coder, never on
  // whichever same-named node the crawl listed last.
  for (const [k, n] of index.children.get(SELF) || []) index.set(k, n);
  for (const [id, n] of STICKY) {
    const from = n.owner && nodes.has(n.owner) ? n.owner : SELF;
    const drawn = from === SELF ? index.get(nodeKey(n.kind, n.name)) : index.children.get(from)?.get(nodeKey(n.kind, n.name));
    if (drawn || nodes.has(id)) { STICKY.delete(id); continue; }
    if (HIDDEN.has(n.kind)) continue;
    nodes.set(id, n);
    edges.set(from + ">" + id, { from, to: id, kind: "task" });
  }
  // Live edges: from the parent edge's target (a subagent that called a coder) or self.
  const flows = [];
  for (const le of LIVE.values()) {
    const owner = le.owner && nodes.has(le.owner) ? le.owner : SELF;
    const tgt = targetOf(le.to_kind, le.to_name, index, owner);
    if (tgt.kind !== "self" && HIDDEN.has(tgt.kind)) continue;
    if (!nodes.has(tgt.id)) nodes.set(tgt.id, tgt);
    if (tgt.synthetic) STICKY.set(tgt.id, tgt);
    let from = owner;
    const parent = le.parent_id && LIVE.get(le.parent_id);
    if (parent) {
      const p = targetOf(parent.to_kind, parent.to_name, index, parent.owner && nodes.has(parent.owner) ? parent.owner : SELF);
      if (nodes.has(p.id)) from = p.id;
    }
    if (!nodes.has(from)) continue;
    const key = from + ">" + tgt.id;
    if (!edges.has(key)) edges.set(key, { from, to: tgt.id, kind: "live" });
    flows.push({ edge: le, from, to: tgt.id });
  }
  return { nodes, edges, flows, index };
}

/** Each node's parent in a spanning tree rooted at self (first edge to reach it, BFS). A
 *  node with two parents (a member that is also another member's delegate) is PLACED under
 *  the first; its other edges still draw. */
function treeParents(g) {
  const out = new Map();
  for (const e of g.edges.values()) { if (!out.has(e.from)) out.set(e.from, []); out.get(e.from).push(e.to); }
  const parent = new Map([[SELF, null]]);
  const q = [SELF];
  while (q.length) {
    const id = q.shift();
    for (const t of out.get(id) || []) if (!parent.has(t) && g.nodes.has(t)) { parent.set(t, id); q.push(t); }
  }
  return parent;
}

const isLeaf = (n) => n && n.kind !== "self" && n.kind !== "agent" && n.kind !== "fold";
const PLURAL = { coder: ["coder", "coders"], subagent: ["subagent", "subagents"], model: ["model", "models"] };

/** Fold each agent's leaves (coders, subagents, models) into one chip, unless the operator
 *  unfolded it. A leaf that is working, just finished, or selected stays out of the fold —
 *  the chart's job is to show work, so work is never hidden. Mutates `g`. */
function fold(g) {
  const parent = treeParents(g);
  const leaves = new Map();   // owner → its leaf nodes, in topology order
  for (const n of g.nodes.values()) {
    if (!isLeaf(n)) continue;
    const owner = parent.get(n.id);
    if (owner == null) continue;
    if (!leaves.has(owner)) leaves.set(owner, []);
    leaves.get(owner).push(n);
  }
  const active = new Set();
  for (const f of g.flows) { active.add(f.to); active.add(f.from); }
  if (SEL) active.add(SEL);
  g.folded = new Map();   // folded leaf id → its chip id
  for (const [owner, ls] of leaves) {
    if (ls.length <= FOLD_OVER) continue;
    const id = "fold:" + owner, open = UNFOLDED.has(owner);
    const tucked = open ? [] : ls.filter((n) => !active.has(n.id));
    const counts = {};
    for (const n of ls) counts[n.kind] = (counts[n.kind] || 0) + 1;
    const work = { dispatches: 0, ok: 0, failed: 0, cancelled: 0 };
    for (const n of tucked) for (const k in work) work[k] += n.work?.[k] || 0;
    g.nodes.set(id, { id, kind: "fold", owner, open, counts, total: ls.length, tucked: tucked.length, work, up: null });
    g.edges.set(owner + ">" + id, { from: owner, to: id, kind: "fold" });
    for (const n of tucked) { g.nodes.delete(n.id); g.folded.set(n.id, id); }
  }
  // An edge into or out of a folded leaf now ends at its chip (one edge per chip pair).
  for (const [k, e] of [...g.edges]) {
    const from = g.folded.get(e.from) || e.from, to = g.folded.get(e.to) || e.to;
    if (from === e.from && to === e.to) continue;
    g.edges.delete(k);
    if (from !== to && !g.edges.has(from + ">" + to)) g.edges.set(from + ">" + to, { ...e, from, to });
  }
  for (const f of g.flows) { f.from = g.folded.get(f.from) || f.from; f.to = g.folded.get(f.to) || f.to; }
  return g;
}

function foldLabel(n) {
  const bits = Object.entries(n.counts).map(([k, c]) => c + " " + (PLURAL[k] || [k, k + "s"])[c === 1 ? 0 : 1]);
  return bits.join(" · ");
}

/** A tidy tree: each node's children sit beside it in one contiguous band, the parent
 *  centred on them, so whose coder is whose reads straight off the chart. Columns are
 *  tree depth; within a parent, agents first, then leaves, then the fold chip, each in
 *  topology order (stable, so a poll never reshuffles rows). */
function layout(g) {
  const parent = treeParents(g);
  const kids = new Map();
  let order = 0;
  const rank = new Map();
  for (const n of g.nodes.values()) {
    rank.set(n.id, order++);
    if (n.id === SELF) continue;
    const p = parent.has(n.id) ? parent.get(n.id) : SELF;   // unreachable: hang off self
    if (!kids.has(p)) kids.set(p, []);
    kids.get(p).push(n);
  }
  const cls = (n) => (n.kind === "agent" ? 0 : n.kind === "fold" ? 2 : n.synthetic ? 1.5 : 1);
  for (const list of kids.values()) list.sort((a, b) => cls(a) - cls(b) || rank.get(a.id) - rank.get(b.id));
  const pos = new Map();
  const ROW = NODE_H + GAP_Y, BAND = GAP_Y * 1.5;   // extra air between one agent's band and the next
  let cursor = PAD, depthMax = 0;
  const place = (id, d) => {
    depthMax = Math.max(depthMax, d);
    const x = PAD + d * (NODE_W + GAP_X);
    const ch = kids.get(id) || [];
    if (!ch.length) { pos.set(id, { x, y: cursor }); cursor += ROW; return; }
    const top = cursor;
    ch.forEach((c, i) => {
      if (i && (kids.get(ch[i - 1].id) || []).length) cursor += BAND;
      place(c.id, d + 1);
    });
    const first = pos.get(ch[0].id).y, last = pos.get(ch[ch.length - 1].id).y;
    const y = Math.max(top, (first + last) / 2);
    pos.set(id, { x, y });
    cursor = Math.max(cursor, y + ROW);
  };
  place(SELF, 0);
  const height = Math.max(PAD * 2 + NODE_H, cursor - GAP_Y + PAD);
  const width = PAD * 2 + (depthMax + 1) * NODE_W + depthMax * GAP_X;
  return { pos, width, height };
}

// ── SVG plumbing ────────────────────────────────────────────────────────────────────
const svg = $("graph");
function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
const defs = el("defs", {}, svg);
const marker = el("marker", { id: "arw", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" }, defs);
el("path", { d: "M0 1L9 5L0 9z", class: "arrow" }, marker);
const L_EDGES = el("g", {}, svg), L_FLOWS = el("g", {}, svg), L_NODES = el("g", {}, svg);

const NODE_EL = new Map();   // id → {g, parts…, cur:{x,y}, from:{x,y}, to:{x,y}, t0}
const EDGE_EL = new Map();   // key → path
const FLOW_EL = new Map();   // live edge id → {path, spark, from, to}

function edgePath(a, b) {
  const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x - 4, y2 = b.y + NODE_H / 2;
  if (x2 <= x1) {   // same column (a member supervised sideways) — loop out to the right
    const dx = NODE_W * 0.35;
    return `M${x1} ${y1} C${x1 + dx} ${y1}, ${x1 + dx} ${y2}, ${b.x + NODE_W + 4} ${y2}`;
  }
  const mx = (x1 + x2) / 2;
  return `M${x1} ${y1} C${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
}

function ensureNode(n) {
  let h = NODE_EL.get(n.id);
  if (h) return h;
  const g = el("g", { class: "node entering", tabindex: "0", role: "button", "data-id": n.id }, L_NODES);
  el("rect", { class: "card", width: NODE_W, height: NODE_H, rx: 12 }, g);
  const ringG = el("g", { transform: `translate(26 ${NODE_H / 2})` }, g);
  el("circle", { class: "halo", r: 15 }, ringG);
  el("circle", { class: "ring", r: 15 }, ringG);
  const glyph = el("path", { class: "glyph", transform: "scale(.82)" }, ringG);
  const name = el("text", { class: "name", x: 52, y: 27 }, g);
  const meta = el("text", { class: "meta", x: 52, y: 45 }, g);
  const tag = el("text", { class: "tag", x: NODE_W - 12, y: 17, "text-anchor": "end" }, g);
  const bar = el("g", { transform: `translate(52 ${NODE_H - 9})` }, g);
  const count = el("g", { transform: `translate(${NODE_W - 4} 4)`, visibility: "hidden" }, g);
  el("circle", { class: "count", r: 9 }, count);
  const countText = el("text", { class: "count-text", "text-anchor": "middle", y: 3.5 }, count);
  const act = () => (n.kind === "fold" ? toggleFold(n.owner) : openDrawer(n.id));
  g.addEventListener("click", act);
  g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); act(); } });
  g.addEventListener("animationend", (e) => { if (e.animationName === "node-in") g.classList.remove("entering"); });
  h = { g, glyph, name, meta, tag, bar, count, countText, sig: "", cur: null, from: null, to: null, t0: 0 };
  NODE_EL.set(n.id, h);
  return h;
}

/** What is a node doing right now? Its running live edges, newest first. */
function runningOn(id, flows) { return flows.filter((f) => f.to === id && f.edge.status === "running").map((f) => f.edge); }

function stateClass(n, busy) {
  const c = ["node", n.kind];
  if (busy) c.push("busy");
  if (n.kind === "self") c.push("self");
  else if (n.up === true) c.push("up");
  else if (n.up === false) c.push("down");
  if (SEL === n.id) c.push("sel");
  return c;
}

function workLine(n) {
  const w = n.work;
  if (!w || !w.dispatches) {
    if (n.kind === "self") return clip(n.role || "orchestrator", 30);
    return n.up === false ? "down · unreachable" : n.synthetic ? "first run" : "no work yet";
  }
  const pct = Math.round((100 * (w.ok || 0)) / Math.max(1, w.dispatches - (w.cancelled || 0)));
  const bits = [w.dispatches + (w.dispatches === 1 ? " run" : " runs"), pct + "% ok"];
  if (w.cost_usd != null && w.priced) bits.push(money(w.cost_usd));
  return bits.join(" · ");
}

/** The one-line "doing" for a busy node: the coder's current tool when it reports one. */
function doingLine(le) {
  const tool = le.progress?.current_tool;
  if (tool && tool.status === "running" && tool.name) {
    const verb = TOOL_VERB[tool.kind] || "Using";
    // The file is the useful noun: "Editing calc.py", not "Editing Edit" (a coder opens a
    // call with a generic title and names it later; its location is known sooner).
    const path = tool.locations?.[0]?.path;
    let noun = path ? path.split("/").pop() : tool.name.replace(/^(Read|Edit|Write|Bash|Run)\b\s*/i, "");
    if (!noun || lc(noun) === lc(tool.name) && /^(read|edit|write|bash)$/i.test(noun)) noun = "";
    return noun ? clip(verb + " " + noun, 27) : verb + "…";
  }
  return clip(le.what || "Working", 27);
}

function paintNode(h, n, busyEdges) {
  if (n.kind === "fold") return paintFold(h, n);
  const busy = busyEdges.length > 0;
  const lead = busyEdges[0];
  const sig = JSON.stringify([n.name, n.kind, n.up, n.work, busy, lead && doingLine(lead), busyEdges.length, SEL === n.id]);
  const keep = [...h.g.classList].filter((c) => c === "entering" || c.startsWith("landed-"));
  h.g.setAttribute("class", [...stateClass(n, busy), ...keep].join(" "));
  h.g.setAttribute("aria-label", `${n.name}, ${TAG[n.kind] || n.kind}${busy ? ", working" : ""}`);
  if (sig === h.sig) return;
  h.sig = sig;
  h.glyph.setAttribute("d", GLYPH[n.kind] || GLYPH.agent);
  h.name.textContent = clip(n.name, 20);
  h.tag.textContent = TAG[n.kind] || "";
  h.meta.textContent = "";
  if (busy) {
    const now_ = el("tspan", { class: "now" }, h.meta);
    now_.textContent = doingLine(lead);
    // While busy the corner carries the clock instead of the kind tag.
    h.tag.textContent = "";
    const clock = el("tspan", { class: "clock", "data-start": String(lead.started_at) }, h.tag);
    clock.textContent = dur(serverNow() - lead.started_at);
  } else {
    h.meta.textContent = workLine(n);
  }
  // Success bar under the meta line: ok vs failed share of this target's lifetime work.
  h.bar.textContent = "";
  const w = n.work;
  if (!busy && w && w.dispatches) {
    const span = NODE_W - 52 - 14, done = Math.max(1, (w.ok || 0) + (w.failed || 0));
    el("rect", { class: "bar-bg", width: span, height: 2.5, rx: 1.25 }, h.bar);
    el("rect", { class: "bar-ok", width: (span * (w.ok || 0)) / done, height: 2.5, rx: 1.25 }, h.bar);
    if (w.failed) el("rect", { class: "bar-bad", x: (span * (w.ok || 0)) / done, width: (span * w.failed) / done, height: 2.5, rx: 1.25 }, h.bar);
  }
  h.count.setAttribute("visibility", busyEdges.length > 1 ? "visible" : "hidden");
  h.countText.textContent = "×" + busyEdges.length;
}

function paintFold(h, n) {
  h.g.setAttribute("class", ["node", "fold", n.open ? "open" : "shut", ...[...h.g.classList].filter((c) => c === "entering")].join(" "));
  const label = foldLabel(n);
  h.g.setAttribute("aria-label", `${n.open ? "Fold" : "Show"} ${label}`);
  h.g.setAttribute("aria-expanded", String(n.open));
  const sig = JSON.stringify([label, n.open, n.tucked, n.total, n.work]);
  if (sig === h.sig) return;
  h.sig = sig;
  h.glyph.setAttribute("d", GLYPH.fold);
  h.name.textContent = clip(label, 26);
  // The hint rides the meta line: a corner tag would collide with a long label.
  h.tag.textContent = "";
  const w = n.work;
  const hint = el("tspan", { class: "hint" });
  hint.textContent = n.open ? "fold ▴" : "show ▸";
  h.meta.textContent = n.open ? ""
    : n.tucked < n.total ? `${n.tucked} folded · `
    : w.dispatches ? workLine({ work: w }) + " · " : "";
  h.meta.appendChild(hint);
  h.bar.textContent = "";
  h.count.setAttribute("visibility", "hidden");
}
function toggleFold(owner) {
  UNFOLDED.has(owner) ? UNFOLDED.delete(owner) : UNFOLDED.add(owner);
  openStore.set(UNFOLDED);
  render();
  NODE_EL.get("fold:" + owner)?.g.focus({ preventScroll: true });
}

// ── camera: zoom + pan ──────────────────────────────────────────────────────────────
// The viewBox is the camera: `x,y` its top-left in chart units, `k` the zoom. `user` turns
// true once the operator zooms or pans, and from then on a poll never yanks the view.
const CAM = { x: 0, y: 0, k: 1, user: false };
const K_MIN = 0.2, K_MAX = 2.5, K_FIT_MAX = 1.45, K_STEP = 1.25;
function viewSize() { const c = $("canvas"); return { w: c.clientWidth || 1, h: c.clientHeight || 1 }; }
function clampCam() {
  if (!LAYOUT) return;
  // Keep a quarter of the view on the chart, so it can't be flung out of sight.
  const { w, h } = viewSize(), vw = w / CAM.k, vh = h / CAM.k;
  CAM.x = Math.min(Math.max(CAM.x, -vw * 0.75), LAYOUT.width - vw * 0.25);
  CAM.y = Math.min(Math.max(CAM.y, -vh * 0.75), LAYOUT.height - vh * 0.25);
}
function applyCam() {
  clampCam();
  const { w, h } = viewSize();
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);
  svg.setAttribute("viewBox", `${CAM.x} ${CAM.y} ${w / CAM.k} ${h / CAM.k}`);
  $("zoom-k").textContent = Math.round(CAM.k * 100) + "%";
}
function fitCam() {
  if (!LAYOUT) return;
  const { w, h } = viewSize();
  const fit = Math.min((w - 16) / LAYOUT.width, (h - 16) / LAYOUT.height);
  CAM.k = Math.min(K_FIT_MAX, Math.max(K_MIN, isFinite(fit) ? fit : 1));
  CAM.x = (LAYOUT.width - w / CAM.k) / 2;
  CAM.y = (LAYOUT.height - h / CAM.k) / 2;
  CAM.user = false;
  applyCam();
}
/** Zoom by `f` keeping the chart point under (`sx`,`sy`) — canvas pixels — where it is. */
function zoomAt(f, sx, sy) {
  const k = Math.min(K_MAX, Math.max(K_MIN, CAM.k * f));
  if (k === CAM.k) return;
  CAM.x += sx / CAM.k - sx / k;
  CAM.y += sy / CAM.k - sy / k;
  CAM.k = k;
  CAM.user = true;
  applyCam();
}
const zoomCentre = (f) => { const { w, h } = viewSize(); zoomAt(f, w / 2, h / 2); };
{
  const c = $("canvas");
  const at = (e) => { const r = c.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  // Map-style: the wheel (and a trackpad pinch, which Chromium reports as ctrl+wheel)
  // zooms at the pointer; dragging the background pans.
  c.addEventListener("wheel", (e) => {
    e.preventDefault();
    const px = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
    // A pinch sends small deltas, so it gets a steeper curve than a notched wheel.
    const rate = e.ctrlKey ? 0.01 : 0.0025;
    zoomAt(Math.exp(-e.deltaY * px * rate), ...at(e));
  }, { passive: false });
  // WebKit (the desktop app's webview) reports a pinch as gesture events, not ctrl+wheel.
  let g0 = 1;
  c.addEventListener("gesturestart", (e) => { e.preventDefault(); g0 = e.scale; });
  c.addEventListener("gesturechange", (e) => { e.preventDefault(); zoomAt(e.scale / g0, ...at(e)); g0 = e.scale; });
  // Drag anywhere to pan, cards included (they cover most of the chart). A press that
  // doesn't move past a few pixels is still a click on whatever it landed on.
  const SLOP = 4;
  let drag = null, dragged = false;
  c.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest(".cam")) return;
    e.preventDefault();   // a drag must never begin a text selection
    drag = { x: e.clientX, y: e.clientY, cx: CAM.x, cy: CAM.y, id: e.pointerId, moved: false };
    dragged = false;
  });
  c.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved) {
      if (Math.hypot(dx, dy) < SLOP) return;
      drag.moved = true;
      c.setPointerCapture(e.pointerId);
      c.classList.add("panning");
    }
    CAM.x = drag.cx - dx / CAM.k;
    CAM.y = drag.cy - dy / CAM.k;
    CAM.user = true;
    applyCam();
  });
  const end = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    dragged = drag.moved;
    drag = null;
    c.classList.remove("panning");
  };
  // The click that ends a pan is not a click on the card it ended over.
  c.addEventListener("click", (e) => { if (dragged) { e.stopPropagation(); e.preventDefault(); dragged = false; } }, true);
  c.addEventListener("pointerup", end);
  c.addEventListener("pointercancel", end);
  $("zoom-in").addEventListener("click", () => zoomCentre(K_STEP));
  $("zoom-out").addEventListener("click", () => zoomCentre(1 / K_STEP));
  $("zoom-fit").addEventListener("click", fitCam);
  addEventListener("keydown", (e) => {
    if (e.target.closest?.("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "+" || e.key === "=") zoomCentre(K_STEP);
    else if (e.key === "-" || e.key === "_") zoomCentre(1 / K_STEP);
    else if (e.key === "0") fitCam();
  });
}

// ── render ──────────────────────────────────────────────────────────────────────────
let GRAPH = null, LAYOUT = null;
function render() {
  if (!TOPO) return;
  GRAPH = fold(buildGraph());
  LAYOUT = layout(GRAPH);
  // Until the operator moves the camera, it keeps the whole chart in view as it changes.
  CAM.user ? applyCam() : fitCam();
  const t = performance.now();

  for (const [id, h] of NODE_EL) if (!GRAPH.nodes.has(id)) { h.g.remove(); NODE_EL.delete(id); }
  for (const n of GRAPH.nodes.values()) {
    const h = ensureNode(n);
    const p = LAYOUT.pos.get(n.id);
    if (!h.cur || REDUCED) { h.cur = { ...p }; h.to = { ...p }; }
    else if (h.to.x !== p.x || h.to.y !== p.y) { h.from = { ...h.cur }; h.to = { ...p }; h.t0 = t; }
    paintNode(h, n, runningOn(n.id, GRAPH.flows));
  }

  const usedTargets = new Set([...GRAPH.nodes.values()].filter((n) => n.work && n.work.dispatches).map((n) => n.id));
  for (const [k, p] of EDGE_EL) if (!GRAPH.edges.has(k)) { p.remove(); EDGE_EL.delete(k); }
  for (const [k, e] of GRAPH.edges) {
    let p = EDGE_EL.get(k);
    if (!p) { p = el("path", { "marker-end": "url(#arw)" }, L_EDGES); EDGE_EL.set(k, p); }
    const cls = ["edge"];
    if (e.kind === "member") cls.push("member");
    if (e.kind === "fold") cls.push("fold");
    if (usedTargets.has(e.to) || e.kind === "task") cls.push("used");
    p.setAttribute("class", cls.join(" "));
    p._e = e;
  }

  const live = new Set();
  for (const f of GRAPH.flows) {
    live.add(f.edge.id);
    let fl = FLOW_EL.get(f.edge.id);
    if (!fl) {
      const path = el("path", { class: "flow" }, L_FLOWS);
      const spark = REDUCED ? null : el("circle", { class: "spark", r: 3.2 }, L_FLOWS);
      fl = { path, spark, phase: Math.random() };
      FLOW_EL.set(f.edge.id, fl);
    }
    fl.from = f.from; fl.to = f.to;
    const st = f.edge.status;
    fl.path.setAttribute("class", "flow" + (st !== "running" ? " " + st : "") + (f.edge.gone ? " gone" : ""));
    if (fl.spark) fl.spark.style.display = st === "running" ? "" : "none";
  }
  for (const [id, fl] of FLOW_EL) if (!live.has(id)) { fl.path.remove(); fl.spark?.remove(); FLOW_EL.delete(id); }

  // Empty because the filters hid everything is a different story from a fleet that has
  // delegated nothing; and clear the text when it hides, so it can't linger over the graph.
  const empty = GRAPH.nodes.size <= 1 && LIVE.size === 0;
  $("empty").hidden = !empty;
  $("empty").innerHTML = !empty ? ""
    : HIDDEN.size && (TOPO?.nodes || []).some((n) => n.kind !== "self")
      ? "<strong>Everything is filtered out</strong><span>Turn a node type back on in the filters above to see the chart.</span>"
      : "<strong>Nothing delegated yet</strong><span>This chart lights up as work flows: try <code>@claude-code</code> in chat, or ask the agent to hand something off.</span>";
  renderStats();
  kick();
}

// One loop for every moving part: node tweens, the edges glued to them, and the sparks.
let raf = 0;
function kick() { if (!raf) raf = requestAnimationFrame(frame); }
function ease(x) { return 1 - Math.pow(1 - x, 3); }
function frame(t) {
  raf = 0;
  let moving = false;
  for (const h of NODE_EL.values()) {
    if (h.from && h.t0) {
      const k = Math.min(1, (t - h.t0) / MOVE_MS), e = ease(k);
      h.cur = { x: h.from.x + (h.to.x - h.from.x) * e, y: h.from.y + (h.to.y - h.from.y) * e };
      if (k >= 1) { h.from = null; h.t0 = 0; h.cur = { ...h.to }; } else moving = true;
    }
    h.g.setAttribute("transform", `translate(${h.cur.x} ${h.cur.y})`);
  }
  const at = (id) => NODE_EL.get(id)?.cur;
  for (const p of EDGE_EL.values()) {
    const a = at(p._e.from), b = at(p._e.to);
    if (a && b) p.setAttribute("d", edgePath(a, b));
  }
  let sparks = false;
  for (const fl of FLOW_EL.values()) {
    const a = at(fl.from), b = at(fl.to);
    if (!a || !b) continue;
    fl.path.setAttribute("d", edgePath(a, b));
    if (fl.spark && fl.spark.style.display !== "none") {
      const len = fl.path.getTotalLength();
      const pt = fl.path.getPointAtLength(((t / 1400 + fl.phase) % 1) * len);
      fl.spark.setAttribute("cx", pt.x);
      fl.spark.setAttribute("cy", pt.y);
      sparks = true;
    }
  }
  if (moving || sparks) raf = requestAnimationFrame(frame);
}

// ── header stats + filters ──────────────────────────────────────────────────────────
function renderStats() {
  const nodes = (TOPO?.nodes || []).filter((n) => n.kind !== "self");
  const running = [...LIVE.values()].filter((e) => e.status === "running").length;
  const today = RECENT.filter((r) => Date.parse(r.at) > Date.now() - 86400e3);
  const priced = today.filter((r) => r.cost_usd != null);
  const spend = priced.reduce((s, r) => s + Number(r.cost_usd || 0), 0);
  const parts = [
    `<span class="stat${running ? " hot" : ""}"><b>${running}</b>working now</span>`,
    `<span class="stat"><b>${nodes.length}</b>${nodes.length === 1 ? "delegate" : "delegates"}</span>`,
    `<span class="stat"><b>${today.length}</b>handoffs today</span>`,
  ];
  if (priced.length) parts.push(`<span class="stat"><b>${money(spend)}</b>spent today</span>`);
  $("stats").innerHTML = parts.join("");
}

let FILTER_SIG = "";
function renderFilters() {
  const counts = {};
  for (const n of TOPO?.nodes || []) counts[n.kind] = (counts[n.kind] || 0) + 1;
  // Rebuild only when the chips would change: a topology poll otherwise replaces the
  // button a keyboard user is focused on, and focus falls back to the page.
  const sig = JSON.stringify([counts, [...HIDDEN].sort()]);
  if (sig === FILTER_SIG) return;
  FILTER_SIG = sig;
  const box = $("filters");
  const focused = box.contains(document.activeElement) ? document.activeElement.dataset.kind : null;
  box.innerHTML = KINDS.filter((k) => counts[k.kind] || HIDDEN.has(k.kind))
    .map((k) => `<button class="chip" type="button" data-kind="${k.kind}" aria-pressed="${!HIDDEN.has(k.kind)}">${k.label}<span class="n">${counts[k.kind] || 0}</span></button>`)
    .join("");
  if (focused) box.querySelector(`button[data-kind="${focused}"]`)?.focus();
}
let resizeT = 0;
addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(render, 120); });
$("filters").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-kind]");
  if (!b) return;
  const k = b.dataset.kind;
  HIDDEN.has(k) ? HIDDEN.delete(k) : HIDDEN.add(k);
  store.set(HIDDEN);
  renderFilters();
  render();
});

// ── activity feed ───────────────────────────────────────────────────────────────────
const FEED_MAX = 40;
let FEED_SIG = "";
const FRESH = new Set();   // ids that should play the arrival animation once
// Who handed the work out, named as the chart names that node: a ledger's `from` is the
// sender's config identity, which a fleet of copied configs can leave the same everywhere.
function nodeName(id) {
  const n = (TOPO?.nodes || []).find((x) => x.id === (id || SELF));
  return n ? n.name : "";
}
function feedItems() {
  const items = [];
  const liveIds = new Set();
  for (const e of LIVE.values()) {
    liveIds.add(e.id);
    items.push({ id: e.id, live: true, from: nodeName(e.owner) || e.from, to: e.to_name, kind: e.to_kind, what: e.what, owner: e.owner || "",
      status: e.status, start: e.started_at, ms: e.duration_ms, cost: e.cost_usd });
  }
  for (const r of RECENT) {
    if (items.length >= FEED_MAX) break;
    if (r._live && liveIds.has(r._live)) continue;
    items.push({ id: "r" + r.edge_id, from: nodeName(SELF) || r.from_agent, to: r.to_name, kind: r.to_kind, what: r.what,
      status: r.origin === "background" && !r.duration_ms && r.outcome === "ok" ? "dispatched" : r.outcome,
      at: r.at, ms: r.duration_ms, cost: r.cost_usd });
  }
  return items.sort((a, b) => (b.start || Date.parse(b.at) / 1000 || 0) - (a.start || Date.parse(a.at) / 1000 || 0)).slice(0, FEED_MAX);
}
function statusIcon(s) {
  if (s === "running") return '<span class="spin" aria-label="running"></span>';
  if (s === "dispatched") return '<span class="dot cancelled" aria-label="dispatched"></span>';
  return `<span class="dot ${esc(s)}" aria-label="${esc(s)}"></span>`;
}
function renderFeed() {
  const items = feedItems();
  const sig = JSON.stringify(items.map((i) => [i.id, i.status, i.ms]));
  const running = items.filter((i) => i.status === "running").length;
  $("feed-sub").textContent = running ? `${running} in flight` : "";
  if (sig === FEED_SIG) return tickFeed();
  FEED_SIG = sig;
  if (!items.length) { $("feed").innerHTML = '<li class="none">No delegations yet.</li>'; return; }
  $("feed").innerHTML = items.map((i) => {
    const t = i.status === "running"
      ? `<span class="t" data-start="${Number(i.start) || 0}">${dur(serverNow() - Number(i.start))}</span>`
      : `<span class="t">${i.ms ? dur(i.ms / 1000) : ""}${i.at ? (i.ms ? " · " : "") + ago(i.at) : ""}</span>`;
    return `<li class="row ${esc(i.status)}${FRESH.has(i.id) ? " fresh" : ""}" data-kind="${esc(i.kind)}" data-name="${esc(i.to)}" data-owner="${esc(i.owner || "")}" tabindex="0">
      <span class="st">${statusIcon(i.status)}</span>
      <span class="who"><b>${esc(i.from || "agent")}</b><span class="arr">→</span><b>${esc(i.to)}</b></span>${t}
      <span class="what">${esc(clip(i.what, 160))}</span></li>`;
  }).join("");
  FRESH.clear();
}
function tickFeed() {
  for (const t of document.querySelectorAll("#feed .t[data-start]")) t.textContent = dur(serverNow() - Number(t.dataset.start));
}
$("feed").addEventListener("click", (e) => {
  const row = e.target.closest(".row");
  if (!row || !GRAPH) return;
  const owner = row.dataset.owner && GRAPH.nodes.has(row.dataset.owner) ? row.dataset.owner : SELF;
  const n = targetOf(row.dataset.kind, row.dataset.name, GRAPH.index, owner);
  if (GRAPH.nodes.has(n.id) || GRAPH.folded?.has(n.id)) openDrawer(n.id);
});
$("feed").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.closest(".row")?.click(); });

// ── drawer ──────────────────────────────────────────────────────────────────────────
let DRAWER_SIG = "";
function openDrawer(id) {
  SEL = id;
  DRAWER_SIG = "";
  $("drawer").classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
  document.querySelector(".stage").classList.add("drilled");
  renderDrawer();
  render();
  revealNode(id);
  $("d-close").focus({ preventScroll: true });
}
/** Pan the camera so a node is in view, centring it, without changing the zoom. */
function revealNode(id) {
  const p = LAYOUT?.pos.get(id);
  if (!p) return;
  const { w, h } = viewSize();
  const inside = p.x >= CAM.x && p.y >= CAM.y && p.x + NODE_W <= CAM.x + w / CAM.k && p.y + NODE_H <= CAM.y + h / CAM.k;
  if (inside) return;
  CAM.x = p.x + NODE_W / 2 - w / CAM.k / 2;
  CAM.y = p.y + NODE_H / 2 - h / CAM.k / 2;
  applyCam();
}
function closeDrawer() {
  const was = SEL;
  SEL = null;
  $("drawer").classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
  document.querySelector(".stage").classList.remove("drilled");
  render();
  NODE_EL.get(was)?.g.focus({ preventScroll: true });
}
$("d-close").addEventListener("click", closeDrawer);
addEventListener("keydown", (e) => { if (e.key === "Escape" && SEL) closeDrawer(); });

/** The node that delegates to `n` — whose ledger holds `n`'s history. A coder records its
 *  owner; otherwise it is the source of the topology edge into it. */
function ownerOf(n) {
  if (n.kind === "self") return SELF;
  if (n.owner) return n.owner;
  const into = (TOPO?.edges || []).find((e) => e.to === n.id && e.kind !== "member");
  return into ? into.from : SELF;
}
// A peer's ledger, read through the plugin (it holds the token): owner → {at, rows}.
const PEER_HISTORY = new Map();
function peerHistory(owner) {
  const hit = PEER_HISTORY.get(owner);
  if (!hit || Date.now() - hit.at > 15000) {
    PEER_HISTORY.set(owner, { at: Date.now(), rows: hit ? hit.rows : null });
    api("/api/plugins/orgchart/history?owner=" + encodeURIComponent(owner))
      .then((d) => { PEER_HISTORY.set(owner, { at: Date.now(), rows: d.readable ? d.edges || [] : [] }); DRAWER_SIG = ""; renderDrawer(); })
      .catch(() => { PEER_HISTORY.set(owner, { at: Date.now(), rows: [] }); });
  }
  return PEER_HISTORY.get(owner).rows;
}
function historyFor(n) {
  const kind = LEDGER_KIND[n.kind];
  if (n.kind === "self") return RECENT.slice(0, 20);
  const owner = ownerOf(n);
  const rows = owner === SELF ? RECENT : peerHistory(owner);
  if (rows == null) return null;   // loading
  return rows.filter((r) => r.to_kind === kind && lc(r.to_name) === lc(n.name)).slice(0, 20);
}

function renderDrawer() {
  if (!SEL || !GRAPH) return;
  const n = GRAPH.nodes.get(SEL);
  if (!n) return closeDrawer();
  // What this node is doing: work it was handed, and — for an agent that delegates
  // onward (this agent, a fleet member) — the work it has handed out right now.
  const handedOut = GRAPH.flows.filter((f) => f.from === n.id && f.edge.status === "running").map((f) => f.edge);
  const busy = [...runningOn(n.id, GRAPH.flows), ...handedOut.filter((e) => !runningOn(n.id, GRAPH.flows).includes(e))];
  const hist = historyFor(n);
  const sig = JSON.stringify([n, busy.map((e) => [e.id, e.progress, e.status]), hist && hist.map((r) => r.edge_id), RECENT.length]);
  if (sig === DRAWER_SIG) return tickDrawer();
  DRAWER_SIG = sig;
  const body = $("d-body");
  const scroll = body.scrollTop;

  $("d-name").textContent = n.name;
  const ring = $("d-ring");
  ring.className = "d-ring" + (busy.length ? " busy" : n.up === true ? " up" : n.up === false ? " down" : "");
  const statusWord = busy.length ? (handedOut.length && busy.length === handedOut.length ? `${busy.length} delegation${busy.length > 1 ? "s" : ""} in flight` : "Working")
    : n.kind === "self" ? "This agent" : n.up === false ? "Down" : n.up === true ? "Idle · up" : "Idle";
  $("d-sub").textContent = [TAG[n.kind], statusWord].filter(Boolean).join(" · ");

  const sec = [];
  for (const e of busy.slice(0, 4)) sec.push(nowCard(e, handedOut.includes(e)));

  const w = n.work;
  if (w && w.dispatches) {
    const done = Math.max(1, w.dispatches - (w.cancelled || 0));
    sec.push(`<section class="sec"><h3>Lifetime</h3><div class="grid">
      <div class="kpi"><b>${w.dispatches}</b><span>runs</span></div>
      <div class="kpi"><b>${Math.round((100 * (w.ok || 0)) / done)}%</b><span>succeeded</span></div>
      <div class="kpi"><b>${w.dispatches ? dur(w.duration_ms / w.dispatches / 1000) : "–"}</b><span>avg run</span></div>
      ${w.priced
        ? `<div class="kpi"><b>${money(w.cost_usd)}</b><span>${w.priced < w.dispatches ? `cost (${w.priced} priced)` : "cost"}</span></div>`
        : `<div class="kpi"><b>${w.last_at ? ago(w.last_at).replace(" ago", "") : "–"}</b><span>last run</span></div>`}
    </div></section>`);
  }

  const owner = ownerOf(n);
  const ownerName = owner !== SELF ? GRAPH.nodes.get(owner)?.name : "";
  sec.push(`<section class="sec"><h3>Recent work${ownerName ? ` <span class="quiet">· from ${esc(ownerName)}</span>` : ""}</h3>${
    hist == null ? `<p class="quiet">Loading…</p>`
    : hist.length ? `<ul class="hist">${hist.map(histRow).join("")}</ul>`
    : `<p class="quiet">No delegations recorded${n.kind === "self" ? "" : " to " + esc(n.name)} yet.</p>`}</section>`);
  sec.push(factsSection(n));
  body.innerHTML = sec.join("");
  body.scrollTop = scroll;
}

function nowCard(e, showTarget) {
  const p = e.progress || {};
  const plan = Array.isArray(p.plan) ? p.plan : [];
  const tool = p.current_tool;
  const toolCount = Math.max(0, Math.floor(Number(p.tool_count) || 0));
  const recent = (p.recent_tools || []).slice(-5).reverse().filter((t) => !tool || t.id !== tool.id || tool.status !== "running");
  const parts = [`<div class="now">`,
    `<div class="when">${showTarget ? `→ <b>${esc(e.to_name)}</b> · ` : ""}<span data-start="${Number(e.started_at) || 0}">${dur(serverNow() - Number(e.started_at))}</span>${toolCount ? ` · ${toolCount} tool call${toolCount > 1 ? "s" : ""}` : ""}</div>`,
    `<div class="task">${esc(clip(e.what, 400)) || "<span class=quiet>Working</span>"}</div>`];
  if (tool && tool.status === "running") {
    parts.push(`<div class="tool-now"><span class="spin"></span>${tool.kind ? `<span class="k">${esc(tool.kind)}</span>` : ""}<span class="nm">${esc(tool.name)}</span></div>`);
  }
  if (plan.length) parts.push(`<ol class="plan">${plan.map((s) => `<li class="${esc(s.status)}">${esc(s.content)}</li>`).join("")}</ol>`);
  if (recent.length) {
    parts.push(`<ul class="tools">${recent.map((t) => `<li class="tool">${statusIcon(t.status === "failed" ? "failed" : t.status === "running" ? "running" : "ok")}${t.kind ? `<span class="k">${esc(t.kind)}</span>` : ""}<span class="nm">${esc(t.name)}</span></li>`).join("")}</ul>`);
  }
  if (p.text) parts.push(`<div class="narr">${esc(clip(p.text, 300))}</div>`);
  if (!e.progress && e.to_kind !== "acp") parts.push(`<div class="quiet">This delegate doesn't stream its steps. It shows here until it answers.</div>`);
  parts.push("</div>");
  return `<section class="sec"><h3>Now</h3>${parts.join("")}</section>`;
}

function histRow(r) {
  const st = r.origin === "background" && !r.duration_ms && r.outcome === "ok" ? "dispatched" : r.outcome;
  const meta = [r.duration_ms ? dur(r.duration_ms / 1000) : "", r.cost_usd != null ? money(Number(r.cost_usd)) : "", ago(r.at)].filter(Boolean).join(" · ");
  return `<li>${statusIcon(st)}<span class="w">${esc(clip(r.what, 220)) || "<span class=quiet>(no description)</span>"}</span><span class="m">${esc(meta)}</span>${r.error ? `<span class="err-line">${esc(clip(r.error, 180))}</span>` : ""}</li>`;
}

function factsSection(n) {
  const rows = [];
  const add = (k, v, mono) => { if (v) rows.push(`<dt>${esc(k)}</dt><dd${mono ? ' class="mono"' : ""}>${esc(v)}</dd>`); };
  add("Role", n.role || n.desc);
  add("URL", n.url, true);
  add("Endpoint", n.endpoint, true);
  add("Model", n.model, true);
  add("Command", n.command, true);
  add("Workdir", n.workdir, true);
  add("Version", n.version);
  add("Via", n.via);
  // Why a node is down, and how it's wired: the old detail panel's health + edge rows.
  if (Number.isFinite(n.latency_ms)) add("Latency", n.latency_ms + " ms");
  if (Number.isFinite(n.checked_at)) add("Checked", ago(n.checked_at));
  add("Error", n.error);
  const edges = TOPO?.edges || [];
  if (n.kind === "agent" || n.kind === "self") {
    const out = edges.filter((e) => e.from === n.id).length;
    add("Delegates", out ? out + " outbound" : n.kind === "self" ? "0" : "unknown (no token)");
  }
  const ins = edges.filter((e) => e.to === n.id).length;
  add("Delegated by", ins + (ins === 1 ? " agent" : " agents"));
  if (!rows.length) return "";
  return `<section class="sec"><h3>Details</h3><dl class="facts">${rows.join("")}</dl></section>`;
}
function tickDrawer() {
  for (const s of document.querySelectorAll("#d-body [data-start]")) s.textContent = dur(serverNow() - Number(s.dataset.start));
}

// ── live edges: the bus + a reconcile poll ──────────────────────────────────────────
function upsertLive(d) {
  const prev = LIVE.get(d.id) || {};
  LIVE.set(d.id, { ...prev, ...d, status: prev.status && prev.status !== "running" ? prev.status : "running" });
}
function finishLive(d) {
  const e = LIVE.get(d.id);
  // Idempotent: a frame and a reconcile can both report the same end.
  if (!e || e.status !== "running") return;
  // A member's edge that ended between polls carries no duration: it ran from its start until now.
  const ms = d.duration_ms != null ? d.duration_ms : e.started_at ? Math.max(0, (serverNow() - Number(e.started_at)) * 1000) : null;
  Object.assign(e, { status: d.outcome || "ok", duration_ms: ms, cost_usd: d.cost_usd, error: d.error, endedAt: now() });
  // The finished edge is recorded now — add it to the history the feed + drawer read,
  // without waiting for the next ledger poll.
  RECENT.unshift({ edge_id: "live-" + e.id, _live: e.id, at: new Date().toISOString(), from_agent: e.from, to_kind: e.to_kind,
    to_name: e.to_name, what: e.what, outcome: e.status, duration_ms: e.duration_ms, cost_usd: e.cost_usd, error: e.error, origin: e.origin });
  if (GRAPH) {
    const n = targetOf(e.to_kind, e.to_name, GRAPH.index, e.owner && GRAPH.nodes.has(e.owner) ? e.owner : SELF);
    // A background job's row is written when it is spawned, so the snapshot already counts it.
    if (!n.synthetic && e.origin !== "background") {
      // The snapshot has absorbed this run once its count reaches what it was at landing,
      // plus every run still waiting on this node, plus this one.
      const waiting = [...LANDED.values()].filter((l) => l.node === n.id).length;
      const seen = (TOPO?.nodes || []).find((x) => x.id === n.id)?.work?.dispatches || 0;
      LANDED.set(e.id, { node: n.id, outcome: e.status, ms: e.duration_ms, cost: e.cost_usd, at: serverNow(), upTo: seen + waiting + 1 });
    }
    if (LANDED.size > 200) LANDED.delete(LANDED.keys().next().value);
  }
  landNode(e);
  setTimeout(() => { e.gone = true; render(); }, FINISH_LINGER_MS - 900);
  setTimeout(() => { LIVE.delete(e.id); render(); renderFeed(); renderDrawer(); }, FINISH_LINGER_MS);
}
function landNode(e) {
  if (!GRAPH) return;
  const n = targetOf(e.to_kind, e.to_name, GRAPH.index, e.owner && GRAPH.nodes.has(e.owner) ? e.owner : SELF);
  const h = NODE_EL.get(n.id);
  if (!h || REDUCED) return;
  const cls = e.status === "failed" ? "landed-failed" : "landed-ok";
  h.g.classList.remove("landed-ok", "landed-failed");
  void h.g.getBBox();
  h.g.classList.add(cls);
  setTimeout(() => h.g.classList.remove(cls), 1500);
}

function onEvent(topic, d) {
  if (!d || !d.id) return;
  setLive("live");
  if (topic === "delegation.started") { FRESH.add(d.id); upsertLive({ ...d, owner: SELF }); }
  else if (topic === "delegation.progress") {
    const e = LIVE.get(d.id);
    if (!e) return;
    e.progress = d.progress;
  } else if (topic === "delegation.finished") finishLive(d);
  else return;
  render();
  renderFeed();
  if (SEL) renderDrawer();
}

async function loadLive() {
  // Only an edge we already knew BEFORE asking may be concluded "ended" by its absence:
  // one whose `started` frame arrived while the (fleet-wide, slower) request was out is
  // simply newer than the answer, not finished.
  const known = new Set([...LIVE.values()].filter((e) => e.status === "running").map((e) => e.id));
  // The fleet fan-in: this agent's in-flight work plus every readable peer's, each tagged
  // with its owner node. The bus only carries this agent's own frames.
  const d = await api("/api/plugins/orgchart/live");
  if (typeof d.now === "number") skew = d.now - now();
  PEERS = d.peers || 0;
  const seen = new Set();
  for (const e of d.edges || []) {
    seen.add(e.id);
    upsertLive(e);
  }
  // A running edge the server no longer has ended while we weren't listening.
  for (const id of known) if (!seen.has(id)) finishLive({ id, outcome: "ok" });
}
async function loadRecent() {
  const d = await api("/api/ledger?limit=200");
  const live = RECENT.filter((r) => r._live && LIVE.has(r._live));
  RECENT = [...live, ...(d.edges || [])];
}
async function loadTopo(force) {
  const d = await api("/api/plugins/orgchart/topology" + (force ? "?refresh=1" : ""));
  TOPO = d;
  SELF = d.self || (d.nodes || []).find((n) => n.kind === "self")?.id || "";
  if (d.poll_s) POLL_S = d.poll_s;
  renderFilters();
  if (d.stale && !staleRetry) {
    staleRetry = setTimeout(() => { staleRetry = 0; loadTopo().then(render).catch(() => {}); }, 2500);
  }
}

function setLive(state) {
  const pill = $("live");
  if (pill.dataset.state === state) return;
  pill.dataset.state = state;
  $("live-label").textContent = { live: "Live", polling: "Polling", connecting: "Connecting", off: "Offline" }[state];
}

function subscribe() {
  addEventListener("message", (e) => {
    if (e.source !== window.parent || e.data?.type !== "protoagent:event") return;
    if (typeof e.data.seq === "number") {
      // A bus that restarted counts from zero again: a big step backwards is a reset, not a replay.
      if (e.data.seq <= lastSeq && lastSeq - e.data.seq < 64) return;
      lastSeq = e.data.seq;
    }
    onEvent(e.data.topic, e.data.data);
  });
  // `background: true` keeps the chart mounted while another view is up, so it is current
  // the moment the operator comes back to it mid-run.
  parent.postMessage({ type: "protoagent:subscribe", patterns: ["delegation.#"], background: true }, "*");
  setLive("live");
}

// ── boot + loops ────────────────────────────────────────────────────────────────────
let booted = false;
async function boot() {
  if (booted) return;
  booted = true;
  try {
    await Promise.all([loadTopo(), loadLive(), loadRecent()]);
    showError("");
  } catch (e) {
    showError("Couldn't load the org chart: " + e.message);
  }
  render();
  renderFeed();
  subscribe();
  setInterval(() => { tickFeed(); tickDrawer(); tickNodes(); }, 1000);
  // Reconcile: frames are live-only, so a missed one (a relay hiccup, a hidden tab) is
  // corrected here. Fast while anything runs, slow when idle.
  let last = 0;
  setInterval(async () => {
    if (document.hidden) return;
    const busy = [...LIVE.values()].some((e) => e.status === "running");
    // Peers' work only arrives this way, so a fleet view polls briskly; a lone agent has
    // the bus and only needs this to catch a dropped frame.
    if (Date.now() - last < (PEERS ? 2500 : busy ? 4000 : 12000)) return;
    last = Date.now();
    try { await loadLive(); render(); renderFeed(); if (SEL) renderDrawer(); }
    catch { setLive("off"); }
  }, 1000);
  setInterval(async () => {
    if (document.hidden) return;
    try { await loadTopo(); render(); renderDrawer(); } catch { /* keep the last picture */ }
  }, POLL_S * 1000);
  setInterval(async () => {
    if (document.hidden) return;
    try { await loadRecent(); renderStats(); renderFeed(); if (SEL) renderDrawer(); } catch { /* ditto */ }
  }, 20000);
  document.addEventListener("visibilitychange", async () => {
    if (document.hidden) return;
    try { await Promise.all([loadLive(), loadRecent()]); render(); renderFeed(); if (SEL) renderDrawer(); } catch { /* next tick */ }
  });
}
function tickNodes() {
  for (const c of document.querySelectorAll(".node .clock[data-start]")) c.textContent = dur(serverNow() - Number(c.dataset.start));
}

$("refresh").addEventListener("click", async () => {
  const b = $("refresh");
  b.disabled = true;
  try { await Promise.all([loadTopo(true), loadLive(), loadRecent()]); render(); renderFeed(); showError(""); }
  catch (e) { showError("Refresh failed: " + e.message); }
  finally { b.disabled = false; }
});

kit.initPluginView?.(boot);
setTimeout(boot, 800);
