# Goal trust-gate — operator-vs-federation token model (design note, DRAFT for decision)

**Status:** Draft / needs a decision. Promote to an ADR once a direction is chosen.
**Source:** 2026-06-28 antagonistic review (goal trust-gate, High) + a 5-proposal design
panel with adversarial red-team (2026-06-29).

## The problem

A semi-trusted A2A **federation peer** (or a `/v1` client) can send a chat message
`/goal {"condition":"…","verifier":{"type":"command","command":"…"}}`. `GoalController.
parse_control` (`graph/goals/controller.py`) parses it and sets the goal; a `command`/
`test`/`ci` verifier **shells out on the host** = remote code execution. We must let the
**trusted local operator** set these (a legitimate feature) while refusing a federation
peer / external API client. Today nothing distinguishes them.

## Why it's hard (the crux)

- `a2a_impl/auth.py` gates **every** non-public path with a **single** bearer (`auth.token`
  / `A2A_AUTH_TOKEN`), default-deny. `/a2a`, `/api/*`, `/v1/*` all share that one secret.
- The operator's own console **streaming chat goes over `/a2a` with that same bearer**
  (ADR 0045 "A2A canonical", `apps/web/src/lib/api.ts`). So console-vs-peer is
  indistinguishable by **code-path or token**.
- `set_goal_safe` already gates the *programmatic* path (tool/plugin/REST) to plugin-only;
  the gap is `parse_control` (the `/goal` **chat message**), reached from the A2A stream
  handler (`server/chat.py:692`) and the console+`/v1` handler (`:1036`).
- The `data`-verifier eval escape is already closed (AST-validated, #1401), so only
  `command`/`test`/`ci` shell out today — but see red-team finding R2.

## What the design panel + red-team established (the load-bearing findings)

These are the constraints **any** implementation must honor — every proposal that ignored
one got a `critical`/`high` red-team verdict:

- **R1 — Gate the SURFACE, not just `/goal`.** The same federation token also gates
  `/api/plugins/install` (host code-exec), config/SOUL rewrite, and subagent runs. Refusing
  the `command` verifier while still letting a "federation" credential reach `/api/*` is
  theatre — it has RCE via plugin-install anyway. **A federation credential must be denied
  the `/api` operator surface entirely (a path/tier ceiling), not just the verifier.**
- **R2 — Allow-list, not deny-list.** Gate by the *complement*: an untrusted caller may set
  only `{plugin, llm}` (and `data` with `contains`, a pure substring check) — never
  `command/test/ci`, and not `data` with `expr` (still a restricted-eval surface). Reuse
  `controller.SAFE_PROGRAMMATIC_VERIFIERS`. A deny-list silently re-opens on any new verifier
  type.
- **R3 — Fail CLOSED once opted in.** In two-token mode an unclassified/ambiguous request
  must default to **federation** (least privilege). Single-token mode stays operator
  (byte-for-byte backward-compat).
- **R4 — Don't trust a cross-task `contextvar`.** Starlette `BaseHTTPMiddleware` →
  streaming-producer-task propagation is fragile. Thread the server-stamped trust level
  through the **proven `request_metadata` plumbing** (`server/chat.py` already threads it
  from the route to the handler), not a contextvar.
- **R5 — Classification is server-side only.** Trust = *which configured secret the inbound
  bearer matched* (constant-time `hmac.compare_digest`), never the path, an Origin header,
  loopback, or A2A message metadata (all caller-forgeable). The fleet hub→member proxy and a
  remote operator console both arrive over the network with a bearer — token identity is the
  only authority.
- **R6 — "Adding a token" protects nothing until peers rotate.** Existing peers still hold
  the operator token. This is inherent to backward-compat; mitigate with a documented
  rotation and (optionally) a `require_federation_token` enforce flag.

## Recommendation — phased

### Phase 1 — ship now, **no token model needed** (closes the RCE-via-chat hole)

The panel's best-scored proposal (chat-always-untrusted; both red-teams: "Layer 1 is
genuinely sound") shows the immediate fix needs no auth change at all:

> **`parse_control` refuses `command`/`test`/`ci`/`data-expr` verifiers from a `/goal`
> *chat message* for *every* caller** (allow-list R2), because both call sites funnel
> through the one method. Status/clear and `{plugin, llm, data-contains}` still work.

This closes the federation-peer RCE-via-chat for everyone **immediately**, with a small,
sound diff. **Cost:** the operator can no longer set a `command`/`test`/`ci` verifier via
the chat box — they set it via a dedicated operator channel (Phase 2). For most operators
that is an acceptable, even desirable, tightening (the chat box was never a great place to
arm a shell verifier). Add a `trusted: bool = True` parameter now (defaulting trusted) so
Phase 2 can flip the operator path back on without re-touching the call sites.

### Phase 2 — restore the operator's full power safely (the actual token decision)

Two viable shapes (pick one; both honor R1–R6):

- **Option A — Two-token + path ceiling.** Add `auth.federation_token`. The middleware
  classifies each request by which token matched and stamps `operator|federation` into
  `request_metadata` (R4/R5). `effective_trust = min(credential_tier, path_ceiling)`: the
  **`/api/*` operator surface requires `operator`** (R1 — a federation token cannot install
  plugins / rewrite config); `/a2a` + `/v1` accept either but carry the tier to
  `parse_control`. Operator keeps setting `command` verifiers via console `/goal` over `/a2a`
  (operator token → trusted). Single-token / open mode → everyone is `operator` (R3
  back-compat). **Pro:** preserves the operator's chat-`/goal` UX. **Con:** the bigger change
  (two tokens, path ceiling, peer re-tokening).
- **Option B — Dedicated operator channel.** Keep Phase 1's universal chat refusal; add a
  privileged **`/api/goals` set-endpoint (+ CLI)** gated to operator-tier (and gate the whole
  host-exec `/api` surface to operator-tier per R1). Dangerous verifiers are set there, never
  via chat. **Pro:** simplest trust model (chat is always untrusted). **Con:** requires
  building the console Goals-panel set path (there is none today) and a small operator-UX
  shift.

**Lead recommendation:** **Phase 1 now** (it is the real mitigation and needs no token
decision), then **Phase 2 Option A** if preserving console chat-`/goal` for dangerous
verifiers matters, else **Option B** for the simpler model. Either Phase 2 must also apply
the **R1 path ceiling** (federation/non-operator credentials denied the `/api` operator
surface) — without that, the token split is cosmetic.

## Decision points for the user

1. Ship **Phase 1** (universal chat refusal of `command`/`test`/`ci`/`data-expr`) now? (Recommended — it closes the hole regardless of the Phase 2 choice.)
2. Is preserving the operator's ability to arm a `command` verifier **from the chat box**
   worth the two-token model (**Option A**), or is a dedicated Goals-panel/CLI channel
   (**Option B**) acceptable?
3. Confirm the **R1 path ceiling**: should a configured federation token be **denied** the
   `/api/*` operator surface (plugin-install, config rewrite, subagent runs)? (Strongly
   recommended — otherwise the gate is bypassable.)

Full proposals + red-team transcripts: workflow `token-model-design-panel` (2026-06-29).
