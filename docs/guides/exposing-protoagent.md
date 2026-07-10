# Exposing a protoAgent to the world

Most protoAgents run private — on a LAN or a tailnet, reachable only by the operator
and the rest of the fleet. Sometimes you want one reachable from the public internet:
an agent other people (or other orgs' agents) can call over [A2A](/guides/delegates),
a card discoverable at a stable URL. This guide is how to do that **without handing the
internet a code-execution box**.

The one-sentence version: **expose only the A2A surface, gate it with a bearer token,
and 404 everything else.** The rest is detail.

## The threat model — know what each surface is

A protoAgent serves several HTTP surfaces at the root, and they are **not** equally safe
to expose:

| Surface | What it is | Safe to expose publicly? |
| --- | --- | --- |
| `/.well-known/agent-card.json` | The A2A agent card — public identity + skills, for discovery | **Yes** — it's designed to be read by anyone |
| `/a2a` | The A2A JSON-RPC endpoint — send the agent messages/tasks | **Yes, but only token-gated** |
| `/health` | Liveness probe | Yes (harmless) |
| `/v1/*` | OpenAI-compatible API — chat/completions against the agent | Only if you *want* an OpenAI-compatible public API, and only token-gated |
| `/app` | The operator **console** (React UI) | **No** |
| `/api/*` | The operator API — config, sessions, **code execution**, file access | **Never** |

`/api/*` and the tools behind the console can run code, read files, and change the
agent's configuration. `/app` is the door to them. Publishing either to the internet is
equivalent to publishing a remote shell. **The whole game is keeping `/app` and `/api`
off the public net while letting `/a2a` through.**

## The pattern

1. **Bind the app private.** Run the agent bound to loopback or a private/tailnet
   interface — never `0.0.0.0` on a public IP. A reverse proxy or tunnel is the only
   thing that reaches it from outside.
2. **Put a reverse proxy / tunnel in front** (Cloudflare Tunnel, nginx, Caddy, …) and
   have it forward **only** `/.well-known/*`, `/a2a*`, and `/health` to the agent.
   **404 everything else** — a catch-all so `/app`, `/api`, `/v1` (unless you deliberately
   want it), and any future route are invisible from the public hostname.
3. **Require a bearer token.** Set `A2A_AUTH_TOKEN` to a strong secret. protoAgent then
   enforces `Authorization: Bearer <token>` on `/a2a`, `/v1`, and `/api`. Without it a
   public bind is refused; with it, the card stays public (discovery) but every *action*
   needs the token.
4. **Advertise the real URL.** Set `A2A_PUBLIC_URL` to the public origin
   (`https://agent.example.com`) so the card advertises a reachable interface, not a
   loopback address. Keep `a2a.require_routable_url: true` in config so the agent
   **refuses to start** if the card would advertise a loopback URL — a boot-time guard
   against a misconfigured proxy silently publishing an undiscoverable or wrong card.
5. **Keep the operator surface on the private net.** Reach `/app` + `/api` over the
   tailnet/LAN (a separate private bind), never through the public proxy.

That's defense in three layers: the proxy only routes the safe paths, the 404 catch-all
hides the rest, and the bearer token gates the actions on the paths that *are* routed.

## Worked example — Cloudflare Tunnel

This is the real setup for `ava.proto-labs.ai` (the fleet's orchestrator). A `cloudflared`
tunnel fronts the agent; the agent's container is only reachable inside the Docker network
and over the tailnet, never on a public port.

Ingress rules (`cloudflared` `config.yaml`) — order matters, first match wins:

```yaml
ingress:
  # Public A2A surface only → the agent container (reached by name on the shared net).
  - hostname: agent.example.com
    path: /.well-known/.*
    service: http://ava:7870
  - hostname: agent.example.com
    path: /a2a.*
    service: http://ava:7870
  - hostname: agent.example.com
    path: /health
    service: http://ava:7870
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
  ava:
    image: ghcr.io/you/ava:latest
    expose: ["7870"]                  # NOT `ports:` on a public IP — internal only
    environment:
      A2A_PUBLIC_URL: https://agent.example.com
      A2A_AUTH_TOKEN: ${AVA_A2A_TOKEN:?a public endpoint must not boot without a token}
      PROTOAGENT_UI: console          # console served, but only reachable privately
    networks: [ai]                    # the same external network cloudflared is on
    # (bind the operator console to the tailnet with a separate published port if wanted:
    #  ports: ["100.x.y.z:7876:7870"]  → http://host:7876/app over the tailnet only)
```

Because `cloudflared` shares the `ai` network, it reaches the container as
`http://ava:7870` — no public port on the host at all. DNS for the hostname points at the
tunnel (`cloudflared tunnel route dns <tunnel> agent.example.com`); the edge terminates TLS.

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
    -H "Authorization: Bearer $AVA_A2A_TOKEN" -H 'content-type: application/json' \
    -d '{"jsonrpc":"2.0","id":"1","method":"message/send",
         "params":{"message":{"role":"user","messageId":"m1",
         "parts":[{"kind":"text","text":"who are you?"}]}}}'
```

Run these four checks every time you expose an agent — the 404s on `/app` and `/api` are
the ones that matter.

## Generic reverse proxy (nginx / Caddy)

Same shape without a tunnel — allowlist the safe paths, default-deny the rest. Caddy:

```
agent.example.com {
    @public path /a2a* /.well-known/* /health
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

The above is the floor. On top of it:

- **Edge WAF / rate limiting.** A proxy like Cloudflare gives you managed rules, bot
  filtering, and rate limits at the edge for free. Turn on rate limiting on `/a2a` — an
  agent endpoint is a natural target for abuse.
- **A strong, rotated token.** `openssl rand -hex 32`. Store it in a secrets manager, not
  the compose file. Rotate it if it ever lands in a log or a transcript.
- **Zero-Trust for the operator surface.** If you ever need `/app` reachable off the
  tailnet, put it behind an identity gate (e.g. Cloudflare Access / an SSO proxy) — never
  behind just the bearer token. The console is an operator tool; treat it like SSH.
- **Scope the agent's power.** A public agent should be *least-privilege*: `allow_run:
  false` (no shell), read-mostly tools, no write credentials it doesn't need. If it
  delegates, it presents downstream tokens — those are separate secrets, scoped per peer.
- **Isolate the network.** The container should reach only what it needs (the model
  gateway, its delegates). Don't give a public-facing agent a route to your whole LAN.

## Checklist

- [ ] Agent bound private (loopback/tailnet), **no** public host port.
- [ ] Proxy/tunnel forwards **only** `/.well-known/*`, `/a2a*`, `/health`.
- [ ] Per-host **404 catch-all** hides `/app`, `/api`, `/v1`.
- [ ] `A2A_AUTH_TOKEN` set to a strong secret (from a secrets manager).
- [ ] `A2A_PUBLIC_URL` = the public origin; `require_routable_url: true`.
- [ ] Verified from outside: card `200`, `/a2a` no-token `401`, `/app` `404`, `/api` `404`.
- [ ] Least-privilege tools; `allow_run: false` unless truly needed.
- [ ] (Recommended) edge rate-limiting; operator console behind identity, not just the token.

## See also

- [Delegates](/guides/delegates) — the A2A peer model these endpoints serve.
- [Headless](/guides/headless) — running an agent as a non-interactive service.
- [Deploy with Docker](/guides/deploy-docker) · [Customize & deploy](/guides/customize-and-deploy).
- [Fleet](/guides/fleet) — many agents discovering + calling each other.
