# ADR 0124 — Per-caller run_command approval policy for A2A callers

- Status: Proposed
- Date: 2026-10-26
- Implementation: Not implemented; this change records the design only.
- Issue: #4535 (part 1, policy)
- Builds on: [ADR 0089](./0089-intra-instance-trust-boundary.md) (authenticated
  operator and fleet-service-token boundary), [ADR 0066](./0066-goal-trust-operator-channel.md)
  (federation token and trust tiers), [ADR 0121](./0121-device-pairing-over-an-operator-supplied-public-url.md)
  (device-scoped tokens).
- Relates to: [ADR 0123](./0123-forward-delegate-approvals-up-the-chain.md)
  (forwarding a delegate's approval card up the chain).

## Context

`run_command`'s approval policy is **host-wide**: every authenticated caller is
treated identically. Two levers decide whether a command runs without an
operator looking at it, and neither can be scoped to *who is calling*:

Grounding below was read from local `main` at
`6f2dbfbc34cefd0b2a5b58fc52cf30ad1ac67d63`; line numbers refer to that revision.

| Source | Current behavior |
| --- | --- |
| `src/tools/run_auto_approve.py` | `filesystem.run_auto_approve`: a fail-closed, argv-prefix allowlist. `compile_auto_approve` validates/drops entries at every graph build; `match_auto_approve` returns the matching rule and the argv to `exec` (no shell). Applies to **every** caller. |
| `src/tools/fs_tools.py:998–1006` | Host config read at graph build: `filesystem_run_requires_approval` (default True), `filesystem_bypass_allowed` (default True), `filesystem_run_auto_approve` → `compile_auto_approve`. |
| `src/tools/fs_tools.py:865–872` | `_bypass_requested()` reads `current_request_metadata().get("bypass_permissions")` — i.e. **caller-supplied** per-turn metadata (the operator's `/bypass` toggle). |
| `src/tools/fs_tools.py:1607–1652` | The gate: if `auto_approve_rules` match and not bypassing → run with an `(auto-approved: matches "…")` marker + an info log; else `interrupt()` an approval card; else (bypass) run with a `ran under bypass-permissions` warning. `bypass_allowed and _bypass_requested()` lets **any** authenticated caller skip approval for every command in a turn unless `filesystem.bypass_allowed: false`. |
| `src/graph/middleware/request_context.py` | `current_request_metadata()` / `request_metadata_scope()` expose the **merged A2A request metadata** (message-level overlaid by request-level) via a contextvar. This mixes server state with **caller-supplied keys**, so it is not a trustworthy identity source. |
| `src/a2a_impl/auth.py:372–381,905–910` | `A2AAuthMiddleware` classifies the presented credential into `Credential(tier, scope, device_id)` and stamps `request.state.trust_tier` / `credential_scope` / `device_id`. `device_id` is set only for a paired device token (ADR 0121). |
| `src/a2a_impl/auth.py:748` | `tracing.set_trust_tier(request.state.trust_tier)` — precedent: a server-derived credential fact bound to a request-scoped contextvar for downstream code. |
| `src/a2a_impl/executor.py:505–514` | Already derives a caller class (`operator`/`scheduler`/`device`/`federation`/`fleet`/`unknown`) **server-side** from `auth._classify_request_credential(...)` + `cred.device_id`, never from message metadata. |
| `docs/reference/a2a-endpoints.md` ("Fleet multi-tenancy") | A hub proxies `/agents/<slug>/a2a` to a loopback member and **swaps** the caller's `Authorization` header for the internal fleet service token (ADR 0089) before forwarding. The member sees the **hub**, not the original caller. |

The operator wants to grant a *specific* authenticated caller a different
`run_command` policy — e.g. auto-approve a wider command set for a trusted CI
key, or forbid `/bypass` for the fleet-proxied hub — **without** widening the
host default for everyone. Operator decision for this part: `bypass_allowed`
keeps defaulting to `true`, and existing callers see no behaviour change.

## Decision drivers

- Key the policy on an identity the **server derives from the authenticated
  credential**, never on anything a caller can put in request/message metadata.
- Handle the fleet header-swap honestly: a member authenticates the **hub**, so
  a per-caller grant there is a grant to the hub, documented as such.
- Reuse the existing fail-closed matcher (`compile_auto_approve` /
  `match_auto_approve`) unchanged — no second, subtly-different parser.
- Make the effective policy for a command auditable: the marker and log name the
  policy that matched.
- Preserve today's behaviour for every caller that has no explicit policy.
- Respect the import-layering invariant: `graph/` and the tool layer must not
  import `server/`; identity must reach the tool through a `graph/`-level
  contextvar, bound by the A2A layer.

## Considered options

1. **(a) Caller-asserted trust flag in metadata.** A caller sets
   `metadata.trusted: true` (or a named policy) and the gate honours it. Rejected:
   `current_request_metadata()` is caller-controlled; any authenticated caller
   could self-elevate. This is the exact hazard `_bypass_requested` is already
   gated against by a *host* setting, and ADR 0123 D4 restates it — authority is
   never read from caller metadata.
2. **(b) Per-delegate config on the caller's side.** The lead/hub declares "I
   trust delegate X, auto-approve my commands there." Rejected: it puts the
   authority on the wrong side of the trust boundary. The box that *runs* the
   command must decide what runs on it; a receiver cannot verify a caller's
   private config, and a compromised caller would simply assert the widest one.
3. **(c) Key policy on `trust_tier` alone.** Rejected as too coarse: every
   operator-tier caller — the local operator, a paired phone, and the fleet
   service token — shares one bucket, so it cannot express "the hub may not
   `/bypass`" while the local operator still can.
4. **(d) Per-caller policy keyed on a server-derived caller identity.** Chosen.
   Reuses the existing matcher, falls back to the host policy for unknown
   callers, and derives identity exactly where `executor.py:505–514` already
   does — from the authenticated credential, never from metadata.

## Decision

### D1 — Caller identity is server-derived, and never from caller metadata

Introduce a request-scoped **caller identity** contextvar in the `graph/` layer,
a sibling of `current_request_metadata()` (e.g. `current_caller_identity()` /
`caller_identity_scope(...)` in `src/graph/middleware/request_context.py`),
mirroring how `tracing.set_trust_tier` already carries a server-derived
credential fact. It is bound by the A2A executor/chat layer from
`request.state` — the same `Credential(tier, scope, device_id)` the auth
middleware stamped (`src/a2a_impl/auth.py:905–910`) and that
`executor.py:505–514` already classifies — **not** from `context.message.metadata`
or the merged request metadata. `run_command` reads it live at the gate, the
same way `_bypass_requested()` reads config live, so it is per-turn and not
captured at tool-build time.

The identity is a single stable string, resolved server-side with this
precedence:

1. a **paired device** → its `device_id` (ADR 0121), e.g. `device:ab12…`;
2. otherwise a **named API key**, when the matched credential carries a key name
   → `key:<name>`;
3. otherwise the operator bearer (`A2A_AUTH_TOKEN`) → `operator`;
4. the **fleet service token** (ADR 0089) → `fleet-service`;
5. a federation credential → `federation` (or its configured label);
6. anything unclassified → `unknown`.

A caller cannot choose, spoof, or override this value; it is a pure function of
the credential the transport authenticated. If identity cannot be derived
(classification error), it resolves to `unknown`, which matches no policy and
therefore inherits the host policy — the fail-closed direction (more prompts,
never fewer).

**Fleet header-swap (explicit).** Because the hub swaps the caller's
`Authorization` for the fleet service token before forwarding to a loopback
member (`docs/reference/a2a-endpoints.md`, "Fleet multi-tenancy"; ADR 0089), a
member authenticates the **hub**, not the human operator behind it. The member
therefore resolves the caller identity of a fleet-proxied request to
`fleet-service` — a per-caller policy on a member keyed on `fleet-service`
applies to *the hub as a whole*, and is documented as such. The member cannot
recover the original caller from the swapped credential and must not try to read
it out of metadata. End-to-end per-operator scoping across the hub is out of
scope for this ADR (it would require signed per-operator assertions, not a
shared service token).

### D2 — Config shape: `filesystem.caller_policies`, override-with-inheritance

Add a list-of-dicts section:

```yaml
filesystem:
  bypass_allowed: true              # host default, unchanged
  run_auto_approve:                 # host baseline, unchanged
    - git status
    - git diff
  caller_policies:
    - caller: device:ab12cd34       # a server-derived identity from D1
      run_auto_approve:             # REPLACES the host list for this caller
        - git status
        - git diff
        - npx vitest run
    - caller: fleet-service
      bypass_allowed: false         # this caller may never /bypass
```

Semantics, when the current caller's identity equals a policy's `caller`:

- **`run_auto_approve` replaces** (does not add to) the host list for that
  caller. A policy that specifies the key uses exactly its entries, compiled by
  `compile_auto_approve` and matched by `match_auto_approve` **unchanged**
  (same validation, same drop-and-warn, same exec-from-tokens path). Replace is
  chosen over union because union can only ever *widen* the auto-approve
  surface, so it cannot express a *less*-trusted caller; replace can express
  both, and it fails in the safe direction — a policy that forgets to re-list
  `git status` merely costs that caller an extra prompt. To extend rather than
  replace, the operator copies the host entries into the caller policy.
- **`bypass_allowed` overrides** the host value for that caller (true or false).
  It is still the server-side gate on a caller-supplied `bypass_permissions`
  flag — a policy can grant or deny the *honouring* of `/bypass`, but the
  decision to bypass still comes from the operator's per-turn toggle; the policy
  never bypasses on its own.
- A key the policy **omits inherits the host value** (an entry can scope only the
  list, only bypass, or both). This keeps unmatched fields at today's behaviour.

A caller with **no** matching policy uses the host `run_auto_approve` /
`bypass_allowed` exactly as today. `caller_policies` defaults to empty, so an
unconfigured host behaves identically to current `main` — satisfying the
"no behaviour change for existing callers" decision (`bypass_allowed` still
defaults to `true`). Policies are compiled once per caller at graph build (and
re-validated on hot reload), the same lifecycle as the host list.

Because this is a **list-of-dicts** section it cannot ride a `string_list`
Settings `Field` the way `filesystem.run_auto_approve` does; the implementation
slice (S2) must plumb it through `graph/config.py` + the config round-trip
golden map and render it as a structured Settings section (it is the kind of
section that legitimately appears in `_LEGACY_SECTIONS` for the round-trip test,
not a golden exemption to be papered over).

### D3 — Gate order and interaction with ADR 0123 and `/bypass`

At the `run_command` gate (`src/tools/fs_tools.py:1607–1652`) the only change is
that the three host values consulted — `bypass_allowed`, the `_bypass_requested`
gating, and `auto_approve_rules` — are first resolved **through the matched
caller policy** (D2), then used exactly as today:

1. resolve the caller identity (D1) and look up its policy (D2);
2. if the resolved `auto_approve_rules` match and the resolved
   `bypass_allowed and _bypass_requested()` is false → **auto-approve** (marker +
   log, D4);
3. else if approval is required and not bypassing → **`interrupt()` the approval
   card** (unchanged payload: title, `detail` with the real `runs via:` runner,
   project);
4. else (bypass permitted and requested) → run with the bypass warning (D4).

**ADR 0123.** 0123 governs step 3 only: when a card *is* raised, a delegate can
forward it up the chain to its lead's operator as a real card with a structured
decision. Per-caller policy acts strictly *before* step 3: if the matched
policy auto-approves a command, no card is raised, so nothing is forwarded.
Crucially, the caller identity consulted here is the **immediate A2A caller of
this agent** (for a delegate that is the lead/hub that called it, resolving to
`fleet-service` under the fleet swap), not the human at the top of the chain.
Per-caller auto-approve is an operator-configured standing grant *on the box that
runs the command*, with the same trust basis as the host auto-approve list —
consistent with 0123's rule that consent is server-carried and never
synthesised from caller input. The two features compose without conflict: a
non-matching command still parks and still forwards under 0123 unchanged.

**`/bypass` console toggle.** The `/bypass` tab toggle still sets
`message.metadata.bypass_permissions` and is still honoured only when the
*effective* `bypass_allowed` (host, or the caller policy's override) is true. So
an operator can, for example, set `bypass_allowed: false` for `fleet-service`
while leaving the host default `true`, and the local operator's `/bypass` keeps
working while a fleet-proxied `/bypass` is ignored. No caller can widen its own
bypass authority through the toggle; the toggle only exercises authority the
server-side policy already granted.

### D4 — Audit names the policy that matched

The auto-approved marker and the log line must name the **caller policy** that
matched, not just the rule, so a transcript and the logs show *why* a command
ran without a human:

- host match (today): `(auto-approved: matches "git status")`;
- caller match: `(auto-approved: caller "device:ab12cd34" matches "git status")`,
  with the log line `run_command auto-approved by caller_policies[device:ab12cd34].run_auto_approve[git status]: <command>`.

The bypass warning (`ran under bypass-permissions (no approval)`) likewise names
the resolved caller identity. The name logged is always the **server-derived**
identity string from D1, never a caller-supplied label. Identity strings are
treated as inert display text in logs and the marker (they are credential-derived
but not themselves secrets), consistent with how 0123 D2 treats hop names.

## Consequences

- An operator can tighten *or* loosen `run_command` approval for a named,
  server-authenticated caller without touching the host default, and the marker
  and logs make each auto-approval attributable to a policy.
- A fleet member can only scope the **hub** (`fleet-service`), not individual
  operators behind it; end-to-end per-operator policy is explicitly deferred.
- Replace (not union) semantics mean adding a caller policy with a short list
  *drops* the host auto-approves for that caller. This is the fail-closed
  direction (extra prompts) and is documented in the Settings help; operators who
  want "host plus a few" copy the host entries in.
- The matcher is reused verbatim, so its fail-closed guarantees and the "list
  read-mostly commands only" doctrine carry over unchanged; no new parser to
  keep in sync.
- Identity is derived once, server-side, from the authenticated credential; the
  contextvar lives in `graph/` and is bound by the A2A layer, so no tool- or
  graph-layer code imports `server/`.
- This ADR ships no runtime changes. The slices below implement and test the
  contract before #4535 part 1 is claimed done.

## Ordered implementation slices

Each card lists at most four files **including its own changelog fragment**.
New test modules below are planned, not files added by this documentation PR.
Dependencies are linear, S1 → S2 → S3; deploy only after S3. Existing regression
suites supplement the named new tests.

1. **S1 — Server-derived caller-identity contextvar.** Files:
   `src/graph/middleware/request_context.py`, `src/a2a_impl/executor.py`,
   `tests/test_caller_identity_context.py` (new), `changelog.d/4535.added.md`.
   Add `current_caller_identity()` / `caller_identity_scope(...)` beside the
   request-metadata contextvar; bind it in the executor from `request.state` /
   the classified `Credential` (the D1 precedence, reusing the
   `executor.py:505–514` classification). Tests:
   `test_identity_from_device_key_operator_fleet_federation`,
   `test_identity_never_read_from_message_metadata`,
   `test_fleet_swapped_request_resolves_to_fleet_service`,
   `test_unclassified_credential_is_unknown`. Until S2 consumes it, the identity
   is bound but nothing keys policy on it (no behaviour change).

2. **S2 — Config + per-caller policy resolution at the gate.** Files:
   `src/graph/config.py` (+ `graph/settings_schema.py` and the round-trip golden
   map), `src/tools/fs_tools.py`, `tests/test_run_auto_approve_caller.py` (new),
   `changelog.d/4535.changed.md`. Load `filesystem.caller_policies`, compile each
   caller's `run_auto_approve` with `compile_auto_approve` at graph build,
   resolve the current caller's policy at the gate with override-with-inheritance
   (D2), and apply `bypass_allowed` / rules through it. Tests:
   `test_caller_list_replaces_host_list`,
   `test_omitted_field_inherits_host`,
   `test_unknown_caller_uses_host_policy`,
   `test_per_caller_bypass_allowed_false_blocks_bypass`,
   `test_default_empty_policies_match_main_behaviour`,
   `test_bypass_flag_still_only_from_toggle_not_policy`.

3. **S3 — Audit + Settings surface.** Files: `src/tools/fs_tools.py`,
   `src/graph/settings_schema.py` (structured list-of-dicts section + help text),
   `tests/test_run_command_audit_marker.py` (new), `changelog.d/4535.docs.md`.
   Make the marker and log line name the matched caller policy (D4) and render
   `caller_policies` in Settings. Tests:
   `test_marker_names_matching_caller_policy`,
   `test_bypass_warning_names_caller_identity`,
   `test_host_match_marker_unchanged`. Run the full Python gate and the console
   unit suite before treating #4535 part 1 as implemented.
