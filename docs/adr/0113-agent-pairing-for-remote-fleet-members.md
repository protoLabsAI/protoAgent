# 0113 — Agent pairing: remote fleet members get a paired, revocable token

- Status: Proposed
- Date: 2026-09-26
- Builds on: [ADR 0042](./0042-fleet-supervisor-unified-console.md) §I (remote fleet
  members), [ADR 0087](./0087-device-pairing-and-per-device-tokens.md) (device pairing +
  per-device tokens), [ADR 0089](./0089-intra-instance-trust-boundary.md) (the fleet service
  token), [ADR 0025](./0025-unified-delegate-registry-and-panel.md) (the delegate registry)
- Amends: ADR 0042 §I (auth, delegation, WebSockets), ADR 0087 (a second code kind, and
  the Devices flag graduates)

## Context

A console can already reach another protoAgent on the same LAN or tailnet. Discovery finds
it (`graph/fleet/discovery.py`: loopback, tailnet, and mDNS when enabled), "Add to this
fleet" registers it as a remote member (`remotes.json`), and the hub reverse-proxies its
console and A2A with a stored bearer. The pieces meet at one place, and that place is
manual:

1. **Credentials are hand-carried.** Discovery can't carry a credential, so a discovered
   remote is added with **no token** and every proxied call to a secured remote 401s. The
   only fix is pasting the remote's *shared* operator bearer into "Add a remote by URL".
   That hands the hub the one secret that also unlocks the remote's desktop, CLI and every
   other client, and revoking the hub means rotating it for all of them.
2. **Two registries, two tokens.** A fleet remote and an `a2a` delegate to the same agent
   are configured separately. The one-click "Add as delegate" creates the delegate with no
   token, so `delegate_to` 401s even when the fleet row works.
3. **Making a remote reachable is unguided.** A remote must bind a non-loopback address
   with a token. The one in-product helper, ADR 0087 D6's "Allow devices on my network",
   is behind the `settings.devices` flag (tier `off`) because it stopped the desktop app
   from starting four times before each layer was fixed. The flag's `remove_by` is
   2026-10-01.
4. **WebSockets to remotes are refused** (#1607). The hub used to attach the remote's
   stored bearer to an upgrade it never authenticated, which lent an anonymous caller a
   ride into the remote's terminal PTY. Refusing closed the hole, and it also took the
   terminal and agent-browser live views off every remote.
5. **Smaller:** mDNS advertises this machine's LAN address even when the server is bound
   to loopback, which is an address nobody can reach. The remote probe is unauthenticated,
   so a remote with a wrong token reads "running". ADR 0042 §I still says "this is **not
   built**".

ADR 0087 already built the hard part. A pairing code is short-lived and single-use, the
claim is unauthenticated by necessity and guarded, and claiming mints a **per-client
token** stored as a hash and individually revocable. It was scoped to phones. A hub is
just another client of the remote.

## Decision

### D1 — A hub pairs with a remote the way a phone does: the remote shows a code, the hub claims it

The remote's operator opens **Settings ▸ Devices ▸ Pair an agent** (or runs
`protoagent pair` on a headless box, D7) and gets a code. On the hub, "Pair…" on a
discovered or registered remote asks for that code. The **hub's server** redeems it against
the remote's existing `POST /api/pairing/claim` and receives a device token, which it stores
in `remotes.json` as the remote's bearer.

The remote's operator consents by generating the code. The hub never learns the remote's
shared bearer. The remote sees the hub as a named, revocable entry in its Devices list. No
new unauthenticated endpoint is added: the claim route ADR 0087 D4 already accepted is
reused as is.

**Rejected: hub requests, remote approves** (Bluetooth-style). It needs a new
unauthenticated inbound "pair request" endpoint, which is a spam and flooding surface, and
someone must be watching the remote's console when the request lands.

### D2 — Agent codes are short and typeable

A phone scans its code; an agent code is read off one screen and typed into another,
usually on a different machine. So an agent code is:

- **10 characters of Crockford base32** (about 50 bits), shown as `XXXXX-XXXXX`. Claim
  normalizes the input: case-insensitive, dashes and spaces ignored, `O→0` and `I/L→1`.
- **5-minute TTL** (typing across machines takes longer than a scan), **single-use**,
  memory-only, the same as D3 of ADR 0087.
- Under the **same failed-claim counter**: 5 misses drop every pending code. An attacker
  gets 5 guesses at a 2^50 space per code the operator issues, a success chance of about
  4×10⁻¹⁵.

The code's **kind** is recorded when it is minted, not claimed: a code minted as an agent
code yields a device with `kind: "agent"`. The claimer cannot upgrade or relabel what it is.
Phone codes are unchanged (32 url-safe characters, 120s, in the URL fragment).

**When the counter resets:** on a successful claim, when the lockout fires, and whenever a new
code is minted. So each code the operator issues gets its own budget of 5 misses, and a typo
made on an earlier code does not count against the next one. Codes expiring does not reset it,
because nothing is pending to guess against then (a claim with nothing pending is rejected
before the counter is touched).

