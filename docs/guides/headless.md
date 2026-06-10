# Run headless (API + A2A, no UI)

protoAgent is an **API-first agent server**. The web console is optional — run it
headless and drive it entirely over HTTP: the **OpenAI-compatible** chat API, the
**A2A** protocol, or both at once. Same agent, same tools/skills/memory/goals — just
no browser.

## UI tiers (ADR 0010)

One flag picks how much UI is served; the **API + A2A always run**.

| `--ui` | Serves | For |
| --- | --- | --- |
| `console` *(default)* | React console + API + A2A | local dev, the desktop sidecar |
| **`none`** | **API + A2A + `/metrics` only** | **headless servers, fleets, CI** |

`full` is still accepted as a **deprecated alias for `console`** (it logs a
deprecation warning and behaves as `console`) — the old Gradio chat tier it once
named has been removed.

```bash
python -m server --ui none --host 0.0.0.0 --port 7870
# or: PROTOAGENT_UI=none python -m server
```

(`--host 0.0.0.0` to accept non-localhost traffic in a container; set an auth token
first — see Auth.)

### Headless setup

No wizard needed. Provision the config (`config/langgraph-config.yaml` +
`config/secrets.yaml`) and mark setup complete in one shot, then serve:

```bash
python -m server --setup     # validate the live config + mark setup, then exit
python -m server --ui none   # serve
```

## Drive it via the OpenAI API

A drop-in `POST /v1/chat/completions` (+ `GET /v1/models`). Point any OpenAI client at
the base URL; the "model" is the agent itself.

```bash
curl http://localhost:7870/v1/chat/completions \
  -H "Authorization: Bearer $PROTOAGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"summarize today’s PRs"}]}'
```

Pass `"stream": true` for an SSE token stream (OpenAI chunk shape). With the OpenAI
SDK: set `base_url` to `http://<host>:7870/v1` and `api_key` to your bearer token.

## Drive it via A2A

protoAgent is a first-class [A2A 1.0](https://a2a-protocol.org) server — the way fleets
of agents talk to each other.

- **Agent card:** `GET /.well-known/agent-card.json` (capabilities, skills, extensions).
- **JSON-RPC:** `POST /a2a` (`message/send`, `message/stream`, `tasks/*` lifecycle, push
  notifications).

Point another protoAgent (or any A2A client) at `http://<host>:7870` and it can delegate
to this one — see [Delegates](./delegates.md).

## Auth

Set an auth token and the API/A2A require `Authorization: Bearer <token>`:

```yaml
# config/secrets.yaml
auth:
  token: "your-strong-token"
```

Localhost with no token is open for dev convenience; **always set a token when binding to
`0.0.0.0` or exposing the agent.**

## What else runs headless

Everything non-UI: **`/metrics`** (Prometheus), the **reactive inbox**
(`POST /api/inbox` for webhooks/cron/sister agents), the **scheduler**, goals, plugins,
and managed MCP servers. The agent is fully operational without a screen.

See [ADR 0010](../adr/0010-headless-setup-and-ui-tiers.md), [Delegates](./delegates.md),
ADR [0003](../adr/0003-reactive-agent-activity-thread.md) (reactive inbox).
