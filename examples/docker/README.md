# Docker deployment with a config seed

This example bakes non-secret settings into an image and persists the live config
and stores under `/sandbox`. Edit `langgraph-config.seed.yaml` before starting.

## Run

```bash
export OPENAI_API_KEY=sk-...
export A2A_AUTH_TOKEN=$(openssl rand -hex 24)
docker compose up -d --build
```

Open <http://localhost:7870/app> and enter the operator token. Use
`docker compose logs agent` to inspect startup failures.

| File | Role |
| --- | --- |
| `langgraph-config.seed.yaml` | First-boot settings, without credentials |
| `Dockerfile` | Bakes the seed and selects it with `PROTOAGENT_SEED_CONFIG` |
| `docker-compose.yml` | Persists `/sandbox`, sets auth, and enables the console |

Console edits survive image updates. To apply later seed changes while preserving
operator overrides, set `PROTOAGENT_SEED_MERGE: "1"` in the compose environment.
Removing the sandbox volume deletes all instance state, including chats and secrets.

See [Deploy in Docker](../../docs/guides/deploy-docker.md) for seed merging,
personas, authentication, and tunnels.
