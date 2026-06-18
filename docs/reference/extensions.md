# Extensions

A2A **protocol** extensions the template implements — typed `DataPart`s on the wire. Each is either emitted, parsed, or both.

> Not what you're after? For *extending the agent's capabilities* — `SKILL.md` skills, MCP servers, and plugins — see the [Skills](/guides/skills), [MCP](/guides/mcp), and [Plugins](/guides/plugins) guides and [ADR 0001](/adr/). This page is about the A2A wire protocol.

## `cost-v1`

**URI**: `https://proto-labs.ai/a2a/ext/cost-v1`
**Direction**: emitted by this agent
**Declared on card**: yes (by default)

Every terminal task carries a DataPart with token usage and duration:

```json
{
  "data": {
    "usage": {
      "input_tokens": 1200,
      "output_tokens": 340,
      "total_tokens": 1540
    },
    "durationMs": 4230
  }
}
```

Captured by the `on_chat_model_end` handler in `_chat_langgraph_stream`. Requires `stream_usage=True` on the ChatOpenAI client — the template sets this in `graph/llm.py`.

**Consumers** (like Workstacean's A2AExecutor) extract this DataPart onto `result.data` and record per-(agent, skill) samples. The consumer keys on the `skill` ID from the card, so skill IDs must be stable.

`costUsd` **is** emitted: `pricing.py::cost_usd` derives it from the model's rates and the captured `usage`, and it rides this DataPart (and the telemetry store). Consumers still tolerate a missing/zero `costUsd` (unknown model → no rate) and can recompute from `usage` themselves.

## `confidence-v1`

**URI**: `https://proto-labs.ai/a2a/ext/confidence-v1`
**mimeType**: `application/vnd.protolabs.confidence-v1+json`
**Direction**: emitted by this agent
**Declared on card**: yes (by default)

When the model self-reports a `<confidence>` tag in its final output, the terminal task carries a DataPart with the score:

```json
{
  "data": {
    "confidence": 0.85,
    "success": true,
    "confidenceExplanation": "two consistent sources agreed"
  }
}
```

The model emits the tags after `</output>` (see the protocol in `graph/output_format.py::OUTPUT_FORMAT_INSTRUCTIONS`); the server parses them with `extract_confidence()` and the A2A handler records them via `set_confidence()`, clamping to `[0, 1]`. The DataPart is omitted entirely when the model didn't report a score. `success` reflects the terminal state (`COMPLETED` only) — a high `confidence` on a non-success run is the "high-confidence failure" calibration signal.

## `blast-v1`

**URI**: `https://proto-labs.ai/a2a/ext/blast-v1`
**Direction**: declared by this agent
**Declared on card**: no (commented template stanza — fill per fork)

Declares the **scope of effect** of each skill so a consumer can gate higher-impact work. `radius` is `self` (affects only this agent), `project`, or `repo`.

```json
{
  "uri": "https://proto-labs.ai/a2a/ext/blast-v1",
  "params": {"skills": {"my_skill": {"radius": "self"}}}
}
```

Purely declarative — keep the declared radius honest with what each skill handler actually does. Uncomment the stanza in `server/a2a.py::_build_agent_card_proto` and use your real skill IDs.

## `hitl-mode-v1`

**URI**: `https://proto-labs.ai/a2a/ext/hitl-mode-v1`
**Direction**: declared by this agent
**Declared on card**: no (commented template stanza — fill per fork)

Declares a human-in-the-loop **approval policy** per skill: `autonomous` (run without approval) or `notification` (surface the action). Composes with `blast-v1` so higher-blast skills can be gated independently of goal-level config.

```json
{
  "uri": "https://proto-labs.ai/a2a/ext/hitl-mode-v1",
  "params": {"skills": {"my_skill": {"mode": "autonomous"}}}
}
```

## `effect-domain-v1`

**URI**: `https://proto-labs.ai/a2a/ext/effect-domain-v1`
**Direction**: declared by this agent
**Declared on card**: no (template has no mutating skills)

Advertises per-skill world-state mutations so Workstacean's L1 planner can rank your agent against goals that target those state selectors.

```json
{
  "uri": "https://proto-labs.ai/a2a/ext/effect-domain-v1",
  "params": {
    "skills": {
      "file_bug": {
        "effects": [{
          "domain": "protomaker_board",
          "path": "data.backlog_count",
          "delta": 1,
          "confidence": 0.9
        }]
      }
    }
  }
}
```

Fields:

| Field | What |
|---|---|
| `domain` | World-state selector domain the mutation targets |
| `path` | Dotted path within the domain |
| `delta` | Signed numeric delta (positive = increase) |
| `confidence` | 0–1 prior for the planner's ranking model |

Only declare effects that actually mutate shared state. Over-declaring confuses the planner into routing your agent for goals it can't move.

**Pair with runtime emission**: if you declare an effect, emit a matching `worldstate-delta-v1` DataPart when the tool succeeds at runtime — yield a `delta` event from the tool and the executor (`a2a_executor.py`) accumulates them onto the terminal artifact. Divergence between declared and observed mutations breaks the planner's scoring model.

See `docs/extensions/effect-domain-v1` in the [protoWorkstacean repo](https://github.com/protoLabsAI/protoWorkstacean) for the full spec.

## `worldstate-delta-v1`

**URI**: `https://proto-labs.ai/a2a/ext/worldstate-delta-v1`
**Direction**: emitted when tools with declared effects succeed
**Declared on card**: yes (declared in `capabilities.extensions` by default)

Emitted as a DataPart on the terminal artifact:

```json
{
  "mime": "application/vnd.protolabs.worldstate-delta-v1+json",
  "data": {
    "deltas": [{
      "domain": "protomaker_board",
      "path": "data.backlog_count",
      "op": "inc",
      "value": 1
    }]
  }
}
```

The template doesn't emit this by default because the shipped tools don't mutate anything. To hook in, yield a `("delta", {domain, path, op, value})` event from your tool; `a2a_executor.py` collects them into the artifact.

## `tool-call-v1`

**mimeType**: `application/vnd.protolabs.tool-call-v1+json`
**Direction**: emitted by this agent
**Declared on card**: no (progressive status DataPart, not a card capability)

Unlike the other DataParts here — which ride the **terminal artifact** — this one rides **`status-update` frames** while the task is still `WORKING`. It's how a live consumer (the React operator console) watches the agent work: each tool the agent invokes streams a `start` frame as it begins and an `end` frame as it finishes, so the UI can render running→done tool-call cards in real time.

```json
{
  "kind": "data",
  "metadata": {"mimeType": "application/vnd.protolabs.tool-call-v1+json"},
  "data": {
    "id": "run-abc123",
    "name": "web_search",
    "phase": "start",
    "input": "latest protoLabs news"
  }
}
```

Fields:

| Field | What |
|---|---|
| `id` | The tool run id (langchain `run_id`) — pairs the `start` and `end` frames. Consumers dedupe/merge by it. |
| `name` | Tool name |
| `phase` | `"start"` or `"end"` |
| `input` | Truncated preview of the tool input (on `start`). Structured inputs (dict/list) are rendered as **compact JSON** so the console can pretty-print them; everything else is stringified. |
| `output` | Truncated preview of the tool result (on `end`). Unwrapped from langchain's `ToolMessage` to its `.content` — the message repr would otherwise leak `name=`/`tool_call_id=` noise into the card. |

**Producer** — `server/chat.py::_run_turn_stream` yields structured `("tool_start"|"tool_end", {...})` tuples from langchain's `astream_events`. Inputs/outputs are coerced via `_coerce_tool_value` / `_coerce_tool_output` (JSON for structured values, `.content` for `ToolMessage`s) and truncated to `_TOOL_PREVIEW_CHARS`. The runner stores the latest on `TaskRecord.last_tool_event` and `_build_status_event` attaches it alongside the existing text status part (`🔧 name: input` / `✅ name → output`), so text-only consumers still see progress — the DataPart is purely additive and backward-compatible.

**Coalescing caveat** — the SSE watcher (`_watch_task`) coalesces bursts of updates, so a tool that starts and ends within a single event-loop tick may only surface one frame. Real tools are slow enough (network, I/O) that `start` and `end` land on separate frames. Consumers must tolerate a missing `start` (render the `end` as a completed card) and dedupe by `(id, phase)`.

**Consumer** — the console's `streamChat` (`apps/web/src/lib/api.ts`) extracts the DataPart in the `status-update` branch and merges it into the streaming assistant message's `toolCalls` by `id`; `ChatSurface` renders the `<ToolCalls>` cards. Cards default **collapsed** (a stable one-line row: icon, name, running→done status) so the message doesn't reflow as tools start and finish — expanding is an explicit, sticky choice. Expanded values render as **structured components** (`apps/web/src/chat/tool-renderers.tsx`), not a raw blob: object inputs become key/value field rows, URLs become links, scalars become chips, and the starter tools' outputs get purpose-built renderers (calculator → `expr = result`, web_search → result cards, fetch_url → status badge + link + body, `current_time` → timestamp; any `Error:` output → an error block). Unknown shapes fall back to a wrapped text block. Cards also show a per-tool icon and the elapsed start→end duration, and each value has a copy button. Tools that run inside a `task` subagent (i.e. that start while a `task` tool is still running) are **nested** under the parent task card. The `last_tool_event` is cleared on terminal transitions so a completed task shows a clean final state.

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

The agent reads it in `server/chat.py` and stamps `caller_trace_id` + `caller_span_id` into its own Langfuse trace metadata. Operators can then filter Langfuse by `metadata.caller_trace_id` to find every agent trace spawned from a single dispatch.

## Adding a new extension

1. Emit or parse in `a2a_executor.py` / `server/chat.py`.
2. Declare on the card under `capabilities.extensions` with a URI consumers agree on.
3. Document the shape in this file.
4. Add a test to `tests/test_a2a_integration.py` asserting the declaration is present on the card.

## Related

- [Agent card reference](/reference/agent-card) — where extensions are declared
- [A2A endpoints](/reference/a2a-endpoints) — how artifacts reach consumers
- [Explanation: cost and trace](/explanation/cost-and-trace) — why these extensions are shaped this way
