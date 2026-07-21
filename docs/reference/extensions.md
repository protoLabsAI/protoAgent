# Extensions

A2A **protocol** extensions the template implements. Each is either emitted, parsed, or both.

> Not what you're after? For *extending the agent's capabilities* — `SKILL.md` skills, MCP servers, and plugins — see the [Skills](/guides/skills), [MCP](/guides/mcp), and [Plugins](/guides/plugins) guides and [ADR 0001](/adr/). This page is about the A2A wire protocol.

## Two wire mechanisms — and why

Extension data reaches a consumer one of two ways, and **which one an extension uses is a design decision, not an accident**:

| | **URI-keyed `metadata`** | **MIME-typed `DataPart`** |
|---|---|---|
| Where | the `metadata` map on an `Artifact` or a status `Message`, keyed by the extension URI | an entry in `parts[]` with `metadata.mimeType` set |
| Carries | **telemetry / annotations** about the turn | **renderable content** produced by the turn |
| Extensions | `cost-v1`, `worldstate-delta-v1`, `tool-call-v1` | `hitl-v1`, `reasoning-v1`, `component-v1`, `context-v1`, structured skill results |
| Declared on the card | yes — all four, by default | no (template-local, no shared contract) |
| Helper | `protolabs_a2a.<ext>_metadata(...)` | `protolabs_a2a.data_part(payload, mime)` |

Until protolabs-a2a **0.3.0** all four fleet extensions rode `parts[]` as MIME-typed `DataPart`s. They moved to `metadata` for one reason: **a generic A2A client renders `parts[]` as content.** A client that doesn't know `application/vnd.protolabs.cost-v1+json` still has to do *something* with an unrecognized part, and "something" is usually dumping it into the transcript — so every turn ended with a blob of token counts stapled to the answer. The A2A spec's own guidance ("extensions should place custom attributes in the `metadata` map present on core data structures", the same convention the official Timestamp extension follows) resolves it: **content stays in `parts[]`, metadata stays in `metadata`.** An ignorant client now correctly ignores telemetry; a knowing client routes `metadata` by URI.

The template-local extensions stayed `DataPart`s precisely because they *are* content — a form to fill in, a thinking stream, a table to render. Rendering them as an unknown blob is a degradation, not a bug.

The URI is both the card declaration and the metadata key, with the version encoded in it (`…/cost-v1`); bump the URI to version an extension. `protolabs_a2a.ALL_EXTENSION_URIS` is the canonical four-tuple, and `protolabs_a2a.build_agent_card` defaults `capabilities.extensions[]` to it — `server/a2a.py::_build_agent_card_proto` doesn't override it, so this agent declares exactly those four.

### Full wire example — terminal artifact

