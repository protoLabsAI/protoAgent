# Docker deploy — config-as-code (seed + UI override)

A reference for running protoAgent in a container where:

- the agent boots **pre-configured** from a config baked into the image (the *seed*), and
- operators can still **override settings in the console**, and those edits **persist**.

It's the protoAgent-native flow — no force-overriding a live config, no setup wizard on a fresh instance.

## Files

| File | Role |
| --- | --- |
| `langgraph-config.seed.yaml` | Your agent's config — the **seed** (no secrets). |
| `Dockerfile` | `FROM protoagent`, bakes the seed, sets `PROTOAGENT_SEED_CONFIG`. |
| `docker-compose.yml` | Named config volume + `PROTOAGENT_HEADLESS_SETUP` + the model key / A2A token. |

## Run

```bash
export OPENAI_API_KEY=sk-...        # your model/gateway key
export A2A_AUTH_TOKEN=$(openssl rand -hex 24)
docker compose up -d --build
# console at http://localhost:7870/app  (paste A2A_AUTH_TOKEN to log in)
```

Edit a setting in the console → it persists. Reboot / `docker compose up -d` after an
image rebuild → your edits survive. Want to start clean from an updated seed?
`docker compose down && docker volume rm <project>_agent-config && docker compose up -d`.

See **[docs/guides/deploy-docker.md](../../docs/guides/deploy-docker.md)** for the full explanation.
