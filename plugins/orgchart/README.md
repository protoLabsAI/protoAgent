# orgChart

A console rail view that renders a **live diagram of the agent fleet and the work moving
through it**: every delegate is a node, a directed edge A→B means "A can delegate to B", a
dashed edge means "A supervises B" (a hub-registered fleet member). The ring is liveness
(green up, red down/unreachable, grey not probed yet); the bar under a node is its record
(runs that succeeded vs failed).

**While work runs, you watch it happen.** An edge carrying a delegation animates, with a
spark travelling along it; the target lights up with what it is doing right now ("Editing
calc.py") and a clock; when it lands, the node flashes green or red and its stats tick. An
**Activity** column lists every hand-off, in flight and recent. Click any node for its
drawer: **Now** (the task, the coder's plan, its current tool, recent tools and narration —
streamed as it works), **Lifetime** stats, **Recent work**, and details. The drawer is
non-modal and sits beside the graph, so the work stays in view while you inspect it.

## Where the live picture comes from

- **This agent's work** — the delegation ledger's open edges (`graph/ledger.py`): every
  dispatch funnel opens an edge before it runs and announces `delegation.started` →
  `delegation.progress` → `delegation.finished` on the bus. The view subscribes to
  `delegation.#` (and stays subscribed in the background), catching up from
  `GET /api/ledger/live` when it opens mid-run.
- **The fleet's work** — the bus only carries this agent's events, so `GET
  /api/plugins/orgchart/live` fans in each readable peer's `/api/ledger/live` (the peers the
  crawl already holds tokens for — never a new one), tagging every edge with the node that
  owns it. A member's own coder lights up under that member, not under ours.
- **Records** — each node's stats are its **owner's** ledger: ours for our delegates, each
  readable peer's (`/api/ledger/edges`) for its own. A member's `claude-code` is a
  different process from ours and never wears our run count. The drawer's history for a
  peer-owned node is read through `GET /api/plugins/orgchart/history?owner=…`.

## Active / All mode

Use the **Active / All** switch in the chart header to choose how much of the fleet
to show. **Active** is the default; **All** shows the full chart, subject to the
kind-filter chips. Press **`a`** to toggle modes while focused in the chart view
(outside inputs and editable fields). The choice persists per browser in
localStorage under `orgchart.mode` (`active` or `all`).

Active keeps:

- **Self** — the agent hosting the chart.
- Nodes touched by **running or lingering flows**, at either end of the flow.
- Nodes whose **`work.last_at` is within the past 5 minutes**, so recently finished
  work stays visible after its flow clears.
- The **selected node**.
- Their **ancestors back to self** along the chart's spanning tree. A running
  peer-owned coder therefore keeps its peer visible too.

The **kind-filter chips apply on top** of the mode: a hidden kind stays hidden in
both Active and All. In Active, the header shows **“N of M shown · show all”** when
there are delegates remaining after the kind filters. The count excludes self:
N is the number kept by Active, and M is the number available before the Active
filter. Click **show all** to switch to All without changing the kind filters.

When Active has only self left, no running or lingering flows, and recent handoff
history, its empty state says **“Nothing working right now”**, names the last
handoff's target, outcome and age when available, and offers **Show all**. Without
recent history, it shows **“Nothing delegated yet”** instead. If kind filters are
set and the topology contains delegates, the empty state takes precedence as
**“Everything is filtered out”**, with a prompt to turn a node type back on.

## The three kinds of node

All three delegate types are drawn, and the chip row above the chart hides or shows a
class of them (the choice sticks per browser):

| Badge | Type | What it is | Crawled? |
|---|---|---|---|
| — | `a2a` | A peer agent | **Yes** — its card and its own delegate list |
| `ACP` | `acp` | A CLI coding agent (protoCLI, Claude Code) | No — a **leaf** |
| `MODEL` | `openai` | An OpenAI-compatible model endpoint | No — a **leaf** |

A leaf serves no agent card and no `/api/delegates`, so it is never contacted from here:
its status is **its owner's** probe result — our delegates health snapshot for our own,
the `health` the peer's `/api/delegates` already reports for a peer's. Nothing has probed
it yet ⇒ `up: null` ⇒ grey "not probed yet", which is a different claim from red. When a
leaf is red, the panel shows the owner's **error** — the fastest way to see that a coder's
binary left `PATH`.

