# ADR 0122 — Subscription usage model and routing

- Status: Accepted (2026-10-21, Josh)
- Date: 2026-10-21
- Epic: #4309 (subscription usage model and routing). This ADR is slice U1 (#4430);
  U2–U9 and the board's B1–B7 implement it.
- Builds on: [ADR 0097](./0097-native-oauth-subscription-providers.md) (the native
  OAuth-subscription accounts the usage belongs to), [ADR 0106](./0106-provider-registry.md)
  (providers are a registry; nothing here adds a provider config field),
  [ADR 0075](./0075-external-interfaces-cli-mcp-api.md) (the CLI/REST/MCP seam the usage
  surface rides), [ADR 0113](./0113-agent-pairing-for-remote-fleet-members.md) (the paired
  bearer the fleet usage fans out over).
- Related: projectBoard-plugin#553 (the board's vendor-named routing vocabulary; folded into
  the epic as Phase 2).

## Context

This week's fleet work hit the same problem three ways:

- **Cost steering is manual.** Ladders get hand-edited (`smart: [codex, sonnet]`), and nothing
  says when to do it or undo it.
- **Limits fail late and look like other failures.** A quota hit surfaces as a retry or a
  blocked card; operators find out from a stalled board, not from a number.
- **There is nowhere to look.** Several fleet agents share one account, and no surface
  answers "how much of the plan is left, and when does the window reset?"

