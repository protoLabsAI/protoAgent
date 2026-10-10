# Deploy in Docker (config-as-code: seed + UI override)

Run the published image, or bake a config seed into your own image while keeping
console edits in a persistent data volume. You need Docker and access to a model
endpoint.

For a configured deployment, copy
[`examples/docker`](https://github.com/protoLabsAI/protoAgent/tree/main/examples/docker):

```bash
cp -r examples/docker my-agent
cd my-agent
# Edit langgraph-config.seed.yaml, then:
export OPENAI_API_KEY=sk-...
export A2A_AUTH_TOKEN=$(openssl rand -hex 24)
docker compose up -d --build
```

Open <http://localhost:7870/app> and enter the operator token. Confirm `/healthz`
returns `200` after the model connects. Use `docker compose logs agent` to inspect
a failed boot or connection.

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
   (fetched + validated live), and agent name — without a separate config format.
5. **Prints** where the agent is running.

It's **idempotent**: re-running pulls the latest image, keeps the data volume,
and offers to re-run the wizard. Over a **plain SSH session with no TTY** it
starts the container and points you at `/app` to finish setup in a browser.

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

The bundled entrypoint sets `PROTOAGENT_HOME=/sandbox`; the live config is
`/sandbox/config/langgraph-config.yaml`. Persist `/sandbox` with a named volume.
A file baked at `/opt/protoagent/config/langgraph-config.yaml` is not the active
config, and an existing live file takes precedence over first-boot seeds.

Bake a seed outside the data volume and select it with `PROTOAGENT_SEED_CONFIG`.
Update an existing instance through Settings or use merge-on-boot below.

## The pattern

**1. Bake your config as a *seed*, not the live file.** Put it on a plain (non-volume) path and point `PROTOAGENT_SEED_CONFIG` at it:

```dockerfile
FROM ghcr.io/protolabsai/protoagent:latest
COPY langgraph-config.seed.yaml /opt/agent/seed/langgraph-config.yaml
ENV PROTOAGENT_SEED_CONFIG=/opt/agent/seed/langgraph-config.yaml
```

On first boot, protoAgent copies the seed to the live config. Existing instances
keep their settings when you update the seed. Use merge-on-boot below to apply
seed changes to those instances.

**2. Persist the instance data on a named volume:**

```yaml
services:
  agent:
    volumes:
      - agent-sandbox:/sandbox
volumes:
  agent-sandbox:
```

This preserves config, conversations, credentials, and plugin stores across
container replacements.

**3. Skip the wizard on a fresh instance** with `PROTOAGENT_HEADLESS_SETUP=1`.
The server validates the seed and marks setup complete. Omit this variable to
complete setup in the browser.

**4. Supply credentials through environment variables.** The example's legacy
`model` block reads `OPENAI_API_KEY`. Explicit `providers` entries use per-connection credentials from `secrets.yaml`,
which you can set through **Settings → Model → Connections**. Do not assume a
provider entry inherits `OPENAI_API_KEY`. Keep secrets out of the seed and image.

## Merge-on-boot: keeping a declarative seed live

To update image-owned settings while preserving operator edits, enable:

```yaml
environment:
  PROTOAGENT_SEED_MERGE: "1"
```

On every boot the seed is re-applied against the live config, **per key**:

| in the live config | result |
|---|---|
| key is absent | take the seed's value — a newly-baked block appears |
| key is untouched since the last roll | take the seed's value — it tracks the image |
| key was edited by an operator | **keep the operator's value** — never clobbered |
| key was dropped from the seed, untouched | removed, so config falls back to its default |

The server records the applied seed in `<config-dir>/.seed-applied.yaml` and
uses that baseline to distinguish image changes from operator edits.

- Without the flag, config is seeded once.
- On the first merge boot of an existing volume, existing keys are treated as
  operator-owned; only new keys are added. Later boots use the recorded baseline.
- Secret fields are skipped. Supply credentials through the environment or
  `secrets.yaml`.
- Only the explicit `PROTOAGENT_SEED_CONFIG` participates, not the bundled template.
- Unknown sections and YAML comments in the live file survive.
- A malformed seed is logged and leaves the live config untouched.

## Baking a persona (SOUL.md)

The live persona is `<instance_root>/config/SOUL.md`. On boot,
`ensure_live_soul` seeds a missing file or replaces the shipped placeholder.
An authored live persona is preserved, so updating a baked seed will not
overwrite it.

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

An open instance (no token) also only answers requests addressed to a host name it
recognizes: IP literals, `localhost`, `*.ts.net` and the names in `PROTOAGENT_TRUSTED_HOSTS`
(see [Environment variables](/reference/environment-variables)). Browsing the bundled
compose's `127.0.0.1` publish works as-is. If other containers call an open agent by its
service name (`http://agent:7870`), add that name: `PROTOAGENT_TRUSTED_HOSTS=agent`.
In open mode, `POST`s to `/a2a` and `/v1/*` must also send `Content-Type: application/json`
(every protoAgent and OpenAI-compatible client already does). A browser page on another origin
can't change state there either. See
[Security & trust](/explanation/security-and-trust#an-instance-with-no-token).

### Where the operator token lives

Set `A2A_AUTH_TOKEN` in the server environment, or `auth.token` in the live
config. Restart after changing an environment-sourced token; clients must then
use the new token.

The browser console stores the token in `localStorage` after sign-in. A script
running on that origin can read it, so use a trusted origin and rotate the token
if the workstation is compromised. For public access, put an authentication proxy
in front of the console; see [Expose to the world](/guides/exposing-protoagent).

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
| Change a setting | Edit it in the console — it persists on the `/sandbox` volume. |
| Roll out a new image | `docker compose pull && docker compose up -d` — live config (your edits) is preserved. |
| Re-seed from an updated seed | Set `PROTOAGENT_SEED_MERGE=1` and roll normally — image-owned keys re-apply, operator edits stay. Without it, change the live setting explicitly; removing the sandbox volume also deletes chats, stores, and credentials. |
| Inspect the effective config | `GET /api/config` (or `/healthz` for `setup_complete`). |

## Reference

- `PROTOAGENT_SEED_CONFIG` — file to seed the live config from on first boot (config-as-code).
- `PROTOAGENT_SEED_MERGE` — `1` to **re-apply** that seed's image-owned keys on *every* boot instead of only the first. See [Merge-on-boot](#merge-on-boot-keeping-a-declarative-seed-live). Unset (the default) keeps seed-once.
- `PROTOAGENT_SEED_SOUL` — file to seed the live `SOUL.md` persona from (persona-as-code); also heals a lingering starter placeholder. Falls back to the bundled `config/SOUL.md`.
- `PROTOAGENT_HOME` — the instance root; the bundled entrypoint sets `/sandbox`, with config and setup marker under `/sandbox/config/`.
- `PROTOAGENT_HEADLESS_SETUP` — validate the seed + auto-complete setup (no wizard).
- `PROTOAGENT_UI` — `console` serves `/app`; the published image defaults to `none`, so the example compose file explicitly selects `console`.
