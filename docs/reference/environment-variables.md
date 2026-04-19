# Environment variables

Every env var the template reads at runtime.

## Required

| Variable | What |
|---|---|
| `OPENAI_API_KEY` | LiteLLM gateway master key (or direct provider key if not using a gateway). Read by `graph/llm.py`. |

## Identity

| Variable | Default | What |
|---|---|---|
| `AGENT_NAME` | `protoagent` | Short slug. Used as the Prometheus metric prefix, Langfuse trace tag, and in log labels. Should match what you used when forking. |
| `<AGENT_NAME>_API_KEY` | (unset — no auth) | Expected value of the `X-API-Key` header if you want to require auth on `/a2a` and `/v1/*`. Uppercased, non-alphanumeric → underscore. e.g. `MY_AGENT_API_KEY`. |

## Tracing (optional)

| Variable | What |
|---|---|
| `LANGFUSE_PUBLIC_KEY` | Langfuse project public key |
| `LANGFUSE_SECRET_KEY` | Langfuse project secret key |
| `LANGFUSE_HOST` | Langfuse host URL (e.g. `https://langfuse.company.com`). Falls back to `LANGFUSE_URL`, then `http://host.docker.internal:3001`. |

If both keys are unset, tracing is disabled and every helper in `tracing.py` becomes a no-op.

## Logging

| Variable | Default | What |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Python logging level. Valid: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

The template explicitly calls `logging.basicConfig(level=INFO)` — without this, Python's default WARNING would hide `logger.info(...)` lines like "webhook delivered", making A2A issues invisible in container logs.

## Push notifications / SSRF guard

| Variable | Default | What |
|---|---|---|
| `PUSH_NOTIFICATION_ALLOWED_HOSTS` | (empty) | Comma-separated hostnames that bypass the private-IP check when accepting webhook URLs. Example: `workstacean,automaker-server`. |
| `PUSH_NOTIFICATION_ALLOWED_CIDRS` | (empty) | Comma-separated CIDR blocks explicitly allowed. Example: `10.0.0.0/8,172.16.0.0/12`. |

Without these set, the handler rejects webhook URLs that resolve to private / loopback / link-local IPs — defends against SSRF where a client registers `http://169.254.169.254/...` or `http://10.0.0.1/...` as a callback.

## A2A security

| Variable | Default | What |
|---|---|---|
| `A2A_AUTH_TOKEN` | (unset — open) | Bearer token required on all `/a2a` requests. When set, the `Authorization: Bearer <token>` header must match. Logs a WARNING at startup when unset. |
| `A2A_ALLOWED_ORIGINS` | (unset — allow all) | Comma-separated list of allowed `Origin` header values for SSE and WebSocket streaming connections (e.g. `https://app.example.com,https://admin.example.com`). When unset, a WARNING is logged and all origins are accepted. Set to `*` to explicitly disable origin checking without a warning. Origin comparison is case-insensitive per RFC 6454. Requests without an `Origin` header (same-origin, curl, server-to-server) are always allowed. |

## UI

| Variable | Default | What |
|---|---|---|
| `GRADIO_SERVER_NAME` | `0.0.0.0` | Bind address for the Gradio UI. |
| `GRADIO_SERVER_PORT` | `7870` | Port for the Gradio UI. The A2A handler + metrics + agent card are all served on the same port. |

## Release pipeline (`scripts/post-release-notes.mjs`)

Read only during the Discord-release step of `release.yml`.

| Variable | What |
|---|---|
| `ANTHROPIC_API_KEY` | Claude Haiku rewrites raw commits as polished notes. If unset, raw commit subjects post instead. |
| `DISCORD_WEBHOOK_URL` | Discord channel webhook. If unset, notes print to stdout and don't leave CI. |
| `AGENT_NAME` | Embed title uses this name. |
| `AGENT_TAGLINE` | Footer tagline under the embed. |

## Not set by the template

The template deliberately doesn't read `GITHUB_TOKEN`, `DISCORD_BOT_TOKEN`, or any tool-specific credentials. Those belong in your fork's tools, not the shared runtime.
