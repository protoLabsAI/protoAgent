# Deploy in Docker {#deploy-in-docker-config-as-code-seed-ui-override}

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
pulls the published image, creates a `protoagent-sandbox` volume, publishes
`127.0.0.1:7870`, and starts the console. With a terminal it prompts for model
setup; without one, finish in the browser at `/app`.

Re-running replaces the container and preserves the selected volume. Re-supply
any image, bind, port, volume, and token overrides on each run; the script does
not recover them from the old container.

**Token-protected installations:** the installer's HTTP helpers currently omit
the bearer header ([tracked in #4315](https://github.com/protoLabsAI/protoAgent/issues/4315)),
so its setup-status probe returns `401` and eventually reports
“did not come up in time,” even if the container started. The wizard cannot
configure a protected instance either. Use the Compose example above for a
protected deployment. If the installer already started it, inspect `docker logs
protoagent` and finish setup through `/app` with your token. Do not remove the
token to make the probe pass.

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

The server listens on `0.0.0.0` **inside the container**. The example's
`127.0.0.1:7870:7870` mapping separately limits the **host** port to loopback.
Set `A2A_AUTH_TOKEN` for the API and console; the example requires it. Remote
clients send `Authorization: Bearer <token>`.

Without a token, a non-loopback server bind is refused unless
`PROTOAGENT_ALLOW_OPEN=1` explicitly opts into open mode. Use that override only
with an enforced network boundary. Open instances also check host names and
request origins; see [Security & trust](/explanation/security-and-trust#an-instance-with-no-token)
and [trusted hosts](/reference/environment-variables).

### Where the operator token lives

Set `A2A_AUTH_TOKEN` in the environment or `auth.token` in the live configuration.
Restart after changing an environment-sourced token and update clients. The
console caches the token in browser `localStorage`; use a trusted origin.

## Expose it with a tunnel (ngrok / Cloudflare)

For private console access, use [phone access](/guides/phone-access). For a public
A2A endpoint, follow [Expose to the world](/guides/exposing-protoagent): terminate
TLS, forward an exact route allowlist, require a token, and set `A2A_PUBLIC_URL`
in the container's environment. A tunnel forwarding the entire port also
publishes the operator API; a public A2A hostname should not do that.

## Node for plugins and `npx` servers

The image doesn't bake Node. Plugins that declare `requires_runtimes: {node: ">=20"}`, the
`npx`-based MCP servers and the ACP coding agents use the **managed Node runtime**
(ADR 0085). It downloads on demand, either from the plugin's **Install Node runtime** banner
button or with `protoagent runtime install-node` inside the container. It lands in
`<box root>/runtime/node`, which is `/sandbox/runtime/node` in the container, so the same
`/sandbox` volume that holds your config keeps it across restarts and image upgrades.
Check where the box root is with `protoagent config explain`.

## Day-2

| Want to… | Do |
| --- | --- |
| Change a setting | Edit it in the console — it persists on the `/sandbox` volume. |
| Update the seed image (`build: .`) | `docker compose build --pull && docker compose up -d` — rebuilds your image against the current base. |
| Update a prebuilt image (`image:` only) | `docker compose pull && docker compose up -d` — pulls the configured tag. |
| Re-seed from an updated seed | Set `PROTOAGENT_SEED_MERGE=1` and roll normally — image-owned keys re-apply, operator edits stay. Without it, change the live setting explicitly; removing the sandbox volume also deletes chats, stores, and credentials. |
| Inspect paths and settings | `docker compose exec agent protoagent config explain`; use authenticated `GET /api/config` for the live config. |
| Check readiness | `/healthz` returns `200` after setup and graph initialization; inspect logs if it remains `503`. |
| Back up or restore | Follow [Docker backups](/guides/backup-and-restore#docker-backups) before upgrading. |

## Reference

See [environment variables](/reference/environment-variables) for seed, path,
setup, and UI options, and [shutdown behavior](/guides/deploy#shutdown-semantics)
before choosing an automatic update policy.