A `message/stream` frame closing the answer artifact. Three extension fragments merged by `pa.merge_extension_metadata(...)` into one `metadata` map; the answer itself is the only `part`:

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "result": {
    "artifactUpdate": {
      "taskId": "t-9f2c",
      "contextId": "ctx-4b81",
      "artifact": {
        "artifactId": "answer",
        "parts": [
          {"text": "PR #2085 is open — I filed it against main."}
        ],
        "metadata": {
          "https://proto-labs.ai/a2a/ext/cost-v1": {
            "usage": {
              "input_tokens": 1200,
              "output_tokens": 340,
              "cache_read_input_tokens": 800
            },
            "durationMs": 4210,
            "costUsd": 0.0134,
            "success": true
          },
          "https://proto-labs.ai/a2a/ext/worldstate-delta-v1": {
            "deltas": [
              {"domain": "repo", "path": "pr/2085", "op": "set", "value": "open"}
            ]
          }
        }
      },
      "lastChunk": true
    }
  }
}
```

Three wire details that bite consumers:

- **`append` is absent, not `false`.** proto3 gives the bool no presence, so a *replace* frame simply omits the key. Per the A2A spec absent/false ⇒ replace; only an explicit `true` is an append. The terminal frame is always a replace carrying the full canonical answer (`append=False, last_chunk=True`), so a naive concat consumer renders the answer twice.
- **`metadata` is a protobuf `Struct`**, whose numbers are doubles. Depending on the JSON serializer, `input_tokens` may arrive as `1200` or `1200.0` — coerce, don't assume int.
- **Key order is not stable** (map field). Route by URI, never by position.

### Full wire example — `tool-call-v1` on a status frame

Progress rides `TaskStatus.message.metadata`, not the artifact:

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "result": {
    "statusUpdate": {
      "taskId": "t-9f2c",
      "contextId": "ctx-4b81",
      "status": {
        "state": "TASK_STATE_WORKING",
        "message": {
          "messageId": "msg-7",
          "contextId": "ctx-4b81",
          "taskId": "t-9f2c",
          "role": "ROLE_AGENT",
          "metadata": {
            "https://proto-labs.ai/a2a/ext/tool-call-v1": {
              "toolCallId": "tc-1",
              "name": "web_search",
              "phase": "started",
              "args": {"query": "protoLabs A2A extensions"}
            }
          }
        }
      }
    }
  }
}
```

Note the message has **no `parts`** — the frame is pure telemetry, which is exactly the point of the move.

## `cost-v1`

**URI**: `https://proto-labs.ai/a2a/ext/cost-v1`
**Rides**: terminal `Artifact.metadata[<uri>]`
**Direction**: emitted by this agent
**Declared on card**: yes (by default)

```json
{
  "usage": {
    "input_tokens": 1200,
    "output_tokens": 340,
    "cache_read_input_tokens": 800,
    "cache_creation_input_tokens": 0
  },
  "durationMs": 4210,
  "costUsd": 0.0134,
  "success": true
}
```

