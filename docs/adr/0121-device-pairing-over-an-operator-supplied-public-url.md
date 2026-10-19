# ADR 0121 — Device pairing over an operator-supplied public URL

- Status: Accepted (2026-10-18, Josh)
- Date: 2026-10-18
- Epic: #4336 (remote device improvements), slice S6. S7 implements it.
- Builds on: [ADR 0087](./0087-device-pairing-and-per-device-tokens.md) (QR pairing and
  per-device tokens), [ADR 0113](./0113-agent-pairing-for-remote-fleet-members.md) (typed
  codes and the shared failed-claim counter), [ADR 0089](./0089-intra-instance-trust-boundary.md)
  (the fleet proxy's credential swap), [ADR 0066](./0066-goal-trust-operator-channel.md) (trust
  tiers), [ADR 0114](./0114-console-browser-storage-budget.md) (console storage).
- Amends: ADR 0087 D1, D4 and D6; ADR 0113 D2; ADR 0089 D3. Also rewrites the console rule in
  `docs/guides/exposing-protoagent.md` and `docs/guides/phone-access.md`.
- Siblings in flight: S2 (typed device codes: Crockford base32, reusing the ADR 0113 agent-code
  machinery and the shared counter) and S4 (Tailscale MagicDNS with `tailscale serve` HTTPS in
  the QR, binding only the picked interface). This ADR assumes both and contradicts neither.

## Context

A phone pairs by scanning a QR on the operator's screen (ADR 0087). The QR leads to
`http://<host>:<port>/app/#pair=<code>`. Today it can reach the agent two ways:

- **Same Wi-Fi or a tailnet.** `_pairable_addresses` in `src/operator_api/pairing_routes.py`
  allowlists only `100.64.0.0/10` (tailnet) and RFC 1918 (LAN). A public address is never
  offered, by design (ADR 0087 D6: "the offer never contains a public address"). The URL is
  always `http://`.
- **Off-LAN means Tailscale.** `docs/guides/phone-access.md` sends people to Tailscale. The
  public proxy guide (`docs/guides/exposing-protoagent.md`) forwards only
  `/.well-known/agent-card.json` and `/a2a` and returns 404 for `/app` and `/api`. Its only
  concession is "a separate identity-protected hostname".

Josh, 2026-10-18: Tailscale comes first, but a phone must be able to pair without it. The
operator brings their own public URL: Cloudflare Tunnel, Cloudflare Access, or a reverse proxy
they run. A built-in tunnel can come later. A hosted relay is out of scope.

Exposing the console publicly is safe enough today in a few respects, and unsafe in others.

**What already holds:**

- Auth is default-deny. Everything off the public allowlist needs a credential
  (`A2AAuthMiddleware`, `src/a2a_impl/auth.py`).
- The only unauthenticated route that mints a credential is `/api/pairing/claim`, matched
  exactly (`_PUBLIC_EXACT`).
- Device codes are about 190 bits and last 120 s. Agent codes, and S2's typed device codes, are
  10 Crockford characters (about 50 bits) and last 300 s (`src/security/devices.py`).
- Codes ride the URL fragment, so they stay out of access logs (ADR 0087 D5).
- The registry stores only `sha256(token)`.
- `verify_token` re-reads `devices.json` on every request, so a revocation affects the next
  request.

**What breaks on the internet:**

1. **The lockout is a public denial-of-service lever.** Five consecutive misses from anyone
   clear every pending code, of both kinds (`claim_pairing`, `_MAX_FAILED_CLAIMS = 5`). ADR
   0113 D2 accepted this as "a nuisance". On a public hostname, anyone can keep pairing
   permanently unavailable for no cost.
2. **A device token is full `operator`.** `_bearer_tier_for` returns `"operator"` for a device
   token. ADR 0087 D1 chose that deliberately. A stolen phone token can therefore:
   - mint more device tokens (`POST /api/pairing/start`);
   - change `network.bind`, the auth token and allowed origins (`POST /api/config`);
   - install plugins, which runs host code (`POST /api/plugins/install`);
   - read and write secrets (`/api/secrets/*`).

   So revoking the phone does not evict an attacker who used it to plant another credential.