**Accepted residual risk:** the 5-miss lockout means anyone who can reach the unauthenticated
claim route can cancel a pending pairing by guessing wrong 5 times. That is a nuisance, not a
compromise, and it exists for phone codes today. Scoping the counter per caller was considered
and rejected: a guesser rotates source addresses for free, and a miss can't be attributed to a
particular code without revealing which codes exist. The operator's recourse is to mint
another code.

### D3 — The paired token is an ordinary device token

The minted token goes through the unchanged ADR 0087 registry: stored as `sha256`, operator
tier, revoked with one delete. A `kind` field (`device` | `agent`) is added so the Devices
list can say *which* entries are other agents. An existing `devices.json` without the field
reads as `device`. Operator tier is correct for the same reason it is for a phone: the hub
drives the remote's full console through the proxy.

The hub stores the token in `remotes.json` (0600, atomic, never returned by `status()`),
exactly where a pasted token goes today. "Add a remote by URL" keeps accepting a pasted
token for instances that predate pairing or aren't protoAgents' own consoles.

### D4 — One registry, one token: a delegate to a remote routes through the hub

A delegate to a remote member points at the **hub's own proxy on loopback**,
`http://127.0.0.1:<hub-port>/agents/<remote-id>/a2a`, not at the remote's URL. That uses two
mechanisms that already exist:

- A loopback delegate with no token presents the fleet service token (ADR 0089 D4), which
  the hub accepts as operator.
- The hub's proxy forwards `/agents/<id>/*` to a remote with the remote's stored bearer,
  which is now the paired token.

So the paired token lives in one place, rotating or re-pairing fixes every consumer at once,
and the token never leaves the hub's box: the delegate carries only the fleet token, which
is loopback-only by construction. `supervisor.status()` reports this URL as the remote's
`a2a`, and the console's "Add as delegate" uses it. A delegate to a remote that is *not* a
fleet member keeps working as before, with its own `auth_token`.

This is the "one registry feeding both views" ADR 0042 §I deferred, done by routing rather
than by merging the stores.

### D5 — The hub probes a remote with its token, and says when the token is refused

The reachability probe stays the unauthenticated agent card (cheap, on the 3s poll). When a
token is stored, the hub also makes an **authenticated** probe, on a slower TTL and
immediately after pairing or editing, and records `auth: "ok" | "rejected" | "unknown"`. The
console shows "token rejected — re-pair" on the row instead of a green dot that 401s when
clicked. A revoked hub therefore shows up as revoked.

### D6 — WebSockets to remotes: re-enabled, and the hub never lends a credential

`forward_ws` stops refusing remotes, under one rule: **the hub never attaches the remote's
stored bearer to an upgrade on its own.** For a remote target:

- A presented `?token=` is authenticated at the hub (`bearer_tier`). If it is operator, it
  is **swapped** for the remote's stored token, mirroring the local-member swap (ADR 0089).
  If it is not operator, the socket is closed.
- A socket with no `?token=` (ticket-based plugins such as agent_browser) is passed through
  with **no** Authorization header. Its ticket was minted over the authenticated HTTP proxy,
  and the remote checks it.
- A remote with **no stored token** is still refused. With nothing to authenticate against,
  the hub would be a blind pipe into an open instance.

An unauthenticated caller therefore gets nothing from the hub it couldn't get by connecting
to the remote directly. That was the property #1607 was protecting.

### D7 — Headless remotes get a CLI: `protoagent pair`

A docker or server install has nobody at its console. `protoagent pair` talks to the running
instance on this machine (its operator bearer is read from the instance's own config, the
way `protoagent fleet` finds its hub), mints an agent code, and prints it with the addresses
the instance is reachable on. `protoagent fleet pair <url> <code>` is the hub-side
counterpart, so the whole flow works without a browser.

### D8 — Devices graduates: the flag is removed after a desktop-app test

"Pair an agent" lives in Settings ▸ Devices, beside "Add a device". It needs the same
reachability step: a remote bound to loopback can't be paired with. So this ADR ships the
Devices section **on**, and removes the `settings.devices` flag, but only after the whole
path has been exercised **in the desktop app**, which is where the four earlier failures
landed. That means a local build with its version bumped above the latest release (otherwise
it self-updates over the build), and these steps:

1. Start the app on its default loopback bind.
2. Run "Allow devices on my network" (token minted, `0.0.0.0` written).
3. Restart the app. It still reaches its own sidecar on loopback, and nothing 401s,
   including CORS preflight from the webview origin.
4. Pair a phone.
5. Pair a second agent from a hub on another port or machine.
6. Revoke each one.
7. Set the bind back to `127.0.0.1`.

Any failure is fixed before the flag is removed.

