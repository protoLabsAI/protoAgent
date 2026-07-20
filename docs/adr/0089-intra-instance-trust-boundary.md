# ADR 0089 — Intra-instance trust boundary: the hub authenticates, members trust a fleet service token

**Status:** Proposed

**Relates to:** [ADR 0087](0087-device-pairing-and-per-device-tokens.md) (per-device tokens —
the credential that exposed this), [ADR 0066](0066-goal-trust-operator-channel.md) (the
operator/federation tiers a member re-checks), [ADR 0042](0042-fleet-supervisor-unified-console.md)
(the slug-routing reverse proxy this changes), [ADR 0065](0065-two-tier-instance-paths.md) (the
box/instance scoping that makes each member's credentials distinct), [ADR 0071](0071-plugin-permissions-trust-model.md)
(present only the boundary you enforce).

## Context

A fleet inside one instance is a **hub plus member agents sitting behind the hub's reverse
proxy** (ADR 0042). The console reaches a member by URL slug — `/agents/<slug>/…` — and the hub
forwards to the member's loopback port (`graph/fleet/proxy.py`). Members bind `127.0.0.1` and are
meant to be reached *only* through the hub.

Every member is launched with its **own** `PROTOAGENT_HOME=<ws>`, so its `instance_root` is its
own workspace dir (`graph/workspaces/manager.py:run_exec`). Two consequences follow, and together
they are the bug:

- **Each member has its own `devices.json`.** The per-device registry is deliberately scoped to
  `instance_root` (ADR 0087 D2 — a device paired to dev must never authenticate against prod), so
  a device token minted on the hub exists **only** in the hub's registry.
- **Each member has its own `config/langgraph-config.yaml`**, hence its own `auth.token`.

When the console calls a **sister** agent — say `/agents/<slug>/api/plugins/spacetraders/…` — the
hub authenticates the caller at its edge and then reverse-proxies to the local member, **forwarding
the browser's `Authorization` header verbatim** (`proxy.py` returns no extra headers for a local
peer). The member then re-authenticates the *external* caller's credential against its *own*
isolated state:

- A **device token** (ADR 0087) is in the hub's registry, not the member's. The member's
  `verify_token` reads a different `devices.json` → not found → **401**. This is structural, not a
  race: per-device tokens are *designed* not to be shared, so they can **never** match across
  members.
- A **shared bearer** matches only if that member was seeded with the hub's exact `auth.token` and
  it was never rotated.

The visible failure — a plugin's console view throwing `TypeError … 'a.symbol'`, "dashboard
offline" — is a **downstream symptom**: the view didn't guard a 401 body. The disease is that a
call the hub already authenticated got rejected by a sister agent.

**Why it ever worked.** Hub-spawned members currently run *open* on loopback: the supervisor
strips `A2A_AUTH_TOKEN` and sets `PROTOAGENT_ALLOW_OPEN=1` (`graph/fleet/supervisor.py`). So
intra-instance auth "works" only by being *absent*. That is also a latent hole — any local process
can hit a member's operator API (plugin-install is code execution, ADR 0071) with no credential —
and it collapses the moment a member actually requires a token, or a caller presents one that
cannot be shared. ADR 0087 is simply the first thing to present such a credential.

The root cause is that **there is no trust model for intra-instance calls at all.** Each member is
treated as an independent security principal that must re-authenticate the end user, when it is
really a backend sitting behind the hub's front door.

## Decision

### D1 — The hub is the instance's single auth boundary; members trust the hub

Adopt the API-gateway model. The hub authenticates the external caller **once**, at its edge
(shared bearer / device token / federation, unchanged), establishes a trust tier, and then presents
a member with a **credential the member trusts** — never the raw external token. A member is a
backend, not a front door; it does not re-authenticate the end user and never consults the device
registry. The registry, and all per-device identity/revocation, stay exactly where ADR 0087 put
them: **at the hub**.

### D2 — A fleet **service token**: one internal secret, injected, not shared-per-config

The instance holds a single internal credential — the **fleet service token** — generated once,
persisted `0600` at the fleet tier (`instance_root/workspaces/.fleet-token`, beside the fleet
registry the hub already owns), and **injected into every local member's environment at spawn**
(`PROTOAGENT_FLEET_TOKEN`). Delivery is env, not config and not URL — the same discipline #2055
used for the desktop webview: a service credential must not ride a file that gets seeded/copied
between workspaces, nor anything the page can read.

Every agent in the fleet (hub included) **accepts** the fleet service token as the `operator` tier,
in a holder distinct from the shared bearer and the federation token (`a2a_impl/auth.py`). It is a
loopback-only service credential; external clients never present it, and it is never written to a
member's `langgraph-config.yaml` (so it cannot leak through `workspace new --from`).

This is deliberately a **shared secret**, not a signed per-request assertion (see Alternatives).
Members do not need to know *which* device called — the hub already recorded that against the real
device identity. They need exactly one bit: "did this come from the hub?" A shared loopback secret
answers that with no per-request crypto.

### D3 — The hub swaps credentials at the proxy boundary, operator-tier only

For a **local-peer** target, the fleet proxy **replaces** the caller's `Authorization` with the
fleet service token (`_target_for_slug` / `forward_to`). Two guards:

