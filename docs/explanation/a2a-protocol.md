# A2A protocol

A2A (Agent-to-Agent) is a JSON-RPC 2.0 protocol with SSE streaming for agent-to-agent communication. The full spec lives at [a2a-protocol.org](https://a2a-protocol.org/). This page covers what the template handles and where naive implementations tend to go wrong.

The template speaks **A2A 1.0** via [`a2a-sdk`](https://pypi.org/project/a2a-sdk/) ≥1.1 (ADR [0014](/adr/0014-a2a-1.0-migration)). v0.3 backward compatibility is mounted on the same endpoint, so older clients keep working — but everything below describes 1.0, which is what you should write against.

## The happy path

Consumer POSTs to `/a2a` with the version header:

```json
POST /a2a
A2A-Version: 1.0

{"jsonrpc": "2.0", "id": "1", "method": "SendStreamingMessage",
 "params": {"message": {"role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                        "messageId": "m-1"}}}
```

Server responds with an SSE stream. Each event is a bare `data:` line carrying a JSON-RPC response:

```
data: {"jsonrpc":"2.0","id":"1","result":{"task":{...}}}

data: {"jsonrpc":"2.0","id":"1","result":{"statusUpdate":{...}}}

data: {"jsonrpc":"2.0","id":"1","result":{"artifactUpdate":{...}}}

...
```

That's it. Everything else — skills, extensions, push notifications — layers onto this.

## Three things that changed in 1.0 (and will silently break a 0.3 client)

If you are porting a consumer from 0.3, these are the breakages, in the order you'll hit them.

**1. Method names are proto-style, not slash-style.** `SendMessage`, not `message/send`. The full 1.0 method table:

| 1.0 method | v0.3 alias |
|---|---|
| `SendMessage` | `message/send` |
| `SendStreamingMessage` | `message/stream` |
| `GetTask` | `tasks/get` |
| `ListTasks` | — |
| `CancelTask` | `tasks/cancel` |
| `SubscribeToTask` | `tasks/resubscribe` |
| `CreateTaskPushNotificationConfig` | `tasks/pushNotificationConfig/set` |
| `GetTaskPushNotificationConfig` | `tasks/pushNotificationConfig/get` |
| `ListTaskPushNotificationConfigs` | `tasks/pushNotificationConfig/list` |
| `DeleteTaskPushNotificationConfig` | `tasks/pushNotificationConfig/delete` |
| `GetExtendedAgentCard` | `agent/getAuthenticatedExtendedCard` |

**2. The `A2A-Version: 1.0` request header is load-bearing.** An `a2a-sdk` ≥1.0 receiver reads a *missing* header as "this client speaks 0.3", and a 1.0-only handler then rejects the call with **`-32009 VERSION_NOT_SUPPORTED`** ("A2A version '0.3' is not supported by this handler. Expected version '1.0'."). Outbound, the template stamps the header in `plugins/delegates/adapters.py::A2aAdapter._rpc`.

The gate's **position in the pipeline** is what makes this confusing to debug, so it's worth stating exactly. The dispatcher (`a2a/server/routes/jsonrpc_dispatcher.py`) resolves in this order:

| Condition | Response | Version header consulted? |
|---|---|---|
| Method name not in the 1.0 table (and no v0.3 alias) | `-32601 Method not found` | no |
| Params fail proto parsing | `-32602 Invalid params` | no — **checked before the version gate** |
| Method + params valid, header missing or mismatched | `-32009 VERSION_NOT_SUPPORTED` | yes |

The practical consequence: **you cannot detect a version problem with a malformed probe.** Send `GetTask` with junk params and you get `-32602` no matter what the header says, which reads as "the peer ignores the header". You only reach the gate with a genuinely valid request. `scripts/a2a_conformance.py` probes with a valid `GetTask{id}` for a nonexistent task precisely so it lands on `-32009` rather than short-circuiting.

**3. Parts are untyped, roles and states are enum-spelled.** 0.3's `{"kind": "text", "text": "…"}` becomes `{"text": "…"}`. `"role": "user"` becomes `"role": "ROLE_USER"`. Task states are `TASK_STATE_COMPLETED`, not `"completed"`. The template's `tools/a2a_parse.py::_is_terminal` accepts both spellings so a fork can talk to mixed-version peers.

One more sharp edge: **`contextId` lives inside the message, not on `params`.** Putting it at params level is a `-32602 Invalid params` (pinned by `test_params_level_context_id_is_rejected`).

## SSE frames are a oneof, not a `kind` discriminator

In 0.3, every SSE frame carried `"kind": "task" | "status-update" | "artifact-update"`. **1.0 removed that field.** The frame type is instead the *single key* of the `result` object:

```jsonc
{"result": {"task":           {...}}}  // first frame — full Task snapshot
{"result": {"statusUpdate":   {...}}}  // state transitions, tool progress, reasoning
{"result": {"artifactUpdate": {...}}}  // streaming answer text
{"result": {"message":        {...}}}  // direct message reply (no task)
```

Read the frame type with the equivalent of `next(iter(result))` — see `evals/client.py::stream` for a reference consumer. There are also no SSE `event:` names on success frames; only errors get `event: error`. A 0.3-era consumer that routes on `kind` or on the event name silently skips every frame and never attaches.

The assistant's answer streams incrementally: `artifactUpdate` frames with `append: true` carry each new suffix as the model generates it, and a final frame with `append` absent/false replaces the artifact with the authoritative full text. The model answers natively — its reasoning streams separately on `statusUpdate` frames, not in the answer text — and the terminal `extract_output` strips any provider-leaked reasoning before the final replace. Consumers that only want the final answer can ignore the deltas and read the last non-append frame.

> **proto3 presence trap.** `append` is a plain `bool` in the proto schema, so `append=False` and *unset* serialize identically — the field vanishes from the JSON either way. The template therefore treats "absent" as "replace", and `a2a_impl/executor.py::_finalize` sends the terminal frame with `append=False, last_chunk=True`. A consumer that blindly accumulates every `artifactUpdate` will double the answer text.

Two durable-store consequences follow (both landed with #1709/#1710). The terminal replace frame is **authoritative**: the persisted task's answer artifact always holds the canonical final text exactly once, even if mid-stream append frames were lost between the executor and the store — so a `GetTask` re-fetch never returns a truncated answer sealed under the cost/context parts. And reasoning frames are a **streaming affordance only**: on the wire they're batched to roughly the same character granularity as the answer deltas (the live thinking bubble still fills word by word), while the durable task history coalesces each contiguous reasoning run into a single Message (`a2a_impl/stores.py::coalesce_reasoning_history`) — one turn of thinking is one history row, not one row per token, so `GetTask(historyLength=N)` pages conversation, not word fragments.

## Camel-case vs snake-case

Wire fields are camelCase: `taskId`, `contextId`, `durationMs`. Python code is snake_case: `task_id`, `context_id`, `duration_ms`. The SDK's proto serialization is the translation boundary. Don't leak snake_case into wire responses.

## Push notification tokens — two shapes

The A2A spec permits two equivalent ways to carry the shared-secret token:

**Shape 1 — top-level `token`** (what `@a2a-js/sdk` serializes by default):

```json
{"url": "https://consumer/callback/abc", "token": "shared-secret"}
```

**Shape 2 — structured `authentication.credentials`** (RFC-8821 AuthenticationInfo):

```json
{
  "url": "https://consumer/callback/abc",
  "authentication": {"schemes": ["Bearer"], "credentials": "shared-secret"}
}
```

Both are active spec — neither is deprecated. Different consumers use different shapes. If your handler only reads one, half of real-world consumers will register a webhook, receive HTTP 401s on every delivery, and silently fall back to polling. Since the 1.0 migration this normalization lives in `a2a-sdk`'s `BasePushNotificationSender` rather than in template code; the template wraps it to add the SSRF check below (`a2a_impl/stores.py::ValidatingPushNotificationSender`).

## SSRF is a real risk

A webhook URL is an outbound HTTP call this agent makes with a shared secret attached. If a malicious (or careless) consumer registers:

- `http://169.254.169.254/...` — cloud metadata endpoint
- `http://10.0.0.1/...` — LAN router
- `http://localhost/...` — sibling services on the host
- `http://internal-db:5432/...` — adjacent services on the docker network

...the agent would happily POST task payloads (potentially with `Authorization: Bearer <secret>`) to any of them.

`is_safe_webhook_url` (`a2a_impl/stores.py:90`) resolves the URL's hostname once and rejects anything that lands in a private range. The check runs **twice** — at registration (`ValidatingPushNotificationConfigStore.set_info`) and again at delivery (`ValidatingPushNotificationSender._dispatch_notification`), so a hostname that re-resolves into a private range between the two is still caught. It's not a full DNS-rebinding defence, but it closes both the "just use an RFC1918 literal" vector and the trivial re-resolve. Operator allowlists (`PUSH_NOTIFICATION_ALLOWED_HOSTS`) bypass the check for trusted docker-network targets that would otherwise fail.

## Task lifecycle

```
SUBMITTED → WORKING → COMPLETED
         ↘         ↘ FAILED
   INPUT_REQUIRED  ↘ CANCELED
```

`INPUT_REQUIRED` is how the template parks a turn awaiting a human answer (HITL): the executor calls `updater.requires_input()` with a `hitl-v1` DataPart describing the form, and the task sits in that state until the consumer sends a follow-up message on the same `contextId`. It is *not* terminal — it is a resumable pause, and it is deliberately preserved across restarts (see below).

All terminal states fire push notifications (if configured). Terminal tasks are swept on a 24h TTL (`sweep_expired_tasks`); until then `GetTask` and `SubscribeToTask` both work.

`SubscribeToTask` (0.3: `tasks/resubscribe`) is the reconnect mechanism. If a streaming consumer's connection drops mid-run, they call it with the task ID and get the remaining frames. This is served by the SDK's active-task registry, which streams live producer events — so it reattaches to an *in-flight* task rather than replaying a buffer of past frames. Reconnect early; a task that has already gone terminal has no remaining frames to send, and you should `GetTask` instead.

**Restart survives.** Tasks are persisted to an instance-scoped SQLite `a2a-tasks.db` (push configs to `a2a-push.db`) via the SDK's `DatabaseTaskStore`. On boot, `reconcile_interrupted_tasks` flips tasks stranded in `SUBMITTED`/`WORKING` to failed — while deliberately **preserving** `INPUT_REQUIRED` and `AUTH_REQUIRED`, because those are waiting on a human, not on a dead process.

## Trace propagation — not in the spec

The template reads `params.metadata["a2a.trace"]` on incoming requests:

```json
{
  "metadata": {
    "a2a.trace": {
      "traceId": "abc123",
      "spanId": "def456"
    }
  }
}
```

This is a protoLabs convention, not part of the A2A spec. It's how the fleet ties multi-agent Langfuse traces together: the callee nests its trace under the caller's span, so a delegation chain across four agents reads as one tree. Outbound, `A2aAdapter` stamps it at both request and message level (peers differ in which one they read). Consumers that don't know about it just don't stamp the field — the agent's trace becomes a standalone root instead of a child. No breakage.

Auth ships **bearer-token** (`A2A_AUTH_TOKEN` / `auth.token`) — when set, the card advertises a `bearer` security scheme — alongside API-key. The `a2a-sdk` advertises schemes on the card but does not enforce them, so enforcement is the template's own default-deny middleware (`a2a_impl/auth.py`). See [A2A endpoints](/reference/a2a-endpoints) + [agent card](/reference/agent-card).

## Transport: JSON-RPC only, deliberately

A2A 1.0 defines three transports — JSON-RPC, REST (HTTP+JSON), and gRPC. **protoAgent mounts JSON-RPC only**, and the card advertises exactly that one interface. This is a decision, not an omission, so it's worth stating the reason.

`a2a-sdk` ships ready-made REST routes, and wiring them up is a single extra kwarg (`add_a2a_routes_to_fastapi(..., rest_routes=create_rest_routes(handler))`). The blocker is the last line of `create_rest_routes`:

```python
routes.append(Mount(path='/{tenant}', routes=base_route_objects))
```

That wildcard mount is how the SDK offers multi-tenant REST (`/{tenant}/tasks/{id}`). But Starlette compiles a `Mount` path as `"/{tenant}" + "/{path:path}"`, so it **full-matches any request with two or more path segments** and then 404s inside its own router rather than falling through. Anything registered after it is shadowed:

| Path | Result once the tenant mount is in the table |
|---|---|
| `/metrics` | 200 — single segment, survives |
| `/plugins/notes/view` | **404** |
| `/app/index.html` | **404** |
| `/v1/chat/completions` | **404** |

Route order decides who loses. Everything mounted *before* the A2A routes is safe, but protoAgent **hot-mounts plugin routes onto the live app at runtime** (plugin reload appends to `STATE.fastapi_app`), and those land after — so enabling REST as-shipped would break every plugin view the moment a plugin reloaded, with a 404 that looks like a routing bug rather than a transport feature.

A fork that wants REST has two workable options: pass a non-empty `path_prefix` (e.g. `/a2a/rest`), or filter the `Mount` out of the returned route list and mount only the concrete `Route` objects. Both need a matching second entry in the card's `supportedInterfaces[]` — `protolabs_a2a.build_agent_card` has no parameter for that today, so it means appending to `card.supported_interfaces` in `server/a2a.py` before the card is served. **Don't advertise a transport you haven't actually mounted**; the same "the card is a contract" rule that removed `confidence-v1` applies here.

Auth is not the obstacle — `A2AAuthMiddleware` is default-deny and none of the REST paths match `_PUBLIC_PREFIXES`, so they'd be gated correctly. Note they would *not* match `_requires_operator` (which keys on `/api/`), so a federation-tier token would reach them — the same tier that already reaches `/a2a` and `/v1`, but worth deciding explicitly rather than inheriting.

gRPC is likewise unmounted, and needs the proto stubs plus a separate server port.

## What the template doesn't do

- **`AUTH_REQUIRED` / `REJECTED` states**: parsed and preserved, but the executor never produces them.
- **Multi-tenancy**: every task sees the same auth context. If you need per-caller isolation, extend the auth middleware.
- **OAuth2**: bearer + API-key ship; A2A security schemes also allow OAuth2 — wire it up in the card's `securitySchemes` (`server/a2a.py`, via `protolabs_a2a`) + middleware if needed.

## Related

- [A2A endpoints reference](/reference/a2a-endpoints) — every method + path
- [Extensions reference](/reference/extensions) — protocol extensions shipped
- [Cost & trace](/explanation/cost-and-trace) — how cost-v1 and `a2a.trace` plug in