3. **Tokens never expire.** A `Device` has no lifetime. Only an explicit revoke ends a token.
4. **The console is served from several origins.** The token is stored in per-origin
   localStorage: `protoagent.authToken` (`apps/web/src/lib/pairing.ts`, `lib/api/routing.ts`).
   So are the transcripts in IndexedDB (ADR 0114) and the home-screen PWA. A phone that loads
   `https://agent.example.com` and later `http://192.168.1.20:7870` is two unrelated apps. Its
   token, history and icon do not follow it.
5. **The edge hides the real client and scheme.** Uvicorn is started with no proxy settings
   (`uvicorn.Config(app, host=…, port=…)` in `src/server/__init__.py`), so it trusts
   `X-Forwarded-*` only from `127.0.0.1`. Behind `cloudflared` on a Docker network, every
   public request looks like it comes from one IP over `http`.

## Decision

### D1 — One home origin per device; the address set arrives after the claim

**The phone does not switch origins to fail over.** The origin is where the token,
transcripts and home-screen icon live (Context 4). Switching origins would mean claiming once
per origin (N tokens per phone, N rows in Devices). It would also mean either copying a
long-lived token across origins in a URL, or widening CORS so one origin's page can call
another. All three are worse than the problem they solve.

Each device instead gets **one home origin** that works both at home and away:

| The phone has | Home origin | Why |
| --- | --- | --- |
| Tailscale | the tailnet HTTPS name from S4 (`https://<machine>.<tailnet>.ts.net`) | Works on any network and is authenticated at the network layer |
| No Tailscale, and a public URL is configured | `network.public_console_url` (D2) | Works on any network, including home Wi-Fi through the edge |
| Neither | the LAN address | Home only, which is honest |

This is how the epic's "moves between home Wi-Fi and away without re-pairing" is met. The home
origin is chosen so that no move is needed.

**Choosing the origin.** The Devices panel asks which way this phone connects and preselects
the best available path, in this order:

1. Tailnet HTTPS
2. Public URL ("phone without Tailscale")
3. Tailnet IP
4. LAN

The QR and the typed-code URL point at that origin. The QR carries only `#pair=<code>`, as it
does today (ADR 0087 D5).

**The address set is not in the QR.** It comes back authenticated in the claim response and
from a new `GET /api/devices/self`, as `addresses: [{kind, origin}]` in the order above. The
console stores it on the home origin. It is used for two things:

- **Unreachable home.** When the home origin stops answering, the shell's offline notice lists
  the other origins as plain links with the requirement for each ("needs Tailscale"). Opening
  one of those origins means pairing again.
- **Moving home in one tap.** `POST /api/devices/self/handoff {origin}` is called from the
  current home. It mints a 60-second, single-use code bound to the **existing** device id. The
  console then navigates to `<origin>/app/#pair=<code>`. Claiming it rotates that device's
  token (D5) instead of adding a row.

Rejected:

- **Addresses in the QR fragment.** The QR grows, a typed code cannot carry them, and the list
  goes stale the moment the operator changes the URL.
- **Active per-request failover across origins.** Mixed-content rules already block it: an
  `https` page cannot call an `http` LAN API. It would also need `CONSOLE_ORIGIN_REGEX`
  (`src/a2a_impl/hosts.py`) widened to arbitrary origins.
- **One claim per origin.** It multiplies tokens and makes "revoke this phone" a multi-row
  action.

Reachability probes (`fetch(origin + "/healthz", {mode: "no-cors"})`) are navigation hints only.
**CORS is not widened.**

### D2 — `network.public_console_url`: an operator-declared, HTTPS-only origin

