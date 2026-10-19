# Security & the trust model

protoAgent's security posture is deliberately simple and **honest**: it sandboxes what is genuinely
untrusted, trusts what you deliberately installed, and tells you plainly that running arbitrary code
needs a container — it does **not** pretend otherwise. There are three actors, three postures.

## 1. Code you installed → trusted (by your choice)

A plugin's backend Python runs **in-process, with full access** — the filesystem and network as the
server user. This is intentional and it's the same deal as `pip install` / `npm install` / a VS Code
extension / a ComfyUI custom node: **installing a plugin runs its code, so installing it *is*
trusting it.** Only install plugins you'd trust with your shell.

Because the backend already runs as you, gating a plugin's *frontend* (its UI) behind extra "trust"
ceremony would be **theater** — it'd be harder on the browser-React of code whose Python you already
let run as your user. So plugin UIs are simply **sandboxed iframes** (the plugin serves its own page;
see [building a plugin view](../guides/building-react-plugin-views.md)) — uniform, no special "this
plugin is trusted" mode. (Earlier versions had a Module Federation in-process-React path behind a
trust gate; ADR 0038 retired it as inconsistent with this model.)

## 2. Code the agent generated → untrusted (always)

This is the boundary that genuinely matters. **Even with a trusted operator and trusted plugins, the
model's *output* is untrusted** — prompt injection, a hostile page it fetched, a hallucinated
snippet. So anything the agent *generates* and renders is sandboxed:

- **Artifacts** (`show_artifact` — generated HTML/SVG/Mermaid/React) render in a **nested
  `sandbox="allow-scripts"` iframe with no same-origin** — they run, but can't touch the console,
  its cookies, or its APIs. Same model as Claude Artifacts / Open WebUI.

This is the *one* place the sandbox isn't ceremony — it's the line between "code you installed" and
"code the model emitted."

## 3. The agent running code → opt-in, honestly partial

`execute_code` and the ACP coding-agent runtimes run real code. We don't pretend this is sandboxed:
`execute_code`'s own docs say it is *"isolation, not a true sandbox"* — a subprocess with a hard
timeout and a scrubbed env, but it can still touch the FS/network as the server user. The real
controls are:

- **Opt-in** — `execute_code` is off unless enabled.
- **Egress allowlist (ADR 0008)** — outbound HTTP is deny-by-default for private IPs, and a
  configured `egress.allowed_hosts` makes it a strict allowlist.
- **The honest guidance**: enable it for trusted-model output, or run inside a hardened container.

## The real isolation boundary is the container

Putting it together: if you want protection from genuinely **untrusted third-party plugins** (not
"I trust what I installed"), the lever is **not** any per-feature toggle — it's the **deployment
boundary**. Run protoAgent in a container (the recommended isolation boundary, see
[multi-instance](../guides/multi-instance.md)). Inside that boundary:

- installed plugins are trusted (you chose them),
- the agent's *generated* output is sandboxed (iframe),
- `execute_code` is opt-in and egress-gated.

That's the whole model, stated plainly: **trust what you install, sandbox what the model generates,
contain the blast radius.** No security theater, no pretending a plugin's React was more dangerous
than its Python.

## An instance with no token

