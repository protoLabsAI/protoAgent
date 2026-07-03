# A2A protocol

A2A (Agent-to-Agent) is a JSON-RPC 2.0 protocol with SSE streaming for agent-to-agent communication. The full spec lives at [a2a-protocol.org](https://a2a-protocol.org/). This page covers what the template handles and where naive implementations tend to go wrong.

## The happy path

Consumer POSTs to `/a2a`:

```json
{"jsonrpc": "2.0", "id": "1", "method": "message/stream", "params": {...}}
```

Server responds with an SSE stream. Each event is a JSON frame:

```
event: task
data: {"jsonrpc": "2.0", "id": "1", "result": {"kind": "task", ...}}

event: status-update
data: {"jsonrpc": "2.0", "id": "1", "result": {"kind": "status-update", ...}}

...
```

That's it. Everything else — skills, extensions, push notifications — layers onto this.

## The `kind` discriminator is not optional

Every SSE frame must carry one of:

- `"kind": "task"` (first frame — full Task object)
- `"kind": "status-update"` (state transitions, tool progress)
- `"kind": "artifact-update"` (streaming artifacts)

The assistant's answer streams incrementally: `artifact-update` frames with `append: true` carry each new suffix as the model generates it, and a final `append: false` frame replaces the artifact with the authoritative full text. The model answers natively — its reasoning streams separately as `reasoning` frames, not in the answer text — and the terminal `extract_output` strips any provider-leaked reasoning before the final replace. Consumers that only want the final answer can ignore the deltas and read the last `append: false` frame.

Two durable-store consequences follow from this (both landed with #1709/#1710). The terminal `append: false` frame is **authoritative**: the persisted task's answer artifact always holds the canonical final text exactly once, even if mid-stream append frames were lost between the executor and the store — so a `GetTask` re-fetch never returns a truncated answer sealed under the cost/context parts. And reasoning frames are a **streaming affordance only**: on the wire they're batched to roughly the same character granularity as the answer deltas (the live thinking bubble still fills word by word), while the durable task history coalesces each contiguous reasoning run into a single Message — one turn of thinking is one history row, not one row per token, so `GetTask(historyLength=N)` pages conversation, not word fragments.

`@a2a-js/sdk`'s `for await` loop routes frames by `kind`. Without the field, the loop silently skips every frame and consumers never attach. The template's regression test `test_message_stream_events_have_kind_discriminator` locks this in — inline dict construction is the path of least resistance and also the easiest way to forget this field.

## Camel-case vs snake-case

Wire fields are camelCase: `taskId`, `contextId`, `durationMs`. Python code is snake_case: `task_id`, `context_id`, `duration_ms`. The A2A handler is the translation boundary. Don't leak snake_case into wire responses.

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

Both are active spec — neither is deprecated. Different consumers use different shapes. If your handler only reads one, half of real-world consumers will register a webhook, receive HTTP 401s on every delivery, and silently fall back to polling. The template's `_extract_push_token` accepts both; when both are present, top-level wins.

## SSRF is a real risk

A webhook URL is an outbound HTTP call this agent makes with a shared secret attached. If a malicious (or careless) consumer registers:

- `http://169.254.169.254/...` — cloud metadata endpoint
- `http://10.0.0.1/...` — LAN router
- `http://localhost/...` — sibling services on the host
- `http://internal-db:5432/...` — adjacent services on the docker network

...the agent would happily POST task payloads (potentially with `Authorization: Bearer <secret>`) to any of them.

`is_safe_webhook_url` in `a2a_stores.py` resolves the URL's hostname once and rejects anything that lands in a private range. It's not a full DNS-rebinding defence, but it closes the "just use an RFC1918 literal" vector. Operator allowlists (`PUSH_NOTIFICATION_ALLOWED_HOSTS`) bypass the check for trusted docker-network targets that would otherwise fail.

## Task lifecycle

```
SUBMITTED → WORKING → COMPLETED
                    ↘ FAILED
                    ↘ CANCELED
```

All three terminal states fire push notifications (if configured). Terminal tasks stay in memory until a background sweeper eventually discards them — in the meantime, `tasks/get` + `tasks/resubscribe` both work.

`tasks/resubscribe` is the reconnect mechanism. If a streaming consumer's connection drops mid-run, they POST `tasks/resubscribe` with the task ID and get the remaining frames. The template keeps a buffer of emitted frames per task to serve resubscriptions reliably.

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

This is a protoLabs convention, not part of the A2A spec. It's how the fleet ties multi-agent Langfuse traces together. Consumers that don't know about it just don't stamp the field — the agent's trace becomes a standalone root instead of a child. No breakage.

Tasks **are** persisted now: the SQLite-backed `DatabaseTaskStore` (`a2a_stores.py`) writes to an instance-scoped `a2a-tasks.db` (24h TTL), and push configs to `a2a-push.db`; non-terminal tasks left over from a restart are marked failed. Auth ships **bearer-token** (`A2A_AUTH_TOKEN` / `auth.token`) — when set, the card advertises a `bearer` security scheme — alongside API-key. See [A2A endpoints](/reference/a2a-endpoints) + [agent card](/reference/agent-card).

## What the template doesn't do

- **Multi-tenancy**: every task sees the same auth context. If you need per-caller isolation, extend the auth middleware.
- **OAuth2**: bearer + API-key ship; A2A security schemes also allow OAuth2 — wire it up in the card's `securitySchemes` (`server/a2a.py`, via `protolabs_a2a`) + middleware if needed.

## Related

- [A2A endpoints reference](/reference/a2a-endpoints) — every method + path
- [Extensions reference](/reference/extensions) — protocol extensions shipped
- [Cost & trace](/explanation/cost-and-trace) — how cost-v1 and `a2a.trace` plug in
