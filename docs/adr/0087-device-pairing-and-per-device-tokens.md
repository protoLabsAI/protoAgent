# ADR 0087 — Device pairing: per-device revocable tokens + short-TTL QR codes

**Status:** Accepted

**Relates to:** [ADR 0066](0066-goal-trust-operator-channel.md) (the operator/federation token
tiers this extends), [ADR 0086](0086-chat-first-mobile-shell.md) (the mobile shell this
exists to get onto a phone).

## Context

Adding a phone to a running instance is entirely manual today: read the host's LAN or
tailnet address off the terminal, type it into the phone, then type a bearer token. The
console makes the last step worse — `authToken()` (`apps/web/src/lib/api.ts`) reads
**localStorage only**, so there is no URL-borne credential at all and no way to shorten the
flow without adding one.

Auth today is a single shared secret: `_BEARER[0]` in `a2a_impl/auth.py`, plus an optional
federation token confined to `/a2a` + `/v1` (ADR 0066). Every device that authenticates
presents the *same* string.

That is the real problem with "add my phone". **Adding implies removing.** With one shared
secret, revoking a lost phone means rotating the secret, which simultaneously logs out the
desktop, the CLI, every script, and every other device. In practice nobody does that, so a
lost phone stays authorised.

## Decision

### D1 — Per-device tokens, individually revocable

A **device registry** joins the operator and federation tiers: a list of named devices, each
holding its own token. Auth accepts the shared operator bearer **or** any non-revoked device
token; both resolve to the `operator` tier.

Device tokens are `operator`, not a lesser tier, on purpose. A paired phone runs the full
console — it needs the same surface the desktop does. The point of the split is **identity
and revocation**, not reduced capability. Anything else would be security theatre: the
console's own features (plugin install, config rewrite) are what make the tier meaningful,
and a phone that can't use them isn't the product.

Revoking a device is a single delete and affects nothing else. The shared bearer keeps
working unchanged, so the CLI, A2A callers, and existing setups are untouched — this is
purely additive.

### D2 — Store hashes, never tokens

The registry persists `sha256(token)`, never the token itself. A leaked registry file cannot
be replayed. The token is returned exactly once, at claim time, and never again — there is
no "show token" affordance to build later.

Registry location is `instance_root/devices.json`, **not** `config_dir`. `config_dir` is the
tier that gets seeded/shared between instances; device credentials must never cross an
instance boundary, so they live at the per-instance tier alongside `.instance-uid`.

### D3 — Pairing is a short-TTL, single-use code — never the token

The QR encodes a **pairing code**, not a credential. Embedding the bearer (or a device
token) in a scannable image would create a permanent credential that leaks through every
screenshot, screen-share and shoulder-surf, forever, with no way to tell it happened.

The code is:

- **~190 bits** of entropy (32 url-safe characters), so guessing is not a threat;
- **120 seconds** TTL;
- **single-use**, consumed atomically on claim;
- **memory-only** — never written to disk, so a restart invalidates every pending pairing.
  That is the desired behavior, not a limitation.

Claiming mints a *new* device token. The scanner names the device, so the operator sees what
joined rather than an anonymous entry.

### D4 — The claim endpoint is unauthenticated, and that is the sensitive part

`POST /api/pairing/claim` cannot require auth — obtaining auth is its entire purpose. It is
therefore on the public allowlist, which widens the unauthenticated surface and deserves to
be called out rather than buried.

It is guarded by: the entropy and TTL above, single-use consumption, an immediate reject
when no pairing is pending (the common case is a closed door), and a failed-attempt counter
that invalidates all pending codes rather than allowing indefinite guessing. Constant-time
comparison throughout.

**Accepted residual risk:** anyone who can see the operator's screen during the 120-second
window can scan the code and claim a device. This is inherent to QR pairing and is why the
window is short, the code single-use, and the resulting device *visible and revocable* in a
list. The alternative — no pairing — leaves people hand-typing bearer tokens, which in
practice means picking weak ones or reusing them.

### D5 — The code travels in the URL fragment

The pairing URL is `…/app/#pair=<code>`. A fragment is never sent to the server, so it stays
out of access logs, proxy logs and `Referer` headers. The console consumes it into
localStorage and immediately strips it from the URL and history, so a shared screenshot of
the address bar (or a back-button trawl) doesn't carry it.

A query string would be simpler and is rejected for exactly those reasons.

### D6 — Pairing requires a reachable bind address, and says so when there isn't one

Pairing is meaningless when the server is bound to loopback: the phone cannot reach the port
no matter what it scans. The start endpoint enumerates candidate addresses (tailnet first,
then LAN) and reports them; with none it returns a clear error rather than a QR that cannot
work.

This must **not** nudge anyone toward `PROTOAGENT_ALLOW_OPEN=1`. The server already refuses a
non-loopback bind with no token (`server/__init__.py`), and that guard stays — pairing is
about adding devices to a *secured* instance, not about opening one up.

## Consequences

- The unauthenticated surface grows by one endpoint. It is the only one, it is rate-limited
  and TTL-bound, and it is the price of not having people type bearer tokens into phones.
- Device tokens are `operator`-tier, so a stolen unlocked phone is as dangerous as a stolen
  unlocked laptop. Revocation is the mitigation, which is the whole point of D1.
- The registry is per-instance. A device paired to the dev sandbox is not paired to prod.
- Losing `devices.json` logs out every paired device (hashes are unrecoverable by design).
  The shared bearer still works, so this is a recoverable state.
- Nothing here changes the existing bearer/federation behavior. An instance that never pairs
  a device is byte-for-byte unaffected.