With no bearer token (the desktop default, bound to loopback), the operator API is open to
anything that can reach the port. On a shared machine that includes every web page open in a
local browser. Three hardening rules narrow what a browser page can do
([#3668](https://github.com/protoLabsAI/protoAgent/issues/3668)):

- **Host allowlist.** Every request and WebSocket must be addressed to a name the instance is
  served under: an IP literal, `localhost`/`*.localhost`, `*.ts.net`, this machine's
  `<name>.local`, a named bind, the hosts of `A2A_ALLOWED_ORIGINS`, or
  `PROTOAGENT_TRUSTED_HOSTS`. Anything else gets `403`. This is the same allowed-hosts check
  Jupyter, VS Code and Vite apply to their local servers.
- **Cross-site requests.** A request that can change state (anything but `GET`/`HEAD`) and every
  WebSocket must come from the console itself. That means one of:
  - the same origin, including the console's plugin views;
  - the desktop app;
  - a loopback origin the server's CORS policy already admits;
  - an `A2A_ALLOWED_ORIGINS` entry;
  - a client that isn't a browser (no `Origin`, no `Sec-Fetch-Site: cross-site`, as with curl or
    an SDK).

  Anything else gets `403`.
- **JSON only on `/a2a` and `/v1/*`.** A request with a body must say
  `Content-Type: application/json` (or a `+json` type). Anything else gets `415`.

These rules narrow the exposure but aren't authentication. Any local process, and any other
page on a loopback origin, can still use an open instance. If other people or programs share
the machine, or the instance is reachable beyond loopback, set a token. A token-gated instance
skips these rules: its credential is a header the client attaches (never a cookie), so a page
on another origin has nothing to send. The server refuses a non-loopback open bind unless
`PROTOAGENT_ALLOW_OPEN=1`.

## The pairing-claim endpoint

`POST /api/pairing/claim` is the one `/api/*` route that takes no token. It can't: it is how a
phone or another agent gets a token in the first place ([pairing](../guides/pairing.md)). So
anyone who can reach the port can send it codes, and the question is what that buys them.

**The code does the work.** A pairing code is 10 Crockford base32 characters, about 50 bits,
and it lives for 300 seconds. QR links carry the same code. Each code works once.

**The throttle caps the guessing.** Only *wrong* codes count. They draw from two token buckets
([ADR 0121 D3](../adr/0121-device-pairing-over-an-operator-supplied-public-url.md)):

| Bucket | Keyed on | Budget |
| --- | --- | --- |
| Per client | The client IP, resolved through [`network.trusted_proxies`](../reference/configuration.md#network) | 5 wrong codes per 5 minutes |
| Per ingress class | `public` (the request's Host is the public console URL) or `private` (everything else) | 30 wrong codes per minute |

When either bucket is empty, the claim gets `429` with `Retry-After`, and **the code isn't
compared with anything**. A throttled request is not a guess. A claim that arrives when no code
is pending is rejected before any comparison. It draws from the per-client bucket only, so a
prober stays throttled without spending the shared budget. A code that works never draws.

**The math.** A code's 300 seconds at the full 30-per-minute ceiling admit at most
30 × 5 = 150 evaluated guesses. Against 2^50 codes that is a success chance of about
1.3 × 10⁻¹³ per code. Even a code that stayed pending all year with the ceiling saturated the
whole time would stay below 2 × 10⁻⁸. The entropy keeps the codes safe. The throttle exists to
cap cost and log noise, so its ceiling can be generous.

**No request can cancel a code.** Before ADR 0121, five wrong codes in a row cancelled every
pending code. That made the guard itself the attack: anyone who could reach the port could keep
the operator from ever pairing. Now a flood can, at worst, hold a bucket empty while it lasts.
A flood on the `public` class makes public pairing wait. It can't touch the `private` class, so
LAN and tailnet pairing keep working. Forging the public Host only drains the public bucket.
Until a public console URL is configured, every claim is `private`.

**Who counts as one client.** `X-Forwarded-For` counts only when the TCP peer is loopback or on
`network.trusted_proxies`. Otherwise a guesser could name a fresh address on every request and
never run out. The flip side: behind a proxy on another address that isn't listed, every client
looks like the proxy and shares its 5 tries.

The buckets live in memory. A restart empties them, and it also clears every pending code.

## A console on the internet {#a-console-on-the-internet}

By default the console is never offered on a public address: pairing only offers tailnet and
LAN addresses it discovers on this machine. The one exception is an address **you** declare in
[`network.public_console_url`](../reference/configuration.md#network), served by a proxy you
run, so a phone without Tailscale can pair
([ADR 0121](../adr/0121-device-pairing-over-an-operator-supplied-public-url.md) D2, D6).

The agent holds a few lines on its own once that is set:

- **Never open.** It refuses to start with a public URL and no token, and refuses a settings
  save that would leave it that way. `PROTOAGENT_ALLOW_OPEN=1` means "a network boundary fences
  this port", which a public URL contradicts, so it doesn't override this.
- **HTTPS only.** The URL must be `https://`. A pairing claim addressed to the public hostname
  must have been https at the proxy, as `X-Forwarded-Proto` from a
  [trusted proxy](../reference/configuration.md#network) says. That catches a proxy serving
  plain http, and a proxy whose headers the agent doesn't believe.
- **Its own throttle bucket.** Wrong codes on the public hostname drain the `public` bucket
  only (see above), so an internet flood can't stop LAN or tailnet pairing.
- **Phones get `device` scope** wherever they pair, so a stolen phone can't mint new tokens or
  change these settings ([Device tokens](#device-tokens)).

**Access in front, or token only.** The recommended setup puts an identity-aware proxy, such as
Cloudflare Access, in front of the whole console hostname
([recipe](../guides/exposing-protoagent.md#console-hostname)). An anonymous caller then can't
reach the sign-in, the pairing claim or anything else. Access decides who reaches the console,
and the token decides what they can do. **Token only** is supported, and weaker: anyone can load
the sign-in and try codes, with the code's entropy, the throttle and the device scope as the
only defences, and every console route is exposed to whatever bug an attacker finds first. The
Devices panel warns for as long as the URL is set: "Anyone on the internet can reach this
console's sign-in. Put Cloudflare Access (or another identity-aware proxy) in front of it."
Neither posture sandboxes the agent's tools; a paired phone's chat turns run them like yours.

## Device tokens

Each paired phone, tablet or agent hub has its own token
([ADR 0087](../adr/0087-device-pairing-and-per-device-tokens.md)), so you can revoke one without
signing out anything else. Two separate properties decide what a token can do.

**Trust tier: who the caller is.** Every device token is the **operator** tier, the same as the
auth token. Chat, goals, knowledge trust and everything else that asks "is this the operator?"
treat a paired phone as you. A phone isn't a second-class user of the product.

**Credential scope: whether it can change who gets in.**
([ADR 0121](../adr/0121-device-pairing-over-an-operator-supplied-public-url.md) D4.)

| Credential | Scope |
|---|---|
| The auth token, `X-API-Key`, the fleet service token, another agent's hub | `full` |
| A phone or tablet paired since ADR 0121, over any network | `device` (shown as **Limited**) |
| A phone or tablet paired before ADR 0121 | `full` (shown as **Full access**, with a **Limit** action) |

A `device`-scope token can use the whole product, but the server refuses it (`403 device_scope`)
everything that would let it keep access after you revoke it, or hand access to someone else:

- pairing: minting or cancelling codes, pairing another agent, and serving over tailnet HTTPS;
- restarting the agent;
- renaming, removing or promoting any device except itself (it can rename and unpair itself);
- plugin lifecycle: install, update, enable, remove, sync, bundles and plugin setup steps;
- MCP servers: adding, importing, changing or removing one. Like a plugin, an MCP server is a
  command the server runs, so a phone that added one would leave host code behind that keeps
  running after you revoke it. The same goes for delegate and ACP agent commands, lifecycle
  hooks and the editor command, which are refused as config writes;
- the secrets manager and provider sign-ins (`/api/secrets/*`, `/api/config/oauth/*`);
- [credential-plane settings](../reference/configuration.md#credential-plane-keys): auth tokens,
  `network.*`, `devices.*`, egress, `filesystem.allow_run`, the secrets manager, the plugin
  lists, and every setting that holds a command the server runs (`mcp.*`, `delegates`,
  `lifecycle_hooks`, `acp.*`, `filesystem.editor_command`).

The full route list is in the [operator API reference](../reference/operator-api.md#device-scope-refusals).
The check runs in the auth middleware, matches the fleet-proxied form of each path
(`/agents/<slug>/…`) too, and runs before the hub swaps in its fleet credential, so a phone
can't reach these routes on a fleet member either. Stream tokens (`/api/sse-token`) carry the
scope of the device that minted them.

To give a phone full access, promote it from a full-access device in
[Settings ▸ Devices](../guides/pairing.md#full-access-and-limited). A phone can't promote itself.

**The honest limit: scope is not a sandbox.** A `device`-scope phone can still chat, and a chat
turn runs with the agent's tools. If the agent can run shell commands or read files, a turn
started from the phone can read anything the server user can, including the auth token in the
config. What the scope guarantees is that revoking a phone is final through the console's own
surface: it can't mint itself a new token, install a plugin or change the network settings on
the way out. To contain tools, use tool policy (`filesystem.allow_run: false`) and, for an
internet-facing console, an identity-aware proxy in front of it. A narrower tool policy for
`device`-scope turns is possible future work, not something this release does.

### Device token lifetime {#device-token-lifetime}

([ADR 0121](../adr/0121-device-pairing-over-an-operator-supplied-public-url.md) D5.) A Limited
(`device`-scope) token expires when it goes unused, rotates while it is used, and revokes the
device if a rotated-out copy turns up later. Full-access tokens do none of this.

**Idle expiry.** A Limited token stops working after
[`devices.idle_ttl_days`](../reference/configuration.md#devices) (default 30, `0` = never)
without use. Use is the device's last-seen stamp, which is refreshed at most every five
minutes, or the pairing time if it was never used. An expired device stays in Settings ▸
Devices as **Expired** until you remove it. A phone left in a drawer pairs again.

**Weekly rotation.** While a phone is in use, the console asks the server about its own device
(`GET /api/devices/self`) when it starts and once a day while it stays open. When a week has
passed since the last rotation (or the pairing), the server says `rotate_due`, and the console
asks for a new token (`POST /api/devices/self/rotate`) and stores it with a strict write. The
server keeps the old token valid **until the new one is first presented**, then for 120
seconds more for requests already in flight. Open plugin views, in this tab and others, are
handed the new token as soon as it's stored, so nothing in the console keeps sending the old
one. If the browser can't store the new token, it
keeps sending the old one, and that keeps working, so a failed write never signs the phone
out. A second rotation that presents the old token within two minutes of an unused one is
refused (`409`), so two tabs racing a rotation can't leave the browser holding the token that
lost. A rotation that was never used is replaced by the next one.

**Reuse detection.** A rotated-out token presented **after** its grace means two parties hold
one token: the phone moved on, and someone else still has the old copy. The server revokes
the device, which stops the new token too, and writes a `device.revoked_token_reuse` entry to
the audit log (`audit/audit.jsonl` under the instance root). It's the standard refresh-token
reuse rule. The phone pairs again; the copy someone else holds is dead.

One exception: a token retired by [moving the phone to another address](/guides/phone-access#move-this-phone)
is refused after its grace, but doesn't revoke the device. The address the phone moved away
from still holds that token in its storage, so presenting it there is expected, not a sign of a
copy. The token stops working all the same, and the console there forgets it. The next weekly
rotation brings reuse detection back for the token it retires. A handoff code lasts 60 seconds,
works once, and only carries the phone to an address this agent offers.

**Revocation reaches open streams.** A new request is refused as soon as a device is removed,
expires or is reuse-revoked, because every request reads the device registry. Streams that
were already open are closed too, within one keepalive (about 15 seconds): the event bus
(`/api/events`), the attendance stream (`/api/chat/attend`), A2A `message/stream`, chat turns
and fleet-proxied streams. Each re-checks the device at a keepalive tick it already sends. A
stream token minted by a device (`/api/sse-token`) carries the device's id, so a revoked phone
can't open a new stream in the 30 seconds that token is valid. Open WebSockets close too, within
about 12 seconds, when the phone opened them with its own token or with a ticket it minted on
this agent, such as agent_browser's viewport. A plugin that mints its own tickets, such as the
terminal, must record the minting phone for this; until it does, removing the phone only stops
the socket reconnecting.

## See also

- [Building a plugin view](../guides/building-react-plugin-views.md) — the sandboxed-iframe plugin UI model.
- ADR 0008 (egress allowlist), ADR 0027 (git-URL plugin install), ADR 0038 (why the in-process React
  trust gate was retired), ADR 0004 / [multi-instance](../guides/multi-instance.md) (containers as
  the isolation boundary).
