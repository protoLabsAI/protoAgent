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

## See also

- [Building a plugin view](../guides/building-react-plugin-views.md) — the sandboxed-iframe plugin UI model.
- ADR 0008 (egress allowlist), ADR 0027 (git-URL plugin install), ADR 0038 (why the in-process React
  trust gate was retired), ADR 0004 / [multi-instance](../guides/multi-instance.md) (containers as
  the isolation boundary).