**Amendment (2026-09-26): exercised 2026-09-26 in an isolated desktop build — passed.** In a
local QA build of the desktop app: loopback offered "Allow devices"; the token was minted and
`0.0.0.0` written, and after a restart the console loaded (no hang, no 401, no CORS
failure); an agent code was shown with the tailnet URL; a hub paired over the tailnet (`auth:
ok`) and an A2A message through the hub completed; revoking gave an immediate 401 at the hub
and `auth: rejected`; the bind went back to `127.0.0.1`, a restart came up loopback-only, and
the console loaded. The `settings.devices` flag is removed (#3651), and the operator guide is
[Pair devices and agents](../guides/pairing.md).

### D9 — Discovery hygiene

- mDNS advertises only when the server is bound to a non-loopback address, and advertises an
  address the server actually listens on.
- No `paired` flag on discovery results: `GET /api/fleet/discover` already drops every
  registered remote, so a discovered agent is by definition unpaired. A registered remote's
  pairing state is its row's `auth` (D5).
- ADR 0042 §I's stale "not built" paragraph is replaced by a pointer here.

**Not doing: HTTPS discovery.** The port scan probes `http://` on 7860–7910. A protoAgent
behind TLS almost always sits behind a reverse proxy on 443, not in that range. Probing
both schemes doubles scan time for a case the manual "Add a remote by URL" form (which
accepts `https://`) already covers.

### D10 — A credential never crosses a plaintext network without an explicit opt-in

Pairing sends the code to the remote, gets an operator-tier token back, and the proxy then
presents that token on every call. Over plain `http://` on a LAN, anyone on that network can
read both. The fleet CLI already has a rule for this (`deck/hub.py` `credential_allowed`): a
credential goes over `http://` only to loopback, and to any other host only with
`--insecure-http`. Pairing adopts the same rule, and adds one exception for encryption the
network already provides:

- **`https://`, loopback, and tailnet addresses (100.64.0.0/10) pair without asking.** A tailnet
  link is WireGuard-encrypted underneath, which is why ADR 0087 already ranks it above LAN.
- **Plain `http://` to any other address is refused** unless the caller opts in:
  `allow_insecure: true` on `POST /api/fleet/remotes/pair` (and on add/update when a token is
  set), `--insecure-http` on `protoagent fleet pair`, or a "this network is trusted, send the
  code in cleartext" confirmation in the console's Pair dialog. The refusal says why, and names
  the tailnet as the fix that needs no confirmation.
- A MagicDNS name (`*.ts.net`) counts as tailnet. Any other name is judged by the address it
  resolves to at pairing time.

This does not make LAN pairing safe; it makes it a decision the operator takes knowingly
rather than a default. TLS on the remote is the real fix and stays the operator's to set up
(the add-by-URL form accepts `https://`).

## Consequences

- **Adding a secured remote becomes: discover → Pair… → type the code.** No token is
  copied by hand, and the remote can revoke the hub on its own.
- **`delegate_to` a remote depends on the hub being up.** It does anyway: the member is
  registered with the hub, and a member's own delegates to a remote are routed the same way.
- **The Devices section ships on.** The flow that bricked the desktop app becomes default
  UI. D8's desktop test is the gate, not CI.
- **The unauthenticated surface does not grow.** The claim route gains a second code kind
  under the same counter.
- **The hub keeps a per-remote operator credential** in `remotes.json`, as it already
  does for a pasted token. It is scoped to that remote and revocable there, which is
  strictly better than a pasted shared bearer.
- **Remote WebSockets come back**, so the terminal and agent-browser live views work on a
  paired remote, and an anonymous caller still gets no credential lent.
- **An open hub lends the stored token only to its own console** (#3662). On a hub with
  no credential every caller is operator, so a cross-site page (a blind form POST) or a
  DNS-rebinding page could have ridden the remote's operator token through the HTTP proxy.
  For remote targets on an open hub, the proxy refuses cross-site Fetch Metadata, a foreign
  `Origin` and an untrusted `Host` with `403` before dialling (the WS path gains the `Host`
  gate too). A token-gated hub needs no gate: its credential is a header, never a cookie.

## Implementation slices

1. **Agent codes on the remote**: `security/devices.py` code kinds, normalization, the
   device `kind` field; `POST /api/pairing/start {kind: "agent"}`; tests for TTL,
   single-use, lockout, normalization, and that the kind comes from the code.
2. **Hub-side pairing + authenticated probe**: `POST /api/fleet/remotes/pair` (with the D10 transport rule)
   (`supervisor.pair_remote`: SSRF-guarded claim, add or re-token), `auth` status (D5),
   `protoagent fleet pair` (D7).
3. **Delegates through the hub**: the remote's `a2a` in `status()` is the hub's loopback
   proxy URL (D4); a live smoke delegates through the hub to a token-gated remote.
4. **Remote WebSockets**: `forward_ws` under D6, with tests for swap, pass-through,
   non-operator refusal, and tokenless-remote refusal.
5. **Discovery hygiene + CLI**: D9 (mDNS) plus `protoagent pair` (D7); ADR 0042 cleanup.
6. **Console**: Devices ▸ "Pair an agent" (code, countdown, reachability, kind badges); the
   Fleet panel's "Pair…" / "Re-pair" dialog; the `auth` badge; "Add as delegate" uses the
   proxied `a2a`.
7. **Devices graduation**: the D8 desktop-app test, fixes, and removal of the
   `settings.devices` flag, docs guide and sidebar.