The board's routing vocabulary is also vendor-named (`opus` as a tier) and cannot express
"Codex only" (projectBoard-plugin#553).

Both providers expose usage, but through undocumented, internal interfaces whose keys drift.
What a 2026-10-21 live spike found:

| | Claude (`anthropic-oauth`) | ChatGPT/Codex (`openai-codex`) |
| --- | --- | --- |
| Usage endpoint | `GET api.anthropic.com/api/oauth/usage` with `anthropic-beta: oauth-2025-04-20` — what Claude Code's `/usage` calls | `GET chatgpt.com/backend-api/wham/usage` plus `ChatGPT-Account-Id` — what codex `/status` reads |
| Best fields | `limits[]`: `{kind, percent, severity, resets_at, scope.model}`, including a model-scoped weekly (Fable at 50% of weekly) | `rate_limit.{primary,secondary}_window` plus per-model buckets in `additional_rate_limits` |
| Free passive signal | `anthropic-ratelimit-unified-{5h,7d}-{utilization,reset,status}` on every `/v1/messages` response | `x-codex-{primary,secondary}-{used-percent,window-minutes,reset-at}` on every `/codex/responses` response |
| ACP | — | `_meta.quota` carries token counts only, no plan windows |
| Status | Undocumented; first-party client uses it | Internal, reverse-engineered; keys drift between releases |
| Token rule | Access token ~8 h; refresh token is **single-use**, so on a 401 re-read the Keychain, never refresh (it races Claude Code, cf. #2441) | Same single-use refresh hazard; re-read the file |

**Window shapes drift too.** Windows vary by plan and over time — Codex prolite moved from
5h+weekly to weekly-only in September — so the model must hold N windows per account, plus
scoped buckets.

Because both endpoints are undocumented and their keys drift, the design is built around one
invariant that the whole feature hangs on:

> Missing, stale, or unparseable usage is **unknown**, and unknown routes exactly as today.

## Decision

### D1 — Two sources, one cache

Each account's usage is read from two sources into one cache:

- **The endpoint is authoritative.** Poll each account's usage endpoint at most once every
  60 s, and only while something is reading it (a console view, the CLI, the board loop).
- **The headers are fresher.** Between polls, update the cache from the response headers on
  calls we already make (D6's hooks). Zero extra requests.

The newest `fetched_at` wins per window. If a poll of the usage endpoint comes back `429`,
back off polling that account to every 5 minutes. The spike did not test for a
provider-side 429 on polling; this keeps the volume flat if one appears.

### D2 — A normalized Account/window model

```
Account {
  provider_id, provider_type, account_key, plan,
  source, fetched_at, stale_after,
  windows[], credits
}

Window {
  id,
  kind: session | weekly | monthly | other,
  window_seconds,
  used_pct 0-100,
  resets_at            # UTC
  status: ok | warning | blocked,
  scope { model?, bucket? } | null,
  binding
}
```

A model is **usable** when no window that applies to it is `blocked`. Unscoped windows apply
to every model; scoped windows apply only to their model. This is what keeps a Fable-scoped
block from looking like an Opus block.

### D3 — Account identity

`account_key` is the Claude org/account UUID or the ChatGPT account id, already resolved in
`src/graph/providers/oauth.py`. A `Provider` gains no new config field (ADR 0106 holds). The
fleet view dedupes by `account_key`, so five members on one Max plan show one meter and one
poller.

### D4 — Best-effort by contract

Every reader treats "no data / stale / parse error" as **unknown**, and unknown routes
exactly as today (the invariant). Fetchers are tolerant parsers, each with a recorded raw
sample in tests. A schema change degrades to unknown plus one logged warning — never an
exception. Golden samples from the redacted spike payloads (including a Fable-scoped window
and a Codex weekly-only account) are what catch key drift in CI.

### D5 — Credentials: read-only, never refresh

Fetchers reuse `resolve_*_oauth`, **never trigger a refresh**, re-read the token store once
on a 401, and then report unknown. A refresh token is single-use, so a refresh from a usage
fetcher would race Claude Code and the codex CLI and kill one of their logins (cf. #2441).
`oauth_status` stays side-effect-free, and usage is a separate call from any model call.

**Implementation constraint found while boarding:** today's `resolve_anthropic_oauth()` and
`resolve_codex_oauth()` (`src/graph/providers/oauth.py`) refresh the token when it is near
expiry. The usage fetchers therefore need a **non-refreshing read path** — the same
resolution, minus the refresh step — rather than reusing those functions as-is.

### D6 — Where things live (layering)

| Concern | Home |
| --- | --- |
| Fetchers (tolerant parsers) and header hooks | `graph/providers/usage/` |
| Cache / store (in-memory + last snapshot per instance) | `observability/usage_store.py` |
| Shared operation for REST, CLI and MCP | one `ops.usage` op |
| REST routes | `operator_api/usage_routes.py` |
| Live updates to the console | `HOST.publish("provider.usage")` |
| Plugin / SDK read path | `graph.sdk.provider_usage()` |

This keeps the layering contract: the fetchers and store live in the core packages, the REST
routes in `operator_api/`, and the CLI/MCP adapters call the shared `ops.usage` op (ADR
0075). Fleet usage is **operator tier only**.

### D7 — Delegates map to accounts

An ACP coder burns the account of the CLI login it runs under. A delegate gains an optional
`account:` (a provider id). It is inferred for `claude-agent-acp` → the `anthropic-oauth`
account and `codex-acp` → `openai-codex`, and can be set explicitly when a delegate runs a
different login.

### D8 — Policy lives with the router

The board's routing policy (allowed delegates, strict mode, headroom thresholds) lives in
projectBoard config, editable live. Core's quota-aware failover lives in `routing:`. The two
never cross the layering line: the board reads usage through `graph.sdk.provider_usage()`,
and core failover reads its own store.

**Decided defaults** (Josh, 2026-10-21):

- **Warn at 80%** of a window.
- **Prefer a sibling at or above 90%.**
- A `blocked` window is **down until `resets_at`**.

## Slices and ordering

The epic (#4309) owns the slices; this ADR is U1. They order as:

```
U1 → U2 → {U3, U4, U5, U7}      U5 → U6, U9      U7 → {B5, B6, U8}
B1 → {B2, B3} → B4               {B1, B2, U7} → B5 → B7
```

- **U2** fetchers + cache + `GET /api/usage`; **U3** passive header capture; **U4**
  `protoagent usage`; **U5** hub `GET /api/usage/fleet`; **U6** connections-panel meter +
  live topic; **U7** `graph.sdk.provider_usage()` + delegate→account mapping; **U8**
  quota-aware failover in core; **U9** attribution (`usage --by-agent`).
- **B1–B7** live in projectBoard-plugin (neutral tiers, strict mode, live reload, routing
  control, headroom preference, reset-aware cooldown, usage policy).

Phases 1 (U) and 2 (B) run in parallel — different repos, no shared files. Phase 3 joins
them.

## Documentation

The ADR itself is U1's deliverable. The slices that ship behaviour each update these pages:

| Slice | Pages it adds or changes |
| --- | --- |
| U2 | `docs/reference/operator-api.md`: `GET /api/usage`. `docs/explanation/security-and-trust.md`: the unknown-invariant and no-refresh rule. |
| U4 | `docs/guides/` (a `protoagent usage` section): flags, exit code 2, `--fleet`. |
| U5 | `docs/reference/operator-api.md`: `GET /api/usage/fleet`, operator tier only. |
| U6 | `docs/guides/model-connections.md`: the account meter and its four states. |
| U8 | `docs/reference/configuration.md`: `routing:` failover under quota. |

## Consequences

- **One number to watch.** The plan's headroom and reset time are visible in a command, the
  console, and across the fleet, from one cache.
- **Routing can steer by headroom.** The board prefers a sibling and rotates before a hard
  failure; core fails over only across accounts.
- **Nothing changes when data is missing.** Unknown routes exactly as today, so the feature
  is safe to ship even on a provider that changes its keys tomorrow.
- **Polling stays cheap and non-disruptive.** On-demand only, header updates between polls,
  a 5-minute backoff on 429, and one poller per `account_key`.
- **No new provider config.** ADR 0106's registry is unchanged; usage is read, not
  configured.
- **The credential surface widens by one read path.** The fetchers need a non-refreshing
  resolver (D5); the refresh path and its single-use hazard are untouched.

## Resolved 2026-10-21 (Josh)

Josh accepted this ADR with every open question resolved:

1. **Tiers** are `standard / reasoning / advanced` (B1).
2. **Thresholds:** warn 80%, prefer sibling 90%, `blocked` = down until `resets_at`.
3. **Per-agent attribution** (U9) stays in the epic, scheduled last.
4. **Fleet usage** is operator tier only.