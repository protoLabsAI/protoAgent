# Exposing a protoAgent to the world

Make an agent reachable by external A2A callers through a stable public URL.
Expose the A2A routes you need, require a bearer token, and block the operator API
at the reverse proxy. The console stays off that hostname. For private remote access, use
[phone access over Tailscale](/guides/phone-access) instead. To reach the console from a
phone without Tailscale, give it [its own public hostname](#console-hostname).

<span id="the-shape-of-the-job"></span>

## The threat model — know what each surface is

| Surface | Public proxy policy |
| --- | --- |
| `/.well-known/agent-card.json` | Allow discovery; review the identity and skills it publishes |
| `/a2a` | Allow authenticated A2A calls |
| `/healthz`, `/metrics` | Keep readiness and monitoring private |
| `/v1/*` | Block unless intentionally providing an authenticated OpenAI-compatible API |
| `/app`, `/api/*`, all other paths | Block on the A2A hostname. Only a dedicated [console hostname](#console-hostname) may forward them |

A2A callers can trigger the agent's tools, so a token does not replace tool and
network restrictions. Operator routes additionally expose configuration, files,
and plugin installation.

<!-- docs-media: exposure-boundaries -->

<!-- /docs-media -->

## The pattern

1. **Bind the app private.** Run the agent bound to loopback or a private/tailnet
   interface — never `0.0.0.0` on a public IP. A reverse proxy or tunnel is the only
   thing that reaches it from outside.
2. **Put a reverse proxy / tunnel in front** (Cloudflare Tunnel, nginx, Caddy, …) and
   have it forward **only** `/.well-known/agent-card.json` and `/a2a` to the agent.
   **404 everything else** — a catch-all so `/app`, `/api`, `/v1` (unless you deliberately
   want it), and any future route are invisible from the public hostname.
3. **Require a bearer token.** Set `A2A_AUTH_TOKEN` to a strong secret. protoAgent then
   enforces `Authorization: Bearer <token>` on `/a2a`, `/v1`, and `/api`. Without it a
   public bind is refused; with it, the card stays public (discovery) but every *action*
   needs the token.
4. **Advertise the real URL.** Set `A2A_PUBLIC_URL` to the public origin
   (`https://agent.example.com`) so the card advertises a reachable interface, not a
   loopback address. Set `a2a.require_routable_url: true` in config so the agent
   **refuses to start** if the card would advertise a loopback URL — a boot-time guard
   against a misconfigured proxy silently publishing an undiscoverable or wrong card.
5. **Keep the operator surface off the A2A hostname.** Reach `/app` + `/api` over the
   tailnet/LAN, or through a separate [console hostname](#console-hostname) declared in
   `network.public_console_url`. Never through the A2A hostname.

## Worked example — Cloudflare Tunnel {#worked-example-cloudflare-tunnel}

Start with a configured agent and a named tunnel for your domain. The example
assumes `cloudflared` already runs on a Docker network named `ai`; create that
network and attach the tunnel container before deploying the agent service.

Ingress rules (`cloudflared` `config.yaml`) — order matters, first match wins:

```yaml
ingress:
  # Public A2A surface only → the agent container (reached by name on the shared net).
  - hostname: agent.example.com
    path: '^/\.well-known/agent-card\.json$'
    service: http://agent:7870
  - hostname: agent.example.com
    path: '^/a2a$'
    service: http://agent:7870
  # Lockdown: everything else on this host 404s — /app + /api never reach the agent.
  - hostname: agent.example.com
    service: http_status:404
  # …other hosts…
  - service: http_status:404          # global catch-all
```

The agent's container (compose): no public host port, joined to the network the tunnel is
on, token required:

```yaml
services:
  agent:
    image: ghcr.io/protolabsai/protoagent:latest
    expose: ["7870"]                  # NOT `ports:` on a public IP — internal only
    environment:
      A2A_PUBLIC_URL: https://agent.example.com
      A2A_AUTH_TOKEN: ${A2A_AUTH_TOKEN:?a public endpoint must not boot without a token}
      PROTOAGENT_UI: console          # console served, but only reachable privately
    volumes: [agent-data:/sandbox]
    networks: [ai]                    # the same external network cloudflared is on
    # (bind the operator console to the tailnet with a separate published port if wanted:
    #  ports: ["100.x.y.z:7876:7870"]  → http://host:7876/app over the tailnet only)
volumes:
  agent-data:
networks:
  ai:
    external: true
```

Because `cloudflared` shares the `ai` network, it reaches the container as
`http://agent:7870` — no public port on the host at all. DNS for the hostname points at the
tunnel (`cloudflared tunnel route dns <tunnel> agent.example.com`); the edge terminates TLS.

Validate the [Cloudflare ingress rules](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/)
before restarting the tunnel:

```bash
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://agent.example.com/a2a
cloudflared tunnel ingress rule https://agent.example.com/api/config
```

The first URL must match the agent service; the second must match the 404 rule.

### Verify the lockdown

After deploying, confirm from *outside* that only the intended surface answers:

```
$ curl -s -o /dev/null -w '%{http_code}\n' https://agent.example.com/.well-known/agent-card.json
200          # public card — good
$ curl -s -o /dev/null -w '%{http_code}\n' -X POST https://agent.example.com/a2a -d '{}'
401          # gated — good (no token)
$ curl -s -o /dev/null -w '%{http_code}\n' https://agent.example.com/app
404          # console hidden — good
$ curl -s -o /dev/null -w '%{http_code}\n' https://agent.example.com/api/config
404          # operator API hidden — good
```

Then an authed call should succeed:

```
$ curl -s -X POST https://agent.example.com/a2a \
    -H "Authorization: Bearer $A2A_AUTH_TOKEN" -H 'content-type: application/json' \
    -d '{"jsonrpc":"2.0","id":"1","method":"message/send",
         "params":{"message":{"role":"user","messageId":"m1",
         "parts":[{"kind":"text","text":"who are you?"}]}}}'
```

Run these four checks every time you expose an agent — the 404s on `/app` and `/api` are
the ones that matter.

## Generic reverse proxy (nginx / Caddy) {#generic-reverse-proxy-nginx--caddy}

With Caddy running on the same host as the agent, use
[exact path matchers](https://caddyserver.com/docs/caddyfile/matchers#path) and a
404 fallback:

```
agent.example.com {
    @public path /a2a /.well-known/agent-card.json
    handle @public {
        reverse_proxy 127.0.0.1:7870
    }
    handle {
        respond 404          # /app, /api, everything else
    }
}
```

The agent binds `127.0.0.1:7870`; Caddy is the only thing that reaches it and only forwards
the allowlist. Keep the token (`A2A_AUTH_TOKEN`) on regardless — the proxy allowlist and
the app's auth are independent layers, and you want both.

## Defense in depth (optional, recommended)

- Restrict tools and credentials to the work public callers need; use
  `filesystem.allow_run: false` when shell access is unnecessary. Restrict outbound network
  access too; see [Sandboxing & egress](/guides/sandboxing).
- Configure edge rate limits appropriate to your traffic and rotate compromised
  tokens. Store tokens outside tracked Compose files.
- If operators need a public console hostname, follow
  [The console on its own hostname](#console-hostname): a **separate hostname**, declared in
  `network.public_console_url`, behind an identity-aware proxy, with the application token
  enabled. The A2A allowlist above deliberately does not provide browser access. The
  declared origin is added to
  [`A2A_ALLOWED_ORIGINS`](/reference/environment-variables#streaming-origin-verification)
  for you when that allowlist is set.

## The console on its own hostname {#console-hostname}

A phone without Tailscale can reach the console through a public HTTPS hostname that you run
([ADR 0121](/adr/0121-device-pairing-over-an-operator-supplied-public-url)). The rule:

> The console may be internet-reachable **only** on a dedicated HTTPS hostname that is declared
> in `network.public_console_url`, on an instance with a token configured. The A2A lockdown
> hostname stays exactly as documented. `/app` and `/api` still return 404 there.

So the console gets its **own** hostname, for example `console.example.com`, beside the A2A
hostname `agent.example.com` above. Don't open `/app` on the A2A hostname, and don't reuse
`A2A_PUBLIC_URL` for this: that one is what the agent card advertises.

The agent enforces three things once the key is set:

- It refuses to start with `network.public_console_url` set and no auth token (no
  `auth.token`, `A2A_AUTH_TOKEN` or `X-API-Key`). `PROTOAGENT_ALLOW_OPEN=1` does not override
  this. A save that would remove the token while the key is set is refused and rolled back.
- A pairing claim whose `Host` is the public hostname must have arrived over `https`, as the
  proxy reports in `X-Forwarded-Proto`. A plain-http claim, or one from a proxy that isn't
  trusted, gets `400` with a message naming `network.trusted_proxies`.
- Wrong codes on the public hostname draw from their own throttle bucket, so a flood there
  can't stop LAN or tailnet pairing ([the pairing-claim endpoint](/explanation/security-and-trust#the-pairing-claim-endpoint)).

### Access in front, or token only {#access-or-token-only}

**Recommended: an identity-aware proxy in front of the whole hostname**, such as Cloudflare
Access. Then an anonymous caller from the internet can't reach the sign-in, the pairing claim
or any part of the app. Access decides who reaches the console, and the device token decides
what they can do there. Neither replaces the other.

**Token only** (no Access) is supported, and it is the weaker posture. Anyone on the internet
can load the sign-in and try pairing codes. Only the claim throttle and the
[device scope](/explanation/security-and-trust#device-tokens) stand between them and the
agent. Settings ▸ Devices shows a warning for as long as the key is set.

### What your proxy must do {#console-proxy-requirements}

- Terminate TLS. Forward the **whole** hostname to the agent, not a path prefix: the console
  expects `/app` and `/api` at the root.
- Keep `Host` set to the public name.
- Send `X-Forwarded-For` and `X-Forwarded-Proto`, from an address the agent trusts:
  loopback (a proxy on the same machine), or an entry in
  [`network.trusted_proxies`](/reference/configuration#network).
- Not cache `/api/*`, and not buffer server-sent events (chat and the activity stream are SSE).
- Pass WebSocket upgrades (the terminal plugin uses them).
- Optionally, add an edge rate limit on `/api/pairing/claim`.

### Worked example: Cloudflare Tunnel and Access {#console-cloudflare-tunnel-access}

This extends the [Cloudflare Tunnel example](#worked-example-cloudflare-tunnel): the same
tunnel, the same `ai` Docker network, a second hostname. Add the console hostname to the
ingress rules, **above** the global catch-all:

```yaml
ingress:
  # The console hostname: the WHOLE host goes to the agent (no path rules).
  - hostname: console.example.com
    service: http://agent:7870
  # The A2A hostname, locked down exactly as before.
  - hostname: agent.example.com
    path: '^/\.well-known/agent-card\.json$'
    service: http://agent:7870
  - hostname: agent.example.com
    path: '^/a2a$'
    service: http://agent:7870
  - hostname: agent.example.com
    service: http_status:404
  - service: http_status:404
```

Route DNS for it with `cloudflared tunnel route dns <tunnel> console.example.com`.
`cloudflared` keeps the public `Host` and sends `X-Forwarded-For` and `X-Forwarded-Proto: https`.

Declare the hostname on the agent, and trust the tunnel container. It reaches the agent from
its address on the `ai` network, not from loopback, so its forwarded headers only count once
that network is listed. Find the subnet:

```bash
docker network inspect ai --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
```

Then add to the agent service from the earlier example:

```yaml
services:
  agent:
    environment:
      A2A_AUTH_TOKEN: ${A2A_AUTH_TOKEN:?a public console must not boot without a token}
      PROTOAGENT_PUBLIC_CONSOLE_URL: https://console.example.com
      FORWARDED_ALLOW_IPS: 172.18.0.0/16   # the `ai` subnet printed above
```

The same settings in config are `network.public_console_url` (per agent) and
`network.trusted_proxies` (host-scoped, restart to apply). Without the trusted proxy, a phone's
pairing claim is refused with a message naming `network.trusted_proxies`.

Then put Cloudflare Access in front. In the Cloudflare Zero Trust dashboard, open
**Access ▸ Applications ▸ Add an application ▸ Self-hosted**, set the domain to
`console.example.com` (the whole hostname, no path), and add an **Allow** policy for your own
identity, for example your email address. Pick a session duration you can live with on a phone:
when the Access session ends, the phone signs in to Access again before the console loads.

An agent's hub that pairs over the public URL makes server-to-server calls, which can't pass
the Access sign-in. Pair other agents over the tailnet or LAN instead.

### Generic console block (nginx / Caddy) {#console-generic-proxy}

nginx in front of an agent on the same host (`127.0.0.1:7870`). The `map` belongs in the
`http` block:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl;
    server_name console.example.com;
    ssl_certificate     /etc/letsencrypt/live/console.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/console.example.com/privkey.pem;

    location / {                      # the whole hostname, not a prefix
        proxy_pass http://127.0.0.1:7870;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_buffering off;          # stream SSE as it arrives
        proxy_cache off;              # never cache /api
        proxy_read_timeout 1h;        # long-lived streams
    }
}

server {
    listen 80;
    server_name console.example.com;
    return 301 https://$host$request_uri;
}
```

Caddy gets a certificate, keeps `Host`, sends `X-Forwarded-For` and `X-Forwarded-Proto`, and
passes WebSockets on its own. It has no cache unless you add one:

```
console.example.com {
    reverse_proxy 127.0.0.1:7870 {
        flush_interval -1     # stream SSE as it arrives
    }
}
```

Both run on the agent's machine, so they connect from loopback and need no
`network.trusted_proxies` entry. Set the agent's key to `https://console.example.com` and keep
its token on.

### Verify the console hostname {#verify-console-hostname}

From **outside** your network:

```
$ curl -s -o /dev/null -w '%{http_code}\n' https://agent.example.com/app
404          # the A2A hostname still hides the console — good
$ curl -s -o /dev/null -w '%{http_code}\n' https://agent.example.com/api/config
404          # and the operator API — good
$ curl -s -o /dev/null -w '%{http_code}\n' https://console.example.com/api/config
401          # token only: gated — good. With Access on, you get the Access sign-in instead (302 or 403)
$ curl -s -X POST http://console.example.com/api/pairing/claim \
    -H 'content-type: application/json' -d '{"code":"AAAAA-AAAAA"}'
             # plain http: a redirect to https, or 400 public_not_https. Never "invalid or expired"
```

With Access off, also check that an **https** claim reaches the code check:

```
$ curl -s -X POST https://console.example.com/api/pairing/claim \
    -H 'content-type: application/json' -d '{"code":"AAAAA-AAAAA"}'
{"ok":false,"error":"invalid or expired pairing code"}
```

A `400` naming `network.trusted_proxies` here means the agent doesn't believe your proxy's
`X-Forwarded-Proto`. Add the proxy's address and restart. Each of these wrong-code checks
counts once against your own address's five tries.

## Checklist

Before sharing the URL, verify the public card advertises the correct origin,
the unauthenticated A2A call returns `401`, and a token-authenticated call works.
Check that `/app`, `/api/config`, `/healthz`, `/metrics`, `/v1/models`, and an unknown path
return `404` through the public proxy. Repeat after changing proxy rules. Confirm
that the underlying agent port cannot bypass the proxy from the public network. If you run a
[console hostname](#console-hostname), also run its [checks](#verify-console-hostname).

## See also

- [Delegates](/guides/delegates) — the A2A peer model these endpoints serve.
- [Headless](/guides/headless) — running an agent as a non-interactive service.
- [Deploy with Docker](/guides/deploy-docker) · [Customize & deploy](/guides/customize-and-deploy).
- [Fleet](/guides/fleet) — many agents discovering + calling each other.
