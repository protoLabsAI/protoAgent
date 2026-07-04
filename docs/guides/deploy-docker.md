# Deploy in Docker (config-as-code: seed + UI override)

Run protoAgent in a container so it boots **pre-configured** from a config baked into the image (the *seed*), while operators can still **override settings in the console** and have those edits **persist**. No setup wizard on a fresh instance, no force-overriding live edits.

The copy-me reference is **[`examples/docker`](https://github.com/protoLabsAI/protoAgent/tree/main/examples/docker)**:

```bash
cp -r examples/docker my-agent && cd my-agent
# edit langgraph-config.seed.yaml, then:
export OPENAI_API_KEY=sk-... A2A_AUTH_TOKEN=$(openssl rand -hex 24)
docker compose up -d --build
```

## One-command install

For a fresh machine you just SSH'd into — no clone, no Python, no hand-written
`docker run`:

```bash
curl -fsSL https://raw.githubusercontent.com/protoLabsAI/protoAgent/main/scripts/install.sh | sh
```

[`scripts/install.sh`](https://github.com/protoLabsAI/protoAgent/blob/main/scripts/install.sh)
is versioned in the repo (so it tracks the agent) and:

1. **Checks prerequisites** — Docker (+ a running daemon) and curl.
2. **Pulls** `ghcr.io/protolabsai/protoagent:latest`.
3. **Runs** it — published to **loopback** (`127.0.0.1:7870`), a named data
   volume (`protoagent-sandbox`), `--restart unless-stopped`, and `PROTOAGENT_UI=console`
   so the console serves at `/app`.
4. **Runs a CLI wizard** that drives the **same `/api/config/*` endpoints as the
   browser setup wizard** — provider gateway URL, API key (silent), model
   (fetched + validated live), and agent name — so it stays in parity for free.
5. **Prints** where the agent is running.

It's **idempotent**: re-running pulls the latest image, keeps the data volume,
and offers to re-run the wizard. Over a **plain SSH session with no TTY** it
starts the container and points you at `/app` to finish setup in a browser.

**Serving the vanity URL.** The one-liner above uses the GitHub-raw URL, which
works today. To serve it from `https://agent.protolabs.studio/install.sh`
instead, point that path at the raw file (a CDN/redirect or a one-line reverse
proxy) — the script content is identical.

**Overrides** (all optional env vars):

| Var | Default | Purpose |
|-----|---------|---------|
| `PROTOAGENT_PORT` | `7870` | Host port |
| `PROTOAGENT_BIND` | `127.0.0.1` | Host bind address (widen only with `A2A_AUTH_TOKEN`) |
| `PROTOAGENT_VOLUME` | `protoagent-sandbox` | Data volume name |
| `PROTOAGENT_IMAGE` | `ghcr.io/protolabsai/protoagent:latest` | Image ref |
| `PROTOAGENT_INSTALL_URL` | — | Configure an **already-running** instance (skips Docker) |
| `PROTOAGENT_INSTALL_NONINTERACTIVE` | — | `1` = start only, never prompt |
| `A2A_AUTH_TOKEN` | — | Bearer required to widen the bind past loopback |

> **Architecture note.** The published image is currently **linux/amd64**. On
> Apple Silicon / arm64 the installer targets amd64 explicitly so Docker Desktop
> runs it under emulation (a slower first boot); native-ARM performance needs a
> multi-arch image or a local build. On amd64 hosts it runs natively.

For a **pre-configured, config-as-code** deploy (bake settings into your own
image, no wizard) use the seed pattern below instead.

## The one trap to avoid

The image declares `VOLUME /opt/protoagent/config`. That's deliberate — it persists wizard/console edits — but it means a config **volume** holds the live `langgraph-config.yaml`. So if you bake your config **as the live file** (`COPY my-config.yaml /opt/protoagent/config/langgraph-config.yaml`), the volume freezes your first-boot copy and **silently shadows every later image update**: enabling a plugin in a new image just… does nothing. Don't bake the live file.

## The pattern

**1. Bake your config as a *seed*, not the live file.** Put it on a plain (non-volume) path and point `PROTOAGENT_SEED_CONFIG` at it:

```dockerfile
FROM ghcr.io/protolabsai/protoagent:latest
COPY langgraph-config.seed.yaml /opt/agent/seed/langgraph-config.yaml
ENV PROTOAGENT_SEED_CONFIG=/opt/agent/seed/langgraph-config.yaml
```

On first boot, protoAgent copies the seed to the live `langgraph-config.yaml` and **never clobbers it afterward** (`ensure_live_config` is idempotent). Updating the seed in a new image re-seeds only a **fresh** instance — an existing one keeps its live config.

**2. Persist the live config on a *named* volume** (not the image's anonymous one):

```yaml
volumes:
  - agent-config:/opt/protoagent/config
```

Console/settings edits write here and survive reboots + image rolls.

**3. Skip the wizard on a fresh instance** with `PROTOAGENT_HEADLESS_SETUP=1` — protoAgent validates the seed and auto-marks setup complete, so the instance comes up configured. Omit it if you'd rather complete setup interactively in the wizard.

**4. Keep secrets in the env, not the seed.** The model key is read from `OPENAI_API_KEY`; the seed (and your image) carry no credentials. In the console the api-key field shows blank (`api_key_configured: false`) — that's expected, the key is env-sourced.

## Baking a persona (SOUL.md)

The persona has the **same** seed/live split as the config — and the same trap. The live `SOUL.md` sits under the config volume (`<instance_root>/config/SOUL.md`), and `read_soul` only falls back to the bundled `config/SOUL.md` when the live file is **absent**. So a placeholder materialised into the live path on an early boot (e.g. by a finished setup wizard) will **silently shadow** any persona you bake into the image later — the agent runs "Replace this file" forever, even after you `COPY` a real `SOUL.md` into the bundle.

`ensure_live_soul` closes that gap on boot, seed-not-force like the config:

- **Absent** live SOUL → seed it (so it's present and console-editable).
- **Still the shipped starter placeholder** → heal it — replace with your baked persona.
- **A real, authored SOUL** → never touched.

Two ways to bake the persona, pick one:

```dockerfile
# a) Overwrite the bundled seed the agent falls back to:
COPY SOUL.md /opt/protoagent/config/SOUL.md
# b) Or point at a persona-as-code seed on a plain path (wins over the bundle):
COPY persona.seed.md /opt/agent/seed/SOUL.md
ENV PROTOAGENT_SEED_SOUL=/opt/agent/seed/SOUL.md
```

To **repair a running instance** whose live SOUL is already a stale placeholder without a redeploy, write the live file directly: `POST /api/config {"soul": "<persona text>"}` (writes the live `SOUL.md` and hot-reloads the graph, no restart).

## Binding & auth

protoAgent refuses to bind `0.0.0.0` with an **open** operator API (`/api/*`, `/v1/*` include plugin-install + config rewrite). Pick one:

- set `A2A_AUTH_TOKEN` and send it as `Authorization: Bearer <token>` (recommended);
- bind `127.0.0.1` (single-host);
- or, only behind a trusted network boundary, `PROTOAGENT_ALLOW_OPEN=1`.

### Where the operator token lives

Configure the token in the **server's environment** — `A2A_AUTH_TOKEN` (or `auth.token` in
`langgraph-config.yaml`). That's the credential's home: it never lives in a browser, and
rotating it instantly invalidates every client.

The **browser console** has to authenticate too, so when you paste the token into its
sign-in prompt it's cached in that browser's `localStorage`. Know the trade-off:

- A script injected into the console's origin (XSS) could read that cached token and
  exfiltrate it. The exposure is bounded by the default posture — the console binds
  `127.0.0.1` and the whole API is default-deny bearer-gated — and the console renders
  agent/model output only through sanitized markdown (no raw-HTML sink). Treat the cached
  token like any browser-stored credential: don't expose the console beyond localhost
  without a fronting auth proxy, and rotate `A2A_AUTH_TOKEN` if a workstation is compromised.
- It stays in `localStorage` deliberately. An httpOnly cookie can't authenticate the
  **desktop app** — its Tauri webview and the local HTTP sidecar are different origins, so a
  `SameSite` cookie isn't sent cross-origin and `SameSite=None` needs the HTTPS the localhost
  sidecar doesn't have — so a cookie would protect only the browser. And hashing/encrypting
  the value at rest doesn't defend against same-origin XSS: a script in the page can read the
  key and reuse the same code path the console uses to send the token. The effective lever,
  if the console is ever exposed beyond localhost, is an egress limit (a CSP `connect-src`
  allowlist) that blocks exfiltration for both the browser and the desktop.

## Expose it with a tunnel (ngrok / Cloudflare)

To reach the agent on a **public hostname** without opening a router port, front it with a
tunnel. The tunnel terminates TLS and forwards to the container's published port; protoAgent
itself can stay bound to `127.0.0.1`. Two non-negotiables when you do this:

- **Keep `A2A_AUTH_TOKEN` set.** A tunnel makes the *whole* operator API
  (`/api/*` plugin-install + config rewrite, `/v1/*`, `/a2a`) internet-reachable — the bearer
  gate is the only thing fencing it. (For LAN/tailnet-only access, prefer
  [Tailscale](/guides/phone-access#tailscale-reach-it-from-anywhere) — no public surface at all.)
- **Set [`A2A_PUBLIC_URL`](/reference/environment-variables#a2a-agent-card-endpoint)** to the
  tunnel hostname, so the agent card advertises the address peers actually use (not the bound
  loopback port).

**ngrok** — ephemeral hostname, good for a quick share or a phone demo:

```bash
ngrok http 7870
# → Forwarding https://abc123.ngrok-free.app -> http://localhost:7870
export A2A_PUBLIC_URL=https://abc123.ngrok-free.app
```

**Cloudflare Tunnel** (`cloudflared`) — a stable hostname mapped to your domain, no inbound
ports:

```bash
# quick, throwaway URL:
cloudflared tunnel --url http://localhost:7870
# or a named tunnel routed to agent.example.com, then:
export A2A_PUBLIC_URL=https://agent.example.com
```

Because the console authenticates over the tunnel too, add the tunnel origin to
[`A2A_ALLOWED_ORIGINS`](/reference/environment-variables#streaming-origin-verification) if
you've enabled origin verification — otherwise the SSE/WebSocket streams it relies on get a
`403`. For a second factor in front of the bearer token, layer the tunnel's own access
control (Cloudflare Access, an ngrok OAuth policy, or a fronting auth proxy) — the
`localStorage`-cached token above is *all* the app-level auth there is.

## Day-2

| Want to… | Do |
| --- | --- |
| Change a setting | Edit it in the console — it persists on the config volume. |
| Roll out a new image | `docker compose pull && docker compose up -d` — live config (your edits) is preserved. |
| Re-seed from an updated seed | `docker compose down && docker volume rm <project>_agent-config && docker compose up -d`. |
| Inspect the effective config | `GET /api/config` (or `/healthz` for `setup_complete`). |

## Reference

- `PROTOAGENT_SEED_CONFIG` — file to seed the live config from on first boot (config-as-code).
- `PROTOAGENT_SEED_SOUL` — file to seed the live `SOUL.md` persona from (persona-as-code); also heals a lingering starter placeholder. Falls back to the bundled `config/SOUL.md`.
- `PROTOAGENT_CONFIG_DIR` — where the live config + setup marker live (default `/opt/protoagent/config`).
- `PROTOAGENT_HEADLESS_SETUP` — validate the seed + auto-complete setup (no wizard).
- `PROTOAGENT_UI` — `console` (default) serves the operator console at `/app`.
