# Deploy in Docker (config-as-code: seed + UI override)

Run protoAgent in a container so it boots **pre-configured** from a config baked into the image (the *seed*), while operators can still **override settings in the console** and have those edits **persist**. No setup wizard on a fresh instance, no force-overriding live edits.

The copy-me reference is **[`examples/docker`](https://github.com/protoLabsAI/protoAgent/tree/main/examples/docker)**:

```bash
cp -r examples/docker my-agent && cd my-agent
# edit langgraph-config.seed.yaml, then:
export OPENAI_API_KEY=sk-... A2A_AUTH_TOKEN=$(openssl rand -hex 24)
docker compose up -d --build
```

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

## Binding & auth

protoAgent refuses to bind `0.0.0.0` with an **open** operator API (`/api/*`, `/v1/*` include plugin-install + config rewrite). Pick one:

- set `A2A_AUTH_TOKEN` and send it as `Authorization: Bearer <token>` (recommended);
- bind `127.0.0.1` (single-host);
- or, only behind a trusted network boundary, `PROTOAGENT_ALLOW_OPEN=1`.

## Day-2

| Want to… | Do |
| --- | --- |
| Change a setting | Edit it in the console — it persists on the config volume. |
| Roll out a new image | `docker compose pull && docker compose up -d` — live config (your edits) is preserved. |
| Re-seed from an updated seed | `docker compose down && docker volume rm <project>_agent-config && docker compose up -d`. |
| Inspect the effective config | `GET /api/config` (or `/healthz` for `setup_complete`). |

## Reference

- `PROTOAGENT_SEED_CONFIG` — file to seed the live config from on first boot (config-as-code).
- `PROTOAGENT_CONFIG_DIR` — where the live config + setup marker live (default `/opt/protoagent/config`).
- `PROTOAGENT_HEADLESS_SETUP` — validate the seed + auto-complete setup (no wizard).
- `PROTOAGENT_UI` — `console` (default) serves the operator console at `/app`.
