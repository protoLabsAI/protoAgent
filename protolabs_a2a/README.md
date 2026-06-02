# protolabs-a2a

A **thin** Python conventions layer on top of the official
[`a2a-sdk`](https://pypi.org/project/a2a-sdk/) (A2A spec 1.0). `a2a-sdk` owns
every piece of protocol mechanics — JSON-RPC dispatch, SSE streaming, the task
lifecycle, push-notification delivery. This package owns **only** what is
specific to the protoLabs fleet, so every agent (in-process Ava, roxy, ORBIS,
pwnDeck, protoAgent) interoperates byte-for-byte. It is the Python mirror of the
TypeScript reference layer `@protolabs/a2a`.

## What's in it

| Module | Responsibility |
| --- | --- |
| `parts` | Build / read the A2A 1.0 member-discriminated `Part` shape. |
| `extensions` | The four custom DataPart conventions: MIME + card-URI constants, payload types, `emit_*` / `parse_*`. |
| `agent_card` | `build_agent_card(...)` — provider, JSONRPC interface, extension declarations, auth schemes. |
| `auth` | The `apiKey` (`X-API-Key`) and `bearer` security-scheme builders. |

## The wire shape (A2A 1.0)

A custom DataPart is the member-discriminated union the JS reference emits:

```json
{
  "content": { "$case": "data", "value": <payload> },
  "metadata": { "mimeType": "<MIME>" },
  "filename": "",
  "mediaType": "application/json"
}
```

The discriminator is `metadata.mimeType`; the payload lives at `content.value`.
`parts.data_part(payload, mime)` produces exactly this; `parts.read_data(part)`
returns `(mime, payload)`. `read_*` also accepts the flattened proto3-JSON form
(`{"data": …}`) that `a2a-sdk`'s protobuf serializer emits, so a part produced
by either runtime parses.

> **Serializer note.** `a2a-sdk` 1.1 represents `Part` as a **protobuf** message
> and serializes it with proto3-JSON, which *flattens* the `content` oneof to a
> top-level `data` (or `text`) field — i.e. `{"data": …, "metadata": …,
> "mediaType": …}`, NOT the `content.$case`/`content.value` form above. The
> `content.$case` form is the TypeScript (ts-proto) in-memory/JSON encoding. The
> **payloads, MIME types, and extension URIs are identical** across both; only
> the part-envelope key differs. Build outbound parts with `parts.data_part` to
> get the protoLabs (`content.$case`) wire shape; parse inbound with
> `parts.read_data`, which is tolerant of both.

## The four extensions

| Extension | MIME (`metadata.mimeType`) | Card URI (`capabilities.extensions[].uri`) |
| --- | --- | --- |
| cost-v1 | `application/vnd.protolabs.cost-v1+json` | `https://proto-labs.ai/a2a/ext/cost-v1` |
| confidence-v1 | `application/vnd.protolabs.confidence-v1+json` | `https://proto-labs.ai/a2a/ext/confidence-v1` |
| worldstate-delta-v1 | `application/vnd.protolabs.worldstate-delta-v1+json` | `https://proto-labs.ai/a2a/ext/worldstate-delta-v1` |
| tool-call-v1 | `application/vnd.protolabs.tool-call-v1+json` | `https://proto-labs.ai/a2a/ext/tool-call-v1` |

Payload shapes:

- **cost-v1** — `{usage:{input_tokens, output_tokens, cache_creation_input_tokens?, cache_read_input_tokens?}, durationMs?, costUsd?, success?}`
- **confidence-v1** — `{confidence, explanation?, success?}`
- **worldstate-delta-v1** — `{deltas:[{domain, path, op:"set"|"inc"|"push", value}]}`
- **tool-call-v1** — `{toolCallId, name, phase:"started"|"completed"|"failed", args?, result?, error?}`

## Usage

```python
import protolabs_a2a as pa
from a2a.types import AgentSkill

# Emit extension DataParts (returns wire-shaped dicts):
cost = pa.emit_cost({"input_tokens": 1500, "output_tokens": 420}, duration_ms=900, cost_usd=0.01, success=True)
conf = pa.emit_confidence(0.92, explanation="high coverage")
delta = pa.emit_worldstate_delta([{"domain": "board", "path": "data.backlog", "op": "inc", "value": 1}])
tool = pa.emit_tool_call("call_1", "file_bug", "completed", result="BUG-12")

# Parse (returns the typed payload iff the MIME matches, else None):
payload = pa.parse_cost(cost)

# Build a 1.0 agent card with the fleet conventions:
card = pa.build_agent_card(
    name="protoagent",
    description="…",
    url="http://protoagent:7870/a2a",
    version="1.0.0",
    skills=[AgentSkill(id="chat", name="Chat", description="…", tags=["chat"])],
    bearer=True,          # advertise the bearer scheme
)
```

The agent owns *which* extensions it emits — pass `extension_uris=[…]` to
`build_agent_card` to declare a subset; it defaults to all four.
