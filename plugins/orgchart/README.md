# orgChart

A console rail view that renders a **live diagram of the agent fleet**: every agent is
a node, and a directed edge A→B means "A can delegate to B". Green = up, red =
down/unreachable, the accent-outlined node is the agent you're looking from.

![rail: Org Chart]

## What it shows

Delegation is **outbound-only and per-agent** — an agent declares its own delegates in
config, and there's no fleet-wide registry. So orgChart **crawls** from the agent it runs
on:

1. Reads this agent's own delegates (from config — the raw block, so it keeps
   `credentialsEnv` and can resolve each peer's token).
2. For each direct A2A peer, fetches its public **agent card** + **`/healthz`** (identity +
   liveness) and its **`/api/delegates`** (the peer's own edges), using the token this
   agent already holds for it.
3. Peers it holds no token for (a delegate-of-a-delegate, an agent on another hub's
   loopback) still appear — as **leaf nodes** with identity from the public card — you just
   won't see their outbound edges. Unreachable peers show red.

The result is a two-level (or deeper, where tokens chain) delegation graph centred on the
host agent. Best run on your **orchestrator / org-head** agent, which holds the peer tokens
and so renders the widest view of the fleet.

**Tokens never reach the browser** — the crawl runs entirely server-side in the gated
`/api/plugins/orgchart/topology` route; the page only ever sees names, roles, health, and
edges.

## Enable

```yaml
# langgraph-config.yaml
plugins:
  enabled: [..., orgchart]
```

Drop the plugin dir in `plugins/` (bundled) or `<config>/plugins/`, restart, and an
**Org Chart** item appears in the console rail. No build step — the view is a single
self-contained HTML page with an inline SVG (BFS-layered so delegation cycles render
fine); it refreshes every 8s.

## Layout

- `protoagent.plugin.yaml` — manifest (`views:` declares the rail item).
- `__init__.py` — `register()` mounts the two routers.
- `view.py` — the page (`VIEW_PAGE` + `build_view_router`) and the crawl
  (`build_data_router` → `_crawl`).
