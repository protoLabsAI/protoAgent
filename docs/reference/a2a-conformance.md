# A2A 1.0 conformance

What protoAgent implements of A2A 1.0, stated honestly — including what it doesn't.

Every row below is checkable rather than asserted. `scripts/a2a_conformance.py` probes a
running agent and reports what it actually does:

```bash
python scripts/a2a_conformance.py --url http://127.0.0.1:7870 --token "$A2A_AUTH_TOKEN"
```

The prober is **stdlib-only** — no `a2a-sdk`, no `httpx`, not even this repo on the path — so
you can copy the single file next to any A2A agent, in any language's project, and run it
against that. If you're evaluating protoAgent, don't take this page's word for it; run the
tool.

Exit codes: `0` all required checks passed · `1` a required check failed · `2` peer unreachable.

## Methods

| A2A 1.0 method | v0.3 alias | Status | Evidence |
|---|---|---|---|
| `SendMessage` | `message/send` | ✅ served | `tests/test_a2a_handler.py`, prober `methods` |
| `SendStreamingMessage` | `message/stream` | ✅ served | prober `stream` drives a real turn |
| `GetTask` | `tasks/get` | ✅ served | prober `lifecycle` calls it on a real task |
| `CancelTask` | `tasks/cancel` | ✅ served | real cancel — `executor.py` catches `CancelledError`, records a `canceled` outcome, re-raises |
| `SubscribeToTask` | `tasks/resubscribe` | ✅ served | prober `lifecycle`; SDK active-task registry |
| `ListTasks` | — | ✅ served | SDK `DefaultRequestHandler` |
| `CreateTaskPushNotificationConfig` | `tasks/pushNotificationConfig/set` | ✅ served | prober `push` (opt-in) |
| `GetTaskPushNotificationConfig` | `tasks/pushNotificationConfig/get` | ✅ served | `tests/test_a2a_push_store.py` |
| `ListTaskPushNotificationConfigs` | `tasks/pushNotificationConfig/list` | ✅ served | prober `push` |
| `DeleteTaskPushNotificationConfig` | `tasks/pushNotificationConfig/delete` | ✅ served | prober `push` |
| `GetExtendedAgentCard` | `agent/getAuthenticatedExtendedCard` | ✅ served | SDK default |

v0.3 aliases are mounted on the same `/a2a` endpoint (`enable_v0_3_compat=True`), so 0.3-era
clients keep working.

> **A served method is not a working method.** The prober's `methods` section proves only
> that a method is *routed* — it sends empty params and reads `-32601` vs. anything else.
> That's deliberately side-effect-free, but it's a weak claim. The `lifecycle` and `push`
> sections exist to make the strong one: they drive the real calls against a real task.

## Task states

| State | Produced? | Notes |
|---|---|---|
| `SUBMITTED` | ✅ | on enqueue, before `start_work()` |
| `WORKING` | ✅ | carries text deltas, reasoning, and `tool-call-v1` progress |
| `COMPLETED` | ✅ | terminal artifact is an authoritative full-text replace |
| `FAILED` | ✅ | ⚠️ emits **no** terminal artifact, so a failed turn carries no cost telemetry |
| `CANCELED` | ✅ | real cancel, not a flag flip |
| `INPUT_REQUIRED` | ✅ | HITL pause; resumable, and preserved across restart |
| `AUTH_REQUIRED` | ⚠️ parsed only | preserved by restart reconciliation; never produced |
| `REJECTED` | ❌ | not produced |

## Transports

| Transport | Status |
|---|---|
| JSON-RPC 2.0 over `/a2a` | ✅ mounted |
| REST (HTTP+JSON) | ❌ **deliberately not mounted** |
| gRPC | ❌ not mounted |

The REST decision has a concrete technical reason — the SDK's `create_rest_routes` appends a
`Mount('/{tenant}')` that shadows every 2+ segment path registered after it, which collides
with protoAgent's runtime plugin-route mounting. The full reasoning, the measured shadowing
behavior, and what a fork must do to enable it safely are in
[A2A protocol § Transport](/explanation/a2a-protocol). The card advertises exactly one
interface, matching what is actually mounted.

## Streaming

| Behavior | Status |
|---|---|
| SSE frames as the 1.0 `result` oneof (no `kind` discriminator) | ✅ |
| Incremental `artifactUpdate` with `append: true` | ✅ |
| Authoritative terminal replace (`append` absent/false, `last_chunk`) | ✅ |
| Reasoning on `statusUpdate` frames, coalesced in durable history | ✅ |
| Resubscribe to an in-flight task | ✅ live producer events (not a replay buffer) |

## Auth & security

| Behavior | Status |
|---|---|
| Bearer + API-key, advertised in `securitySchemes` | ✅ |
| **Enforcement** (the SDK advertises but does not enforce) | ✅ `a2a_impl/auth.py`, default-deny |
| Push-callback SSRF guard, checked at registration **and** delivery | ✅ prober `push` asserts refusal of a link-local metadata URL |
| OAuth2 | ❌ wire it up per fork |
| Per-caller multi-tenancy | ❌ every task shares one auth context |

The SSRF check is worth calling out because it inverts: a peer **refusing** the callback is
the passing result. A push callback is an outbound request the *agent* makes with a shared
secret attached, so an unguarded peer can be aimed at its own cloud-metadata endpoint.

## Extensions

The card declares exactly the extensions with a live emission path — `cost-v1`,
`worldstate-delta-v1`, `tool-call-v1` — carried as URI-keyed artifact/message `metadata`.
See [Extensions](/reference/extensions).

`confidence-v1` is in the shared fleet vocabulary but is **not** declared here, because
nothing emits it. The prober's `ext` section compares declared against actually-observed and
flags either direction, which is how that discrepancy was originally found.

## Known gaps

Stated plainly rather than left for you to discover:

- **A failed turn emits no cost telemetry.** `_terminal_parts` is only reached on success, so
  `cost-v1` never carries `success: false`. Cost accounting undercounts failures.
- **`AUTH_REQUIRED` / `REJECTED` are never produced.**
- **REST and gRPC transports are not mounted** (see above — deliberate).
- **`/api/chat` turns bypass the telemetry chokepoint.** Telemetry rows, Prometheus counters,
  and trajectory export all hang off the *A2A* terminal hook, so the desktop app's non-SSE
  fallback path produces a Langfuse trace but no telemetry row.

## Related

- [A2A protocol](/explanation/a2a-protocol) — the wire contract and the 0.3→1.0 traps
- [A2A endpoints](/reference/a2a-endpoints) — every method and path
- [Agent card](/reference/agent-card) — card shape and fields
- [Extensions](/reference/extensions) — the protoLabs extension conventions