- **Swap only when the caller cleared `operator`-tier auth at the edge**
  (`request.state.trust_tier == "operator"`). The fleet token is operator-grade; it must never
  elevate a lesser credential. The `/agents/<slug>/…` proxy is operator-console traffic — federation
  is denied the `/api` ceiling at the edge (ADR 0066) and does not use slug routing — so in practice
  the swap only ever fires for operator callers; the guard makes that a property, not a coincidence.
  A non-operator request is forwarded **without** the fleet token (fail closed to the member's own
  auth), never elevated.
- **Preserve the #1890 anonymization.** A request admitted off the member's *public* list arrived
  anonymous (`request.state.member_public`); it is forwarded anonymous — the swap must not lend an
  unauthenticated caller a ride on the fleet token, the same rule that already strips the stored
  remote bearer there.

The incoming `Authorization` is stripped before the fleet token is applied, regardless of header
casing, so the upstream request carries exactly one credential.

### D4 — The fleet token is the credential for **every** in-instance caller of a member

The swap fixes the console proxy, but the principle is general: any in-instance caller of a loopback
member presents the fleet token. That means the same credential is attached by:

- the **console reverse proxy** (D3) — the path in the reported bug;
- **`delegate_to` / the A2A client** when the resolved target is a loopback member;
- the **portfolio-PM local board dispatch** the supervisor comment calls out as "tokenless" today.

The fleet token becomes the one intra-instance operator credential, replacing "the member is open so
anything works".

### D5 — Members stop running open (phased, because D4's other call sites must land first)

Once the fleet token is injected and accepted, a member no longer needs to run open. Stop stripping
`A2A_AUTH_TOKEN` into `PROTOAGENT_ALLOW_OPEN=1`; a member **requires** a credential (its own bearer
*or* the fleet token) and rejects unauthenticated loopback traffic — closing the local-process RCE
hole.

This is sequenced **after** D4, not with D2/D3, because closing members breaks any in-instance caller
that hasn't been converted to present the fleet token (exactly the tokenless PM/delegate dispatch
D4 enumerates). Phasing:

- **Phase 1 (fixes the reported bug):** D2 + D3 — inject + accept the fleet token, swap at the
  proxy. Members may stay open transitionally; the swap presents a credential they accept either way,
  so device tokens work on sisters immediately.
- **Phase 2 (closes the hole):** D4 conversions land, then D5 flips members closed. Members must be
  restarted to adopt the closed posture; the hub triggers the restart on upgrade.

### D6 — Rotation and lifecycle

The fleet token is regenerated by deleting the file; the hub mints a fresh one and members pick it up
on their next spawn. Rotation is instance-wide, which is acceptable for a loopback-only internal
secret (contrast a device token, whose whole point is per-device revocation — that lives at the hub
and is unaffected). Losing the file is self-healing: the hub regenerates and re-injects on the next
member spawn.

### D7 — Plugin console views must tolerate a non-200 (follow-up, not the fix)

The `a.symbol` white-screen is the SpaceTraders view dereferencing a 401 error body as if it were
data. Independent of the auth fix, a plugin view should render an error state, not throw, on a
non-2xx. Tracked as a hardening follow-up; it is a symptom, and shipping it as "the fix" would leave
the trust gap in place.

## Consequences

- **Device tokens work on sister agents.** A paired phone reaches every member's plugin surface,
  because the member never sees the device token — it sees the fleet token. The registry is consulted
  once, at the hub, exactly as ADR 0087 intended.
- **The open-member loopback hole closes** (Phase 2). A member no longer serves its operator API to
  any local process unauthenticated.
- **Remote members are unchanged.** They are a cross-instance boundary (ADR 0066 federation) and keep
  their per-remote stored bearer; the fleet token is loopback-only and is never sent off-box.
- **ADR 0087's per-instance registry isolation is preserved.** Nothing moves `devices.json` up a
  tier; dev-paired devices still cannot authenticate against prod. We removed the coupling by moving
  the *decision* to the hub, not by sharing the *state*.
- **Migration.** Existing fleets keep working through Phase 1 (members stay open; the swap is
  additive). Phase 2 requires a member restart to adopt the closed posture; a fleet that never
  restarts a member stays in the transitional-but-working state.
- **A lost `.fleet-token` file** logs nothing out permanently — the hub regenerates it. In the window
  between regeneration and a member respawn, a still-running member holds the old token; the hub
  restarts members on rotation to avoid a split.

## Alternatives considered

- **Signed per-request assertion (JWT/HMAC of `{caller, tier, exp}`).** The member verifies a
  hub-signed assertion instead of a shared secret, and could enforce per-caller/tier policy. It is the
  more capable long-term shape and the natural evolution *if* members ever need to make decisions about
  *which* caller/tier is talking to them. Rejected for v1: a loopback hop between processes that already
  trust the same box does not need per-request crypto or key distribution, and members enforcing
  per-caller policy is a non-goal today (they are backends behind the hub). D2 leaves the door open —
  swapping a static token for a signed assertion is a change confined to the same two seams.

- **Shared registry at the box tier.** Move `devices.json` (and the bearer) up so every sister reads
  one registry and the external token validates directly on each member — no swap. Rejected: it
  contradicts ADR 0087 D2's deliberate per-`instance_root` scoping (dev/prod isolation), couples member
  data, and *keeps* every member re-authenticating the end user and exposing the device-auth surface —
  it makes the coupling shared-state instead of removing it. It also does nothing for the "member is
  open" hole.

- **Leave members open (do nothing).** Rejected: it is the current state, it is the RCE hole, and it
  only "works" until a member requires a token — which per-device tokens now routinely cause.
