# 0066 — Goal trust-gate Phase 2: federation token + operator `/api` channel

Status: **Accepted** (Option B of the goal trust-gate design note, 2026-06-30)

> Promotes `docs/dev/notes/goal-trust-gate-token-model.md` to an ADR. Phase 1 (#1492)
> closed the RCE-via-chat hole by refusing `command`/`test`/`ci`/`data-expr` verifiers from a
> `/goal` **chat** message for every caller. Phase 2 restores the *operator's* ability to set
> those verifiers — safely — through a dedicated channel, and adds the path ceiling that makes
> a federation token meaningful. **Chosen: Option B (dedicated operator channel).**

## Context

The goal verifiers `command`/`test`/`ci` shell out on the host and `data`+`expr` hits a
restricted-eval sink — arming one is remote code execution. Phase 1 made all four
**unsettable through any exposed door**: the `/goal` chat path refuses them (`trusted=False`
from both server call sites) and the programmatic path (`set_goal_safe`) is `plugin`-only. That
closed the hole but cost the *operator* a legitimate feature ("keep going until `pytest -q`
passes").

The crux (from the design note's red-team, R1/R5): protoAgent gates `/a2a`, `/api/*`, and
`/v1/*` with a **single** bearer (`auth.token` / `A2A_AUTH_TOKEN`), default-deny — so a
semi-trusted federation peer and the trusted operator are indistinguishable by code-path or
token. And gating only the goal verifier is theatre while the same credential can `POST
/api/plugins/install` (also host code-exec). The real control is **which surface a credential
may reach**, not which verifier it may set.

## Decision — Option B

Keep chat **universally untrusted** (Phase 1 unchanged — the simplest, soundest model per both
red-teams), and give the operator a **dedicated `/api` channel** for dangerous verifiers,
protected by a real operator-vs-federation distinction.

### D1 — A second credential: `auth.federation_token`

Add an optional `federation_token` (YAML `auth.federation_token` / env `A2A_FEDERATION_TOKEN`),
alongside the existing operator bearer. The middleware classifies each request by **which
secret the inbound bearer matched** (constant-time `hmac.compare_digest`, server-side only —
never by path, Origin, or loopback, all caller-forgeable; R5). Result: tier `operator` or
`federation`.

### D2 — The R1 path ceiling

The **`/api/*` operator surface** (plugin install/enable = host code-exec, config/SOUL rewrite,
subagent runs, the operator goal set-path) **requires the operator credential**. A federation
token is denied it with `403`, even though it's a valid token. `/a2a` + `/v1` remain open to
either tier (the consumer/federation surfaces). This is the whole point: without it, a
federation credential has RCE via `/api/plugins/install` regardless of any goal gating.

The ceiling lives **entirely in the auth middleware** — a request that *reaches* an `/api`
handler is operator-tier by construction, so no per-request trust level has to be threaded into
handlers or the streaming producer (this is why Option B avoids Option A's fragile
`BaseHTTPMiddleware`→streaming-task propagation, R4).

### D3 — The operator goal channel

`POST /api/goals` (already routed) accepts **any** verifier type — `command`/`test`/`ci`/`data`
included — via a new `GoalController.set_goal_operator`. It is safe because it sits under the
`/api` ceiling: only the operator reaches it. (`set_goal_safe`, the *programmatic* agent/plugin
path, stays `plugin`-only — unchanged.) A CLI and a console Goals set-form are thin clients of
this endpoint (follow-up slices).

### D4 — Backward compatibility (R3, fail-safe)

When **no** `federation_token` is configured (the default), there is no federation tier: the
bearer check is byte-for-byte the old single-token check and the ceiling never fires. Adding a
federation token is **opt-in**. Note that in single-token mode any bearer holder already has
host code-exec via `/api/plugins/install`, so allowing `/api/goals` to set a `command` verifier
adds **no** new capability — the ceiling, not the goal endpoint, is the control. R6 caveat:
existing peers hold the *operator* token until rotated onto the federation token; "adding a
token protects nothing until peers rotate" is inherent to backward-compat and is a documented
operational step.

## Consequences

- The operator sets dangerous verifiers again — from a dedicated, operator-tier channel, never
  the chat box.
- A configured federation token is confined to `/a2a` + `/v1`; it cannot install plugins,
  rewrite config, run subagents, or set host-exec goals. The federation split stops being
  cosmetic.
- Trust model stays simple: chat is always untrusted; `/api` is always operator. No per-request
  trust threading, no contextvar fragility.
- Additive + opt-in: unset `federation_token` ⇒ zero behavior change.

## Alternatives considered

- **Option A — two-token + thread the tier into `parse_control`** so the operator arms shell
  verifiers from the chat box. Preserves chat UX, but needs the tier to survive the
  Starlette→streaming-producer hop (R4, fragile) and keeps chat trust-aware. Rejected: the chat
  box was never a good place to arm a shell command, and B's "chat always untrusted" is the
  sounder, simpler model.
- **Loosen the chat gate for a "trusted" origin/loopback.** Rejected — Origin/loopback are
  caller-forgeable (R5); trust must be the matched secret.
- **Leave Phase 1 as the final state** (dangerous verifiers unsettable forever). Rejected — a
  real operator use case (CI/test-driven goals) stays broken.

## Slices

- **PR1 (this ADR)** — D1 + D2 + D3: `federation_token` + middleware classification + `/api`
  path ceiling + the `set_goal_operator` endpoint. Held for security review.
- **PR2** — a `protomaker`/CLI `goal set` command (thin client of `POST /api/goals`).
- **PR3** — the console Goals set-form (DRAFT, local-test gate) — the set-path UI deferred from
  the goal.iteration work.

**Operational follow-ups (the federation token itself)** — management UI, peer rotation + an
optional `require_federation_token` enforce flag, fleet-member tokens, and `trust_tier`
observability — are tracked in #1504.
