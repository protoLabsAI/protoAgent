# Access from your phone (LAN / Tailscale)

The console is an **installable PWA**, so you can drive your agent from your phone — on
your home Wi-Fi, or from anywhere over **Tailscale** — and pin it to the home screen so it
opens like an app (standalone, no browser chrome, recolored to the agent's accent). No app
store, and **no offline mode** — the install is convenience, not a cached copy (see
[the note below](#no-offline-mode)).

It's three steps: bind the server to the network behind a token, open it on the phone, add
it to the home screen.

## 1. Bind to the network + set a token

By default protoAgent binds **loopback only** (`127.0.0.1`) so a desktop run isn't exposed.
To reach it from a phone you bind a routable interface — and the boot gate **refuses a
non-loopback bind without an auth token** (the operator API includes plugin-install + config
rewrite). So set both at once:

```bash
A2A_AUTH_TOKEN=$(openssl rand -hex 24) python -m server --host 0.0.0.0 --port 7870
```

Note the token — you'll paste it into the phone once. (You can also put it in
`config/secrets.yaml` under `auth.token`; env wins.) See
[`A2A_AUTH_TOKEN`](/reference/environment-variables#authentication-a2a-bearer-token) and
[`PROTOAGENT_HOST`](/reference/environment-variables#deployment-ui-tier-adr-0010).

## 2. Reach it from the phone

Open the console at **`http://<host>:7870/app`** in the phone's browser. On the first
request it prompts *"Authentication required"* — paste the token; it's cached in that
browser's `localStorage`, so you do it once per device.

What `<host>` is depends on how the phone gets to the machine:

| From | `<host>` | Notes |
|---|---|---|
| **Same Wi-Fi (LAN)** | the machine's LAN IP — `192.168.x.x` / `10.x.x.x` | `ipconfig getifaddr en0` (macOS) or `hostname -I` (Linux). Only works while both are on that network. |
| **Anywhere (Tailscale)** | the machine's tailnet IP (`100.x.x.x`) or its MagicDNS name | **Recommended for off-LAN.** Encrypted mesh, no port-forwarding, nothing exposed to the public internet. |

### Tailscale (reach it from anywhere)

Install [Tailscale](https://tailscale.com) on **both** the host and the phone and sign both
into the same tailnet. Then the host is reachable at its stable `100.x.x.x` address (or
`http://<host>.<tailnet>.ts.net:7870/app` with MagicDNS) from the phone over LTE/5G — no
ports opened on your router, no public surface. The bearer token is still your auth; the
tailnet is the network boundary.

> Discover (Settings → Agents → **Discover**) already finds other protoAgents on your
> tailnet via the Tailscale CLI — see [Fleet](/guides/fleet#remote-fleet-members-the-agent-there-the-ui-here)
> for operating a remote agent's console through the hub.

## 3. Add to Home Screen

Once the console loads, install it so it launches full-screen:

- **iOS Safari** — Share → **Add to Home Screen**. Works over plain `http` on your LAN /
  tailnet. Uses the apple-touch-icon and opens standalone.
- **Android Chrome** — ⋮ → **Add to Home screen**. (Chrome's *automatic* install banner
  needs HTTPS **and** a service worker, which the console deliberately omits — so add it
  from the menu instead.) The manifest's `display: standalone` still applies.

The launched app is scoped to `/app/` and follows the agent's theme — favicon and mobile
browser chrome recolor to the agent's accent.

## No offline mode

The console is a **manifest-only** PWA — there is **deliberately no service worker**. A SW
would cache console assets (going stale the moment the agent updates — a real hazard on a
version-coherent fleet) and would sit in front of the `/a2a` SSE stream the live chat rides
on. So "install to home screen" gives you an app-like launcher, **not** an offline copy: the
agent has to be reachable (LAN or tailnet) for the console to work. That's the intended
trade-off — zero stale-asset risk over offline support.

## Going further: a public URL

Tailscale covers "just me, from my phone." If you want a **real hostname** — to share with
others, or reach the agent without Tailscale on the client — put it behind a tunnel or
reverse proxy and set
[`A2A_PUBLIC_URL`](/reference/environment-variables#a2a-agent-card-endpoint) so the agent
card advertises the right address. See
[Deploy in Docker → Expose it with a tunnel](/guides/deploy-docker#expose-it-with-a-tunnel-ngrok-cloudflare).
Keep the token set — once it's internet-reachable, the bearer gate is the only thing
between the open operator API and the world.

## See also

- [Run headless (API + A2A)](/guides/headless) — the no-UI server you're binding here
- [Deploy in Docker](/guides/deploy-docker) — binding, auth, and the tunnel section
- [Fleet](/guides/fleet) — operate a remote agent's console from one hub
- [Environment variables](/reference/environment-variables) — `A2A_AUTH_TOKEN`,
  `PROTOAGENT_HOST`, `A2A_PUBLIC_URL`
