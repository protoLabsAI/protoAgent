# orgChart

A console rail view that renders a **live diagram of the agent fleet**: every delegate is
a node, a directed edge A→B means "A can delegate to B", a dashed edge means "A
supervises B" (a hub-registered fleet member). Green = up, red = down/unreachable, grey =
not probed yet, the accent-outlined node is the agent you're looking from. Click any node
for details.

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
**Org Chart** item appears in the console rail. No build step — the view is one
self-contained HTML page (kept single-file on purpose: split-out `.js` would need
`public_paths` or 401 behind the bearer gate).

Tunables live in **Settings → Org Chart**: cache TTL, poll interval, probe timeout,
max nodes, whether supervised fleet members are drawn, and whether coding agents and
model endpoints are included at all. Turning a class off there removes it from the crawl
result entirely; the chips only hide what the server already sent, and the chip row says
so when a class is excluded in Settings.

## Layout

- `protoagent.plugin.yaml` — manifest (`views:` declares the rail item; `config`/`settings`).
- `__init__.py` — `register()` mounts the two routers (public page, gated data).
- `topology.py` — the crawl engine: sources, caches, BFS, stale-while-revalidate.
- `view.py` — the two routers.
- `view.html` — the page (inline SVG, BFS-layered so delegation cycles render fine).

Tests: `tests/test_orgchart_plugin.py`.
