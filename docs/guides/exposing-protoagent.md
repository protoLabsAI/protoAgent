# Exposing a protoAgent to the world

Make an agent reachable by external A2A callers through a stable public URL.
Expose the A2A routes you need, require a bearer token, and block the operator API
at the reverse proxy. Keep the console private. For private remote access, use
[phone access over Tailscale](/guides/phone-access) instead.

<span id="the-shape-of-the-job"></span>

## The threat model — know what each surface is

| Surface | Public proxy policy |
| --- | --- |
| `/.well-known/agent-card.json` | Allow discovery; review the identity and skills it publishes |
| `/a2a` | Allow authenticated A2A calls |
| `/healthz`, `/metrics` | Keep readiness and monitoring private |
| `/v1/*` | Block unless intentionally providing an authenticated OpenAI-compatible API |
| `/app`, `/api/*`, all other paths | Block; keep operator access private |

A2A callers can trigger the agent's tools, so a token does not replace tool and
network restrictions. Operator routes additionally expose configuration, files,
and plugin installation.

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
5. **Keep the operator surface on the private net.** Reach `/app` + `/api` over the
   tailnet/LAN (a separate private bind), never through the public proxy.

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
- If operators need a public console hostname, put that **separate hostname**
  behind an identity-aware proxy protecting the console and every backend route.
  Keep the application token enabled. The A2A allowlist above deliberately does
  not provide browser access.

## Checklist

Before sharing the URL, verify the public card advertises the correct origin,
the unauthenticated A2A call returns `401`, and a token-authenticated call works.
Check that `/app`, `/api/config`, `/healthz`, `/metrics`, `/v1/models`, and an unknown path
return `404` through the public proxy. Repeat after changing proxy rules. Confirm
that the underlying agent port cannot bypass the proxy from the public network.

## See also

- [Delegates](/guides/delegates) — the A2A peer model these endpoints serve.
- [Headless](/guides/headless) — running an agent as a non-interactive service.
- [Deploy with Docker](/guides/deploy-docker) · [Customize & deploy](/guides/customize-and-deploy).
- [Fleet](/guides/fleet) — many agents discovering + calling each other.