| Field | What |
|---|---|
| `usage` | Token counts **summed across every LLM call in the turn** (snake_case, matching the provider's own field names). Cache fields are present when the provider reports them. |
| `durationMs` | Wall-clock for the whole turn. |
| `costUsd` | Derived from the model's rates and the captured usage. Omitted when zero/unknown — consumers should tolerate its absence and can recompute from `usage`. |
| `success` | Terminal state was a success. |

There is no `total_tokens`; derive it.

**Producer** — `server/chat.py::_run_turn_stream` yields a `("usage", {...})` frame per LLM call (line 793), carrying the cache fields, the per-call `cost_usd` from `observability/pricing.py::cost_usd`, and the *actual* model that served the call. `a2a_impl/executor.py` accumulates them (line 469) and `_terminal_parts` (line 649) builds the fragment via `pa.cost_metadata(...)`. Requires `stream_usage=True` on the client — `graph/llm.py` sets it.

**Consumers** call `pa.parse_cost(artifact["metadata"])` (or read the URI key directly) and record per-(agent, skill) samples. The consumer keys on the `skill` ID from the card, so **skill IDs must be stable**. The in-repo reference consumer is `evals/client.py::_extract`, which scans both the artifact's and the terminal status message's `metadata`.

## `confidence-v1` — not declared by this template

`https://proto-labs.ai/a2a/ext/confidence-v1` exists in the shared fleet
vocabulary (`protolabs_a2a` ships `confidence_metadata` / `parse_confidence`),
but **this agent does not declare it on its card and does not emit it**.

There *was* a producer once. The `<confidence>` self-report was part of the
`<output>` tag protocol, and when native reasoning replaced that protocol the
parser retirement deleted the tag along with everything else that no longer had
a live producer (see [Output protocol](/explanation/output-protocol)). The card
declaration was simply never cleaned up with it.

Leaving it declared is worse than not having the extension at all: a card is a
contract, so a consumer that saw the URI was entitled to build a calibration
pipeline on it and would wait forever for a payload that never came — a failure
it cannot detect except by timing out. `server/a2a.py::_emitted_extension_uris` now declares exactly the three
extensions with a live emission path, and
`tests/test_a2a_integration.py` asserts set **equality**, so re-declaring
without implementing fails CI.

A fork that wants self-reported confidence should, in this order: surface a
score from its output protocol, merge `pa.confidence_metadata(score,
explanation=..., success=...)` into the fragment list in `_terminal_parts`, then
add the URI to `_emitted_extension_uris`.

## `worldstate-delta-v1`

**URI**: `https://proto-labs.ai/a2a/ext/worldstate-delta-v1`
**Rides**: terminal `Artifact.metadata[<uri>]`
**Direction**: emitted when a tool reports a shared-state mutation
**Declared on card**: yes (by default)

```json
{
  "deltas": [
    {"domain": "repo", "path": "pr/2085", "op": "set", "value": "open"}
  ]
}
```

| Field | What |
|---|---|
| `domain` | World-state selector domain the mutation targets |
| `path` | Dotted/slashed path within the domain |
| `op` | `"set"`, `"inc"`, or `"push"` |
| `value` | The new value (or the increment, for `inc`) |

**Producer** — yield a `("delta", {domain, path, op, value})` event from your tool; the executor's event loop accumulates them (line 465) and `_terminal_parts` wraps the list with `pa.worldstate_delta_metadata(deltas)`. The template emits nothing by default because the shipped tools don't mutate shared state. Keep observed deltas consistent with whatever your card declares (see [fork-only declarative extensions](#fork-only-declarative-extensions) below) — divergence breaks a planner's scoring model.

## `tool-call-v1`

**URI**: `https://proto-labs.ai/a2a/ext/tool-call-v1`
**Rides**: `TaskStatus.message.metadata[<uri>]` on **`status-update` frames while `WORKING`** — not the terminal artifact
**Direction**: emitted by this agent
**Declared on card**: yes (by default)

This is how a live consumer (the React operator console) watches the agent work: each tool streams a `started` frame as it begins and a `completed`/`failed` frame as it finishes, so the UI can render running→done tool-call cards in real time.

```json
{
  "toolCallId": "tc-1",
  "name": "web_search",
  "phase": "started",
  "args": {"query": "protoLabs A2A extensions"}
}
```

| Field | What |
|---|---|
| `toolCallId` | The tool run id — pairs the `started` and terminal frames. Consumers merge/dedupe by it. |
| `name` | Tool name |
| `phase` | `"started"` \| `"completed"` \| `"failed"` |
| `args` | Truncated preview of the tool input (on `started`). Structured inputs are coerced to compact JSON so a client can pretty-print them; everything else is stringified. Capped at `_TOOL_PREVIEW_CHARS` (800). |
| `result` | Truncated preview of the tool result (on `completed`). Unwrapped from langchain's `ToolMessage` to its `.content` — the message repr would otherwise leak `name=`/`tool_call_id=` noise into the card. |
| `error` | Error text (on `failed`). |
| `parentToolCallId` | Present only when the tool ran *inside* a subagent delegation: the parent `task` call's id, so a client can nest the child card under it. Not part of the SDK's `ToolCallPayload` — the executor sets it as an extra key on the fragment (`a2a_impl/executor.py` line 639). |

**Producer** — `server/chat.py::_run_turn_stream` yields structured `("tool_start" | "tool_end", {id, name, input|output, parentId?})` tuples off langchain's `astream_events` (lines 676–745). Values are coerced by `_coerce_tool_value` / `_coerce_tool_output` and truncated. The executor's event loop turns each into a frame via `_tool_call_frame` (`a2a_impl/executor.py` line 606), which returns a `(part, metadata)` pair where **exactly one side is non-`None`**:

- a **dict** payload → `(None, {TOOL_CALL_EXT_URI: {...}})` — the URI-keyed fragment, emitted as a status message with *no* parts;
- a **plain-string** payload (legacy producers; the goal-mode `🎯 …` notes still take this path) → `(text_part, None)` — a plain text status part, so text-only consumers see progress.

**Coalescing caveat** — SSE frames get coalesced, so a tool that starts and ends within a single event-loop tick may surface only one frame. Real tools are slow enough (network, I/O) that `started` and `completed` land separately. Consumers must tolerate a missing `started` (render the terminal frame as a completed card) and dedupe by `(toolCallId, phase)`.

**Consumer** — the console's `streamChat` (`apps/web/src/lib/api.ts`) reads it off `statusUpdate.status.message.metadata` in the `status-update` branch (line 1602). `extByUri(metadata, uri)` (line 476) is the generic "read an extension payload by URI" helper; `toolEventFromMeta` (line 505) maps the wire payload onto the frontend `ToolEvent` — `toolCallId`→`id`, `args`→`input`, `result ?? error`→`output`, `phase: "started"`→`"start"` (anything else →`"end"`), `parentToolCallId`→`parentId`. That rename is load-bearing: casting the raw payload straight to `ToolEvent` leaves `id` undefined, and the merge-by-id then collapses every tool of a turn into one ever-overwriting card. `costFromMeta` (line 556) does the same for `cost-v1` off `artifactUpdate.artifact.metadata` (line 1620).

`ChatSurface` renders `<ToolCalls>` cards: collapsed by default (stable one-line row — icon, name, running→done, elapsed), expanding into structured renderers (`apps/web/src/chat/tool-renderers.tsx`) rather than a raw blob, with subagent tools nested under their parent `task` card by `parentId`.

## Template-local DataPart extensions

These are **not** on the agent card and have no SDK helper. They stay MIME-typed `DataPart`s in `parts[]` because they carry renderable content, built with `pa.data_part(payload, mime)` (+ `a2a_impl/executor.py::_ext_data_part` to convert the contract dict into a proto `Part`, line 160). Wire shape:

```json
{
  "data": {"contextTokens": 18432, "enabled": true, "trigger": "auto", "compactionAtTokens": 120000},
  "metadata": {"mimeType": "application/vnd.protolabs.context-v1+json"},
  "mediaType": "application/json"
}
```

| MIME | Rides | Payload | Producer | Consumer |
|---|---|---|---|---|
| `application/vnd.protolabs.hitl-v1+json` | the `input-required` frame | the form / approval spec (`{question}`, or a JSON-schema form / Approve-Deny card) | `a2a_impl/executor.py` line 490 (const at line 53), alongside a plain-text prompt for consumers that don't parse it | `hitlFromParts` (`api.ts` line 542) |
| `application/vnd.protolabs.reasoning-v1+json` | `WORKING` frames | `{"text": "…"}` — streamed `scratch_pad` "thinking", batched ~24 chars per frame | `a2a_impl/executor.py` line 314 (const at line 57) | `reasoningFromParts` (`api.ts` line 546) |
| `application/vnd.protolabs.component-v1+json` | `WORKING` frames | `{"component": "table"\|"keyvalue"\|"timeline", "props": {...}}` — ADR 0051 Slice 2 generative components | `a2a_impl/executor.py` line 451; MIME + types in `graph/components.py` line 17 | `componentFromParts` (`api.ts` line 533) |
| `application/vnd.protolabs.context-v1+json` | terminal artifact | `{contextTokens, enabled?, trigger?, compactionAtTokens?}` — context-window / compaction readout | `a2a_impl/executor.py` line 685 (const at line 646) | `contextFromParts` (`api.ts` line 587) |
| `application/vnd.protolabs.<schema>+json` | terminal artifact | a schema-validated structured skill result, when the caller sent a `skillHint` for a skill declaring an `output_schema` | `ProtoAgentExecutor._append_structured` (line 208), MIME from `pa.skill_result_mime(name)` | any consumer matching the declared MIME off the card's skill |

Reasoning deltas are a *streaming* affordance, but the a2a-sdk `TaskManager` moves every status frame's message into durable `history` — so `a2a_impl/stores.py::coalesce_reasoning_history` (line 240) collapses each contiguous run of reasoning messages into one before the task is saved. Without it a single turn persisted ~700 near-single-word `Message` rows and `tasks/get?historyLength=N` returned word fragments instead of conversation.

## Fork-only declarative extensions

`blast-v1` (`https://proto-labs.ai/a2a/ext/blast-v1`), `hitl-mode-v1` (`…/hitl-mode-v1`), and `effect-domain-v1` (`…/effect-domain-v1`) are **purely declarative** protoLabs extensions — per-skill statements on the card (`{uri, params: {skills: {...}}}`) that let a consumer gate or rank work before dispatching it: blast radius (`self` / `project` / `repo`), approval policy (`autonomous` / `notification`), and declared world-state effects (`{domain, path, delta, confidence}`) respectively.

**Status**: none of the three is declared by this template. `protolabs_a2a.build_agent_card` defaults `capabilities.extensions[]` to `ALL_EXTENSION_URIS` (the four above) and `server/a2a.py::_build_agent_card_proto` doesn't pass `extension_uris`, so the card carries exactly four extensions and no commented stanza remains in the code. They are documented here because forks with mutating skills do declare them and [ADR 0014](/adr/0014-a2a-1.0-migration) left them as fork territory. To add one, pass your own `extension_uris` (or post-process the card) and keep the declaration honest: an over-declared `effect-domain-v1` confuses a planner into routing your agent for goals it can't move, and a declared effect should be matched by a real `worldstate-delta-v1` fragment at runtime.

## `a2a.trace` — distributed Langfuse propagation

**Not an extension**, a protocol convention. Lives in `params.metadata`, not `capabilities.extensions`.

**Direction**: parsed by this agent (incoming)

When the caller stamps their trace context:

```json
{
  "method": "message/send",
  "params": {
    "message": {...},
    "metadata": {
      "a2a.trace": {
        "traceId": "abc123",
        "spanId": "def456"
      }
    }
  }
}
```

The executor reads it (`a2a_impl/executor.py::_extract_caller_trace`, line 568 — off the merged request metadata, message-level overlaid by request-level) and passes it into the turn; `server/chat.py` (line 1410) stamps `caller_trace_id` + `caller_span_id` into its own Langfuse trace metadata. Operators can then filter Langfuse by `metadata.caller_trace_id` to find every agent trace spawned from a single dispatch. The same metadata bag carries `skillHint`, per-tab `model` / `reasoning_effort`, and `incognito`.

## Adding a new extension

1. **Pick the mechanism.** Telemetry or an annotation *about* the turn → a URI-keyed `metadata` fragment. Content the user should see → a MIME-typed `DataPart`. When in doubt, ask what a client that has never heard of your extension should do with it: if the answer is "ignore it", it belongs in `metadata`.
2. Emit or parse it in `a2a_impl/executor.py` (`_terminal_parts` for terminal telemetry, `_tool_call_frame`/the event loop for streaming frames) or `server/chat.py` (to yield a new producer event).
3. For a metadata extension, declare the URI on the card under `capabilities.extensions` — pass `extension_uris` to `pa.build_agent_card` in `server/a2a.py::_build_agent_card_proto`. Version by URI suffix.
4. Document the shape in this file.
5. Add a test to `tests/test_a2a_integration.py` asserting the declaration is on the card (see `test_agent_card_declares_all_four_protolabs_extensions`), and one to `tests/test_a2a_handler.py` asserting the payload lands where you say it does.

## Related

- [Agent card reference](/reference/agent-card) — where extensions are declared
- [A2A endpoints](/reference/a2a-endpoints) — how artifacts reach consumers
- [Explanation: cost and trace](/explanation/cost-and-trace) — why these extensions are shaped this way