**Identity differs by kind, on purpose.** An ACP coder is a subprocess on *its owner's*
box, so it's owner-scoped (`acp:<owner>#<name>`) and the same name on two agents is two
nodes. A model endpoint is shared infrastructure, so it merges by endpoint + model
(`model:<host><path>/<model>`) and several agents pointing at one model converge on a
single node — which is what makes "who shares this model" answerable at a glance.

## What it shows

Delegation is **outbound-only and per-agent** — there's no fleet-wide registry — so
orgChart **crawls** from the agent it runs on:

1. Reads this agent's **effective roster** via the delegates store (ADR 0105): its own
   `delegates:` ∪ the box's fleet-shared (`scope: host`) entries, secrets overlaid —
   so a coder registered once on the hub appears from every member.
2. On a hub, also folds in **supervised remote members** (dashed edges), whose stored
   bearers widen the crawl even where no delegate edge exists.
3. For each A2A peer it holds a token for, fetches the peer's public **agent card**
   (identity) and its **`/api/delegates`** (the peer's own edges). Direct delegates'
   liveness/latency comes from the delegates plugin's **cached health snapshot** — the
   background prober that already maintains it — never a duplicate probe.
4. Peers it holds no token for (a delegate-of-a-delegate) still appear as **leaf
   nodes** with identity from the public card; unreachable peers show red.
5. Every crawled peer's **acp and openai delegates** join as leaves too, so the chart
   covers the coders and models the fleet works through — not only the agents it talks to.

Best run on your **orchestrator / org-head** agent, which holds the peer tokens and so
renders the widest view of the fleet.

## Why it's fast

- Every peer read is **TTL-cached** — cards 5 min, delegate lists 60 s, and
  *unreachable* peers are remembered for 30 s, so a dead node can't stall every reload.
- Probes and peer-delegate fetches run **concurrently per BFS layer**.
- The assembled topology serves **stale-while-revalidate**: a stale snapshot returns
  instantly (marked `stale`, the view shows "refreshing") while a single-flight
  re-crawl runs in the background. The ⟳ button forces a full inline re-crawl.
- The view only polls while its tab is visible.

**Tokens never reach the browser** — the crawl runs entirely server-side in the gated
`/api/plugins/orgchart/topology` route; the page only ever sees names, roles, health,
and edges. Every display field is copied **by name**, never by spreading the raw entry:
`merged_delegates()` hands back the roster with secrets *overlaid* (`api_key`,
`auth.token`), so a node built from the raw dict would ship them to the page.

## Enable

```yaml
# langgraph-config.yaml
plugins:
  enabled: [..., orgchart]
```

Drop the plugin dir in `plugins/` (bundled) or `<config>/plugins/`, restart, and an
**Org Chart** item appears in the console rail. No build step — the view is authored as
`view/orgchart.{html,css,js}` and served as ONE self-contained page (the CSS and JS are
inlined at request time: split-out sub-resources would need `public_paths` or 401 behind
the bearer gate).

Tunables live in **Settings → Org Chart**: cache TTL, poll interval, probe timeout,
max nodes, whether supervised fleet members are drawn, and whether coding agents and
model endpoints are included at all. Turning a class off there removes it from the crawl
result entirely; the chips only hide what the server already sent, and the chip row says
so when a class is excluded in Settings.

## Layout

- `protoagent.plugin.yaml` — manifest (`views:` declares the rail item; `config`/`settings`).
- `__init__.py` — `register()` mounts the two routers (public page, gated data).
- `topology.py` — the crawl engine: sources, caches, BFS, stale-while-revalidate.
- `view.py` — the two routers (`/view`; `/topology`, `/live`, `/history`).
- `view/` — the page: `orgchart.html` (skeleton), `orgchart.css` (tokens only, with a
  reduced-motion branch), `orgchart.js` (a keyed SVG whose node moves, edges and sparks
  all run in one rAF loop that stops when nothing moves; BFS-layered so delegation cycles
  render fine).

Tests: `tests/test_orgchart_plugin.py`, `tests/test_orgchart_ledger.py`, and the live
ledger in `tests/test_ledger_live.py`.
