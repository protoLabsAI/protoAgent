# A2A endpoints

Every endpoint the A2A handler exposes. Served on port 7870 by default.

## Well-known paths

| Path | Returns |
|---|---|
| `GET /.well-known/agent-card.json` | The agent card as JSON |
| `GET /.well-known/agent.json` | Alias for the card; some clients expect this path |

Both paths return identical content. Serving both is a spec compatibility hedge — early A2A clients (including older `@a2a-js/sdk` versions) probed different paths.

## Fleet multi-tenancy (`/agents/<slug>/…`)

A **hub** instance (one running a fleet of workspace members — [ADR 0042](/adr/0042-fleet-supervisor-unified-console)) hosts each member as an independently-addressable A2A **tenant** under a URL prefix, implementing the [A2A URL-based routing](https://a2a-protocol.org/latest/topics/multi-tenancy/) pattern. The hub reverse-proxies `/agents/<slug>/*` to the member's own server, so every path in this reference is reachable per-member:

| Path | Reaches |
|---|---|
| `GET /agents/<slug>/.well-known/agent-card.json` | the **member's** card |
| `POST /agents/<slug>/a2a` | the **member's** JSON-RPC endpoint (its task queue) |

`<slug>` is the member's workspace id (e.g. `matt-7689`); `host` targets the hub itself. Routing to a member is pure transport — **the hub agent's own reasoning loop is not invoked** — so dispatching to a member's tenant path is a *direct* call, not a `delegate_to` through the hub agent.

Because a member self-advertises this tenant URL in its card (see [`supportedInterfaces`](/reference/agent-card#supportedinterfaces)), a peer that **discovers** the member's card routes to it correctly with no special client logic — the spec's "read the card, follow its URL" contract. The hub is the **single auth boundary** (ADR [0089](/adr/0089-intra-instance-trust-boundary)): a caller presents any credential the hub accepts — its `A2A_AUTH_TOKEN`, an API key, or a per-device token — to `/agents/<slug>/a2a`. The hub authenticates it, and the proxy then **swaps** the `Authorization` header for the internal **fleet service token** before forwarding to the loopback-bound member. It does not forward the caller's credential verbatim, because each member has its own `instance_root` and therefore its own device registry and `auth.token` — a hub-minted device token would be structurally 401'd by every sister. The swap is **operator-tier only** and preserves the member-public anonymization, so unauthenticated callers still reach only a member's declared public paths.

## JSON-RPC methods (POST /a2a)

All methods use JSON-RPC 2.0 envelopes:

```json
{
  "jsonrpc": "2.0",
  "id": "<caller-chosen>",
  "method": "<name>",
  "params": { ... }
}
```

### `message/send` — blocking

Submit a message and wait for the terminal task. Returns the full Task object including artifacts.

```json
{
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"text": "summarize https://example.com"}]
    },
    "metadata": {"skill": "summarize_pr"}
  }
}
```

Result shape:

```json
{
  "result": {
    "kind": "task",
    "id": "<task-id>",
    "contextId": "<ctx-id>",
    "status": {"state": "completed"},
    "artifacts": [...],
    "data": {
      "usage": {"input_tokens": 1200, "output_tokens": 340, "total_tokens": 1540},
      "durationMs": 4230
    }
  }
}
```

The `kind: "task"` discriminator is required — `@a2a-js/sdk` routes by it.

#### File parts in the answer

When a tool delivers a file — it saved it to the media store and called
`graph.sdk.attach_file(ref)` (#4252) — the terminal answer artifact carries an A2A 1.0
file part after the text, one per file:

```json
{"url": "https://agent.example.com/media/3f2a….mp4?exp=1761300000&sig=…", "mediaType": "video/mp4", "filename": "overview.mp4"}
```

The `url` is the signed media link, made absolute against `A2A_PUBLIC_URL`, the same base the
card advertises. With `A2A_PUBLIC_URL` unset, the base is the bound loopback port (a
non-loopback bind logs a startup warning). The signature admits the GET without a bearer.
A client that reads only text parts sees the answer unchanged.

**Links handed to a client expire** (#4258). `exp` is a unix-seconds expiry inside the signed
bytes (the HMAC covers `<file>|exp=<exp>`), so it can't be extended without the agent's key.
The lifetime is `media.remote_link_ttl_s` (default `604800`, 7 days; at most `31536000`, a year;
an explicit `0` = never expire, while `null` or an invalid value means the default), or a tool's
own `attach_file(ref, ttl_s=…)`. A never-expiring link from a fleet member still needs the hub's
token. Past it the agent answers `403` with
`{"detail": "Forbidden: this media link has expired — ask the agent for a fresh link"}`. Links without `exp` (the
console's inline links, and any minted before #4258) stay valid permanently.

**Fleet members.** A member's public base is the hub's `{hub}/agents/<slug>` prefix, so its links
are `{hub}/agents/<slug>/media/<file>?exp=…&sig=…`. A remote peer fetches them **without the hub's
token**: the hub forwards exactly that shape to the member without its own auth, and the member
verifies its own signature (the hub never holds the member's key). The hub forwards it only for a
`GET`/`HEAD` whose path carries no percent-encoding at all, whose `<file>` is a plain store name,
and whose query is exactly `exp=<unix seconds>` and `sig=<64 hex>`. **Only expiring links are
exempt**: a permanent `?sig=` link (the console's inline links, in history, exports or logs), and
anything else (unsigned, extra parameters, an encoded path), gets the hub's normal auth (`401`).
A forged or expired link of the right shape reaches the member and is refused there (`401` /
`403`). An unknown, stopped or unreachable slug answers the same `401`, so the exempt path can't
be used to probe the fleet. The hub lends none of its own credentials to these requests, and
forwards none of the caller's to a remote member.

So that links in a member's answer **text** work through the hub too, a member's terminal A2A
answer has its signed media links made absolute and expiring (the same lifetime). A standalone
agent's answer text keeps its permanent relative links, which its console resolves itself.

`/v1/chat/completions` has no file part. There, each attached file arrives as an absolute link
in the content (appended when the text doesn't already carry it) and in an `attachments:
[{url, mime_type, name}]` field on the message. Every signed `/media/…` link in a `/v1` answer
is made absolute and carries the same expiry as an attached file's link.

### `message/stream` — SSE

Same as `message/send` but streams frames as the run progresses. One SSE event per frame. Every frame carries a `kind` discriminator:

| `kind` | When emitted |
|---|---|
| `task` | First frame — initial task state |
| `status-update` | State transitions, tool-start / tool-end progress, and the `input-required` pause (with `final: true`) |
| `artifact-update` | Streaming partial outputs |

Consumers must check `kind` before interpreting fields — without it, `@a2a-js/sdk`'s `for await` loop silently skips frames.

### `tasks/get`

```json
{"method": "tasks/get", "params": {"id": "<task-id>"}}
```

Returns the current state of a task. Use to poll when push notifications aren't wired.

Task records are **persisted** (instance-scoped `a2a-tasks.db`, 24h TTL,
write-through on create + every terminal transition), so `tasks/get` and
`tasks/resubscribe` answer with the final state + artifacts even after the
in-memory copy is evicted (1h) or the process restarts. The background *runner*
doesn't survive a restart, so any task still non-terminal at boot is marked
`failed` ("interrupted by server restart") rather than left hanging.

### `tasks/resubscribe`

```json
{"method": "tasks/resubscribe", "params": {"id": "<task-id>"}}
```

SSE stream of remaining frames for an in-flight task. Lets a consumer reconnect after a network blip without losing events.

### Human-in-the-loop (`input-required`)

The agent can pause mid-task to ask the operator a question — the spec's
`input-required` flow (ADR 0003). It's driven by the lead-agent **`ask_human`**
tool, which issues a LangGraph `interrupt()`; the graph checkpoints at that exact
point.

1. The task transitions to **`input-required`** — a `status-update` whose
   `status.message` carries the question, with `final: true`, closing the SSE
   cycle. The task is **not** terminal; it's parked (and persisted, so it
   survives a restart). Webhook consumers get an immediate (un-throttled) push.
2. The caller answers by sending a **`message/send`** (or `message/stream`)
   carrying the **same `taskId`** (and `contextId`) with the reply as a text
   part. protoAgent resumes the graph via `Command(resume=…)` from the
   checkpoint — continuing exactly where `ask_human` paused — and drives to a
   terminal state (or another `input-required`).

```json
{"method": "message/stream", "params": {
  "message": {"taskId": "<parked-task-id>", "contextId": "<ctx>",
              "parts": [{"kind": "text", "text": "approved"}]}}}
```

A message with no `taskId` (or one that isn't `input-required`) starts a fresh
task as usual. HITL is **not** advertised as a card extension — the form rides a
`application/vnd.protolabs.hitl-v1+json` DataPart on the `INPUT_REQUIRED` frame,
and a consumer discovers it by reading the part's mimeType. (Earlier revisions of
this page and ADR 0014 referred to a `hitl-mode-v1` card capability; no such
extension is declared or implemented.)

#### Approval prompts (shell commands)

`run_command` can also park a task on `input-required` when shell approval is
required. The status frame's `status.message.parts` includes this DataPart
(A2A 1.0 wire shape):

```json
{
  "data": {
    "kind": "approval",
    "title": "Approve shell command?",
    "detail": "git status\n\nruns via: /bin/sh -c",
    "project": "workspace"
  },
  "metadata": {"mimeType": "application/vnd.protolabs.hitl-v1+json"},
  "mediaType": "application/json"
}
```

`kind` identifies an approval; `title` is the prompt, `detail` contains the
command and its actual executable chain, and `project` identifies the target
project. A caller without a console can display or speak the prompt and answer
in-task: send `message/send` or `message/stream` with the **same `taskId` and
`contextId`**, as in the continuation example above.

The approval parser accepts these resume values:

- Boolean `true`.
- Strings `approve`, `approved`, `yes`, `y`, `true`, or `ok`, case-insensitive
  and with surrounding whitespace stripped (for example, a text part containing
  `approved`).
- A data object with a boolean `approved` field: `{"approved": true}` approves,
  and `{"approved": false}` declines. This boolean takes precedence over other fields.
- Otherwise, an object whose `decision` field (or, if it is empty/missing, its
  `answer` field) contains an accepted answer above, such as
  `{"decision": "approve"}` or `{"answer": "yes"}`.

Anything else is a decline, including `false`, `no`, `deny`, or an empty answer.
The command is not run; the tool tells the model not to retry and to wait for
further instructions or ask how to proceed. A reply without the parked task's
IDs does not answer that approval.

For a fully trusted caller, set **`message.metadata.bypass_permissions: true`**
on the request to skip `run_command` approval for that turn:

```json
{
  "jsonrpc": "2.0",
  "id": "trusted-delegation-1",
  "method": "message/send",
  "params": {
    "message": {
      "messageId": "caller-message-1",
      "role": "user",
      "parts": [{"text": "Run git status in the workspace project."}],
      "metadata": {"bypass_permissions": true}
    }
  }
}
```

This approves **every shell command for that turn**, not just the command named
in the message. It is honoured only when the host's `filesystem.bypass_allowed`
is `true` (the default); set it to `false` to prevent caller bypass. Each command
whose approval is bypassed is logged at WARNING. `delete_file` and onboarding
approvals **always ask**, regardless of this flag.

Prefer the host's [safe-command allowlist](/guides/sandboxing#run-auto-approve)
for routine reads. These filesystem approval policies are **host-wide, not
per-caller**; a per-caller policy is being designed separately in ADR 0124.
See [Delegated and voice callers](/guides/sandboxing#delegated-and-voice-callers)
for choosing among the three options.

### `tasks/cancel`

```json
{"method": "tasks/cancel", "params": {"id": "<task-id>"}}
```

Transitions the task to `canceled` if it's still running. No-op on terminal tasks.

### `agent/getAuthenticatedExtendedCard`

```json
{"method": "agent/getAuthenticatedExtendedCard", "params": {}}
```

Returns the agent card to an authenticated caller (the request has already
passed the bearer/api-key check). Same shape as the public
`/.well-known/agent-card.json`.

### `tasks/pushNotificationConfig/{set,get,list,delete}`

Register webhooks so a non-streaming consumer is kept updated as work
progresses. The agent POSTs to the webhook on **every meaningful transition the
caller cares about** — the initial `working`, each per-tool progress step, and
the terminal state — mirroring what SSE subscribers see. Non-terminal updates
are **throttled** to at most one POST per ~1.5s (carrying the latest state) so a
burst of tool events can't storm the webhook; terminal transitions flush
immediately. The COMPLETED webhook also carries the terminal `artifact`.

```json
{
  "method": "tasks/pushNotificationConfig/set",
  "params": {
    "taskId": "<task-id>",
    "pushNotificationConfig": {
      "url": "https://consumer/callback/abc",
      "token": "shared-secret"
    }
  }
}
```

The handler accepts both token shapes the A2A spec permits:

| Shape | JSON |
|---|---|
| Top-level `token` (what `@a2a-js/sdk` serializes by default) | `{"url": "...", "token": "..."}` |
| Structured `authentication.credentials` (RFC-8821) | `{"url": "...", "authentication": {"schemes": ["Bearer"], "credentials": "..."}}` |

Both produce `Authorization: Bearer <token>` **and** the spec-canonical
`X-A2A-Notification-Token: <token>` header on outgoing webhook POSTs, so a
strict receiver can validate the notification belongs to a config it created.
When both token shapes are present, top-level wins.

Webhook payload: a `TaskStatusUpdateEvent` (the same envelope as the matching
SSE `status-update` frame); the terminal/COMPLETED POST attaches the full
`artifact`. Delivery retries 3× with exponential backoff (1s/3s/9s), skipping
retry on 4xx.

Registered configs are **persisted** (write-through to an instance-scoped
`a2a-push.db`, 24h TTL) so they survive the task's terminal eviction and a
process restart.

## REST aliases

Thin REST wrappers are also exposed for non-JSON-RPC clients:

| Method + Path | Equivalent to |
|---|---|
| `POST /a2a/tasks/:taskId/pushNotificationConfig` | `tasks/pushNotificationConfig/set` |
| `GET /a2a/tasks/:taskId/pushNotificationConfig` | `tasks/pushNotificationConfig/list` |
| `GET /a2a/tasks/:taskId` | `tasks/get` |

Same semantics, same token-shape parsing, same SSRF guarding.

## SSRF guard

Outgoing webhook URLs are resolved once and checked against an allowlist before the handler accepts a push config. By default, private IP ranges (RFC1918 + loopback + link-local) are refused.

To permit trusted docker-network hostnames, set:

```bash
PUSH_NOTIFICATION_ALLOWED_HOSTS=automaker-server,internal-hooks
PUSH_NOTIFICATION_ALLOWED_CIDRS=10.0.0.0/8,172.16.0.0/12
```

Hosts in `PUSH_NOTIFICATION_ALLOWED_HOSTS` bypass the DNS check entirely.

## Extension advertisement

The card's `capabilities.extensions` array declares protocol extensions the agent implements. See [Extensions reference](/reference/extensions) for the ones the template ships.
