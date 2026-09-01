# orgChart

A console rail view that renders a **live diagram of the agent fleet**: every agent is
a node, a directed edge A→B means "A can delegate to B", a dashed edge means "A
supervises B" (a hub-registered fleet member). Green = up, red = down/unreachable, the
accent-outlined node is the agent you're looking from. Click any node for details
(URL, version, latency, in/out degree).

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
and edges.

## Enable

```yaml
# langgraph-config.yaml
plugins:
  enabled: [..., orgchart]
```

Drop the plugin dir in `plugins/` (bundled) or `<config>/plugins/`, restart, and an
**Org Chart** item appears in the console rail. No build step — the view is one
self-contained HTML page (kept single-file on purpose: split-out `.js` would need
`public_paths` or 401 behind the bearer gate).

Tunables live in **Settings → Org Chart**: cache TTL, poll interval, probe timeout,
max nodes, and whether supervised fleet members are drawn.

## Layout

- `protoagent.plugin.yaml` — manifest (`views:` declares the rail item; `config`/`settings`).
- `__init__.py` — `register()` mounts the two routers (public page, gated data).
- `topology.py` — the crawl engine: sources, caches, BFS, stale-while-revalidate.
- `view.py` — the two routers.
- `view.html` — the page (inline SVG, BFS-layered so delegation cycles render fine).

Tests: `tests/test_orgchart_plugin.py`.