| YAML key | `LangGraphConfig` field | Default | Scope | Env fallback |
| --- | --- | --- | --- | --- |
| `network.public_console_url` | `public_console_url: str` | `""` | agent (each agent on a box has its own URL) | `PROTOAGENT_PUBLIC_CONSOLE_URL` |
| `network.trusted_proxies` | `trusted_proxies: list[str]` | `[]` | host, restart-gated | `FORWARDED_ALLOW_IPS` (uvicorn's own) |
| `devices.idle_ttl_days` | `device_idle_ttl_days: int` | `30` (`0` = never) | agent | none |

`trusted_proxies` is fed to uvicorn's `forwarded_allow_ips`. Empty keeps uvicorn's default of
`127.0.0.1`, which already covers a `cloudflared` running on the same host. `devices.idle_ttl_days`
is defined in D5.

Each field needs a `Field(...)` row in `src/graph/settings_schema.py`. Each also needs an entry in
the golden field map in `tests/test_config_roundtrip.py`, updated in the same PR, because that
golden fails on any new field.

**Why not reuse `A2A_PUBLIC_URL`.** It is the origin the A2A card advertises
(`src/graph/sdk.py`, `src/infra/media.py`). The recommended posture keeps the console on a
**separate hostname** from the A2A lockdown hostname. Sharing one key would push people to open
`/app` on their A2A hostname.

Validation:

- **At config load:** `https://` only, an origin only (no path, query or fragment), and not
  loopback.
- **At boot:** the agent refuses to start in open mode with this key set (no bearer and no
  `X-API-Key`). A public console with no credential is never a state the agent will run in. This
  mirrors the existing refusal of a non-loopback bind without a token.
- **Discovered addresses never include a public one.** `_pairable_addresses` keeps its
  allowlist unchanged. A declared `public_console_url` is the only public candidate.

**Folding into the allowlists.** The public origin is appended automatically to:

- **`A2A_ALLOWED_ORIGINS`**, when that allowlist is set. Otherwise every console call from the
  public origin gets a `403`.
- **The Host allowlist's trusted names** (`_trusted_host_names` in `src/a2a_impl/hosts.py`).
  `HostGuardMiddleware` only acts in open mode, which this key forbids, so this is for
  consistency, not protection.

`CONSOLE_ORIGIN_REGEX` is unchanged. The console is same-origin on its public host and needs no
CORS.

### D3 — The claim endpoint throttles instead of locking out

**Replaced.** The 5-miss lockout that drops every pending code (ADR 0087 D4, ADR 0113 D2).

**New rule.** A **failed** claim draws from two token buckets. A request that finds a bucket
empty gets `429` with `Retry-After`. **It is not evaluated against any code.** A throttled
request is therefore not a guess, and no request can make pending codes disappear.

| Bucket | Keyed on | Budget |
| --- | --- | --- |
| Per client | client IP, as resolved through `trusted_proxies` | 5 misses per 5 min |
| Per ingress class | `public` (Host matches `public_console_url`) or `private` (everything else) | 30 misses per min |

- **Successful claims never draw** from either bucket.
- **A miss with nothing pending** is still rejected before any comparison, as today. It draws
  only from the per-client bucket, so probers stay throttled.
- **Codes survive a flood.** A distributed flood can hold the `public` bucket empty, which
  makes public pairing unavailable while it lasts. It cannot touch the `private` class, so LAN
  and tailnet pairing keep working. It also cannot cancel a pending code.
- **Spoofing the Host header only hurts the public bucket.** A LAN host that forges the public
  Host drains the public bucket, not the private one.

**Why the math allows a generous ceiling.** A typed code (S2, about 50 bits, 300 s) admits at
most 30 × 5 = 150 evaluated guesses in its lifetime. That is a success chance of at most
150 / 2^50 ≈ 1.3 × 10⁻¹³ per code. Even a code pending all year at a saturated ceiling stays
below 2 × 10⁻⁸. QR codes (about 190 bits) are not reachable by guessing at all. The entropy
does the work. The throttle exists to cap cost and log noise, and it can never be used to
destroy codes.

**HTTPS on the public path.** A claim whose Host is the public host must arrive with
`https` as the scheme resolved through `trusted_proxies`. Otherwise it is refused with an error
that names `network.trusted_proxies`. This catches a proxy that serves `http://` publicly. It
also catches the Docker-network case where the forwarded headers are not trusted.

Rejected:

- **Keep the lockout, but scope it per IP.** ADR 0113 D2 already explains why: a guesser rotates
  source addresses for free.
- **A per-code miss budget.** A miss cannot be attributed to a code without revealing which
  codes exist.
- **A CAPTCHA.** It doesn't fit a scan-and-go flow, and an identity-aware proxy (D6) does the
  job better.

### D4 — Device credentials get a narrower *scope*, not a lower trust tier

A device stays `trust_tier == "operator"`. Its user is the operator for goal trust (ADR 0066),
knowledge trust and chat. Every `== "operator"` check (`src/server/chat.py`,
`src/knowledge/trust.py`, `src/graph/fleet/proxy.py`) keeps its meaning. A new tier string would
silently fail all of them.

What narrows is a separate, orthogonal attribute: the **credential scope**,
`request.state.credential_scope`.

| Credential | Scope |
| --- | --- |
| Shared bearer, `X-API-Key`, fleet service token, `kind: agent` devices | `full`. A hub must proxy the whole console (ADR 0113). |
| New phone and tablet pairings, from **any** path (LAN, tailnet, public) | `device` |
| Legacy `devices.json` rows with no `scope` field | `full`, labelled "Full access" in Devices with a **Limit** action |

**Scope is per credential, not per path.** A per-path rule hangs off a Host header the proxy
controls, and a phone moves between paths.

**The rule for `device` scope:** it can use the whole product but cannot change who can get in.
The **credential-plane ceiling** is enforced in `A2AAuthMiddleware`, next to the ADR 0066
federation ceiling. It matches the bare path and its `/agents/<slug>/…` proxied form by suffix,
as `_is_sse_query_token_path` already does. A `device`-scope request gets `403` on:

- `POST /api/pairing/start`, `/api/pairing/cancel`, and `/api/fleet/remotes/pair`. A device
  cannot pair devices or agents.
- `DELETE` and `PATCH /api/devices/{id}`, **except its own id**. A device can rename and unpair
  itself (S3), and nothing else.
- Plugin lifecycle: `/api/plugins/install|sync|install-deps|ack`, `/api/plugins/{id}/update`,
  `/api/plugins/{id}/enabled`, `DELETE /api/plugins/…`, bundles, and `/api/plugin-setup/…`.
  These run host code that would outlive a revoke.
- `/api/secrets/*` and `/api/config/oauth/*`.
- Config writes that touch a **credential-plane key**. These are marked
  `credential_plane=True` on the `Field` in `settings_schema.py` and checked at the config write
  path, not by URL. The set is:
  - auth and federation tokens;
  - `network.*`;
  - `A2A_ALLOWED_ORIGINS` and trusted hosts;
  - `devices.*`;
  - `filesystem.allow_run` and egress, because these are the tool-sandbox boundary.

Promoting a device to `full` (`PATCH /api/devices/{id} {"scope": "full"}`) requires a
`full`-scope caller.

**SSE tokens carry the scope.** `generate_sse_token` adds `scope` to its HMAC'd payload, so an
EventSource stream minted by a device does not come back as `full`.

**Fleet proxy.** The hub applies the ceiling *before* the ADR 0089 D3 swap. The swap still
happens for a `device`-scope operator: members can't verify hub device tokens, so this is
required for plugin views to work. The member sees the fleet token, but a denied path never
reaches it. The query-token swap (`bearer_tier(tok) == "operator"` in `proxy.py`) gets the same
check.

**Honest limit.** `device` scope is not a sandbox. A device can still chat, and if the agent has
shell or file tools, a chat turn can read anything the server user can. That includes the
bearer in the config. The ceiling makes revocation **final through the console's own
surface**, which is the property ADR 0087 D1 actually promised. Containing tools is the job of
tool policy (`filesystem.allow_run: false`) and of D6's identity-aware proxy.

A narrower tool policy for `device`-scope chat turns (for example, no shell from a phone) is
**out of scope** for this ADR. It is possible future work and would need its own decision.

Rejected:

- **A new `device` trust tier.** It silently breaks every `== "operator"` consumer (see above).
- **An allowlist of routes a device may call.** The console and plugin views call hundreds of
  routes. It would break the product, which is ADR 0087 D1's original objection.
- **`device` scope only on the public path.** That is path-based trust, which the scope design
  above rejects.

### D5 — Lifetime: idle expiry, rotation on use, reuse detection

**Idle expiry.** A `device`-scope token expires after `devices.idle_ttl_days` (default 30)
without use. Expiry is measured against `last_seen_at`. That is throttled to 300 s, which is fine
at day granularity. An expired device stays listed as "Expired" until it is re-paired or
removed.

**Rotation on use.** At most once every 7 days (a constant, not a setting), `GET
/api/devices/self` reports `rotate_due`. The console then calls `POST /api/devices/self/rotate`
and stores the new token with `writeKeyStrict` (ADR 0114 D1). The previous hash **stays valid
until the new token is first presented**, then for a 120 s grace for in-flight requests. This
makes rotation safe against the browser failing to write the token.

**Reuse detection.** A retired token presented after its grace **revokes the device** and is
written to the audit log. Two parties holding one token is the theft signal that rotation
exists to surface. This is the standard refresh-token-reuse rule, applied without splitting
tokens into access and refresh.

`Device` gains three fields: `scope`, `prev_token_sha256` and `rotated_at`. Rows written before
this change load with `scope: full` and no expiry.

**Revocation propagation:**

- **New requests.** Immediate, as today, because every request re-reads `devices.json`.
- **Long-lived streams.** The middleware stamps `request.state.device_id`. These streams
  re-run `verify_token` at their existing keepalive tick, close when it fails, and close within
  one tick of a revoke:
  - `/api/events` and `/api/chat/attend`;
  - the A2A SSE stream;
  - plugin WebSockets that authenticate at open, such as the terminal.
- **Fleet members.** Revocation propagates for free. Members only ever see the hub's fleet
  token (ADR 0089), so a device revoked at the hub stops being proxied on its next request.

Rejected:

- **Short-lived access tokens plus a refresh token.** Every console call reads the token
  synchronously from localStorage (`authToken()`). Adding a refresh round-trip touches every
  call site, for no gain over rotation with reuse detection.
- **A fixed absolute lifetime.** It forces periodic re-pairing of a phone in daily use, which
  people work around with the shared bearer.

**Amended in implementation (S7.3, #4358).**

- `Device` has a fourth field, `prev_retired_at`. It records when the new token was first
  presented, which is when the previous token's 120 s grace starts. While it is unset, the
  previous token stays valid.
- A rotate that presents the **previous** token while the current one has never been used is
  refused with `409 rotation_in_progress` if that rotation is less than 120 s old. Two tabs
  racing one rotation can therefore mint only one token. An unused rotation older than that,
  which is what a failed storage write leaves behind, is replaced, and the presented token stays
  the previous one.
- Only `device`-scope tokens rotate. `GET /api/devices/self` answers `{"device": null}` for a
  credential that isn't a paired device.
- Streams re-verify the **device** (it still exists and hasn't expired), not the token. This
  happens through one ASGI layer at the streams' existing keepalive chunks, so re-presenting a
  rotated-out token can't trip reuse detection.

### D6 — Amending the rule that the console is never public

The new rule, replacing "keep the console private" in `exposing-protoagent.md` and
`phone-access.md`:

> The console may be internet-reachable **only** on a dedicated HTTPS hostname that is declared
> in `network.public_console_url`, on an instance with a token configured. The A2A lockdown
> hostname stays exactly as documented. `/app` and `/api` still return 404 there.

The guide's recommended recipe is **Cloudflare Tunnel plus Cloudflare Access**, an
identity-aware proxy, in front of the whole console hostname. Access means an anonymous
internet caller cannot reach `/api/pairing/claim` or any app surface at all. Access then gates
who reaches the console, and the device token gates what they can do. Neither replaces the
other.

**Token-only exposure** (no Access) is supported, and documented as the weaker posture. Only
D3's throttle and D4's scope stand between the internet and the claim route. The Devices panel
shows a warning when `public_console_url` is set: "anyone on the internet can reach this
console's sign-in."

**What the operator's proxy must do:**

- Terminate TLS. Forward the **whole** hostname, not a path prefix, because the console assumes
  `/app` and `/api` at the root.
- Preserve or set Host to the public name.
- Send `X-Forwarded-For` and `X-Forwarded-Proto`, from an address listed in `trusted_proxies`.
- Not cache `/api/*`, and not buffer SSE.
- Optionally, add an edge rate-limit rule on `/api/pairing/claim`.

The guide's verification checks become:

- On the A2A hostname, `/app` and `/api` return 404.
- On the console hostname, an unauthenticated `/api/config` returns 401, or the Access login
  page when Access is on.
- A claim over plain `http` is refused.

**ADR amendments:**

- **ADR 0087.** D1 now reads "device tokens are operator-trust with `device` scope (ADR 0121
  D4)". D4's lockout is replaced by ADR 0121 D3. D6's "never a public address" still holds for
  *discovered* addresses, and the operator-declared URL is the only exception.
- **ADR 0113 D2.** The 5-miss counter is replaced by D3's buckets. Agent codes keep their
  entropy and TTL.
- **ADR 0089 D3.** The credential swap runs after the credential-plane ceiling (D4). ADR 0089
  never stated the rule that the console is never public. That rule lived only in the guides,
  which this ADR rewrites.

### D7 — Slicing (S7), in order

Each slice ships on its own and includes its documentation (see Documentation below). **S7.4 is
the only one that exposes anything, and it must not merge before S7.1 and S7.2.**

1. **S7.1 Claim throttle.**
   - Two buckets replace `_MAX_FAILED_CLAIMS` in `security/devices.py`.
   - `429` with `Retry-After` from the claim route.
   - The `network.trusted_proxies` key, passed to uvicorn.
   - LAN and tailnet benefit immediately, because pending codes can no longer be cancelled.
2. **S7.2 Device scope.**
   - The `Device.scope` field.
   - The middleware's credential-plane ceiling, including `/agents/<slug>/` suffixes and the
     proxy query-token swap.
   - `Field(credential_plane=True)` and the check at config write.
   - `self` routes for rename and unpair (shared with S3).
   - The scope in the SSE token payload.
   - Devices panel: "Full access" and "Limited" badges, plus Limit and Promote actions.
3. **S7.3 Lifetime.**
   - The `devices.idle_ttl_days` key.
   - `GET /api/devices/self` reporting `rotate_due`, and `POST /api/devices/self/rotate`.
   - Retire-on-first-use, then grace, then revoke on reuse, with an audit entry.
   - Streams re-verify at keepalive.
4. **S7.4 Public URL.**
   - The `network.public_console_url` key, with validation and the open-mode boot refusal.
   - The public candidate in the `/api/pairing/start` response.
   - The origin folded into `A2A_ALLOWED_ORIGINS` and the trusted hosts.
   - The https check on public-Host claims.
   - "Phone without Tailscale" and the token-only warning in the Devices panel.
5. **S7.5 Address set and handoff.**
   - `addresses` in the claim response and in `GET /api/devices/self`.
   - Alternate origins in the shell's offline notice.
   - `POST /api/devices/self/handoff`, with claim rebinding the existing device id.
   - Also useful without S7.4, for moving from LAN to tailnet.

A built-in tunnel (a managed `cloudflared`) gets its own ADR after S7.4 and reuses D2 to D6
unchanged.

## Documentation

Docs ship in the same PR as the behaviour they describe. A slice is not done until its pages
are updated.

| Slice | Pages it must add or change |
| --- | --- |
| S7.1 | `docs/guides/pairing.md`: what a `429` means, and that codes no longer vanish after wrong guesses. `docs/explanation/security-and-trust.md`: the claim-endpoint threat model (buckets, entropy math). `docs/reference/configuration.md`: `network.trusted_proxies`. `docs/reference/operator-api.md`: `429` on `/api/pairing/claim`. |
| S7.2 | `docs/explanation/security-and-trust.md`: a new "Device tokens" section covering trust tier vs credential scope, what `device` scope cannot do, and the honest limit (chat and tools are not sandboxed by scope). `docs/guides/pairing.md`: the Limited and Full access badges, and how to promote a device. `docs/reference/operator-api.md`: the device-scope `403`s and the `self` routes. `docs/reference/configuration.md`: which keys are credential-plane. |
| S7.3 | `docs/reference/configuration.md`: `devices.idle_ttl_days`. `docs/explanation/security-and-trust.md`: expiry, rotation, reuse detection and revocation propagation (streams close within one keepalive). `docs/guides/pairing.md`: "Revoke" and "Expired" states. `docs/reference/operator-api.md`: `/api/devices/self` and `/rotate`. |
| S7.4 | `docs/guides/phone-access.md`: a new section on off-tailnet access with your own public URL, replacing "Use a public URL". `docs/guides/exposing-protoagent.md`: the revised console rules (D6), a **Cloudflare Tunnel plus Access recipe** for the console hostname next to the existing A2A recipe, the proxy requirements, the generic nginx and Caddy console block, and the new verification checks. `docs/reference/configuration.md`: `network.public_console_url`. `docs/reference/environment-variables.md`: `PROTOAGENT_PUBLIC_CONSOLE_URL`, and the public origin folded into `A2A_ALLOWED_ORIGINS`. `docs/explanation/security-and-trust.md`: public posture, Access vs token-only. |
| S7.5 | `docs/guides/phone-access.md`: moving between networks, the home origin, and "Move this phone to …". `docs/reference/operator-api.md`: `/api/devices/self/handoff`. |

ADR 0087, 0113 and 0089 get a one-line "Amended by ADR 0121" note in the slice that changes the
behaviour each describes. Those are S7.1 (0087 D4, 0113 D2), S7.2 (0087 D1, 0089 D3) and S7.4
(0087 D6).

## Consequences

- **Pairing over the internet is possible without Tailscale.** The worst an anonymous caller
  can do to it is make public pairing unavailable while a flood lasts. It can no longer destroy
  codes, and it cannot affect LAN or tailnet pairing.
- **A stolen phone can no longer persist past its revoke** through the console: no new tokens,
  no plugins, no auth or network changes. It can still do anything chat and tools can do until
  it is revoked. The docs say so.
- **New phone pairings are `device` scope everywhere,** so a phone can no longer pair another
  phone or install a plugin. An operator who wants that must promote the phone, or use the
  desktop.
- **Tokens now die after 30 idle days and rotate weekly.** A phone left in a drawer re-pairs.
- **One home origin per device.** A phone paired on the LAN does not work away from home until
  it is moved, which is a one-tap handoff while it is still home. This is a deliberate trade
  against cross-origin token copying.
- **The new config keys touch the golden.** Three fields change
  `tests/test_config_roundtrip.py`, `settings_schema.py` and the configuration reference.
- **Nothing changes unless S7.4's key is set.** An instance with no `public_console_url` is
  never publicly offered. S7.1 to S7.3 tighten every instance, and that is intended.

## Resolved 2026-10-18 (Josh)

Josh accepted this ADR with every open question resolved as recommended:

1. **Scope everywhere.** Every new phone pairing gets `device` scope, including over the LAN and
   the tailnet (D4).
2. **Legacy devices** keep `full`, labelled "Full access" in Devices, with a **Limit** action
   (D4).
3. **Tool policy** for `device`-scope chat turns is out of scope here and noted as possible
   future work (D4).
4. **One home origin plus a one-tap handoff** satisfies "moves between home Wi-Fi and away
   without re-pairing" (D1).
5. **Token-only public exposure** is allowed, with the Devices-panel warning. An identity-aware
   proxy (Cloudflare Access) is the recommended recipe (D6).
6. **Defaults:** 30 idle days (`devices.idle_ttl_days`) and weekly rotation (D5).
