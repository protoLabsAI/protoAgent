# Two agents talking over A2A

Learning-oriented. In about ten minutes you'll run a second agent, verify it's a
conforming A2A 1.0 peer, have your first agent delegate real work to it over the
protocol, and then read the whole cross-agent exchange as one trace.

Every command here was run against a real pair of agents; the outputs are copied
from that session, not composed.

**You'll need:** a working protoAgent (the [first-agent tutorial](/tutorials/first-agent)
gets you there) with a model gateway configured. Langfuse is optional — the last
section is the only part that needs it.

## 1. Start a second agent

Instance scoping (ADR 0004) makes this a one-liner: a second agent with its own
config, chat history, and stores, leaving your existing one untouched.

```bash
PROTOAGENT_INSTANCE=demo PORT=7891 scripts/dev.sh --ui none
```

::: warning Pick a genuinely free port
If you already run a fleet, the obvious ports are taken — and a container
publishing `7881` will answer TCP without being the agent you meant, so you get a
confusing `404` instead of a connection error. Check first:

```bash
(echo >/dev/tcp/127.0.0.1/7891) 2>/dev/null && echo BUSY || echo free
```
:::

The new instance seeds its config from your default one. It needs a model key —
if you keep yours in `~/.protoagent/protoagent.env`, pass it through:

```bash
env $(grep -E '^(OPENAI_API_KEY|LANGFUSE_)' ~/.protoagent/protoagent.env | xargs) \
  PROTOAGENT_INSTANCE=demo PORT=7891 A2A_AUTH_TOKEN=demo-agent-token \
  scripts/dev.sh --ui none
```

Wait for `Application startup complete`, then:

```bash
curl -s http://127.0.0.1:7891/healthz     # 200
```

## 2. Read its agent card

The card is A2A's entry point — how a peer discovers what an agent can do and how
to reach it. It's served unauthenticated at a well-known path:

```bash
curl -s http://127.0.0.1:7891/.well-known/agent-card.json | jq
```

The fields that matter for interop:

- `supportedInterfaces[]` — the transport, URL, and **protocol version**. protoAgent
  advertises one JSONRPC interface at `/a2a`, protocol `1.0`.
- `skills[]` — what a peer can route to. Skill IDs must be stable; consumers key on them.
- `capabilities.extensions[]` — the [extensions](/reference/extensions) this agent emits.
- `securitySchemes` — `bearer` appears only when a token is actually configured.

## 3. Verify it's genuinely conformant

Don't take the card's word for it. Point the prober at it:

```bash
python scripts/a2a_conformance.py --url http://127.0.0.1:7891 --token demo-agent-token
```

```
card
  ✓ interface           JSONRPC v1.0 → http://127.0.0.1:7891/a2a
  ✓ extensions          3 declared
version
  ✓ without header      -32009 — header is load-bearing; its absence is read as 0.3
methods
  ✓ SendMessage         served  (0.3: message/send)
  … 11 methods
stream
  ✓ oneof (not `kind`)  1.0 oneof shape
  ✓ terminal replace    authoritative full-text replace sent
ext
  ✓ cost-v1             declared and emitted
lifecycle
  ✓ GetTask             retrievable after terminal (state=TASK_STATE_COMPLETED)
  ✓ SubscribeToTask     HTTP 200 + frames
```

Two of those lines are worth pausing on.

**`without header → -32009`.** The `A2A-Version: 1.0` header is load-bearing. Omit
it and an SDK ≥1.0 peer reads you as a 0.3 client and refuses. This is the single
most common A2A integration failure, and it's invisible — the request looks
perfectly well-formed. See [the version gate](/explanation/a2a-protocol).

**`cost-v1 declared and emitted`.** The prober compares what the card *claims*
against what actually appeared on the wire, in both directions. A declared-but-never-emitted
extension is a broken promise a consumer can't detect except by waiting forever.

Add `--push-url https://example.com/hook` to also exercise the push-notification
config lifecycle and assert the SSRF guard. It cleans up after itself.

## 4. Introduce the two agents

Delegation is configured, not coded. Register your *first* agent as a delegate of
the new one:

```bash
curl -s -X POST http://127.0.0.1:7891/api/delegates \
  -H 'Authorization: Bearer demo-agent-token' \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "ava",
        "type": "a2a",
        "url": "http://127.0.0.1:7870/a2a",
        "description": "the agent next door",
        "auth": {"scheme": "bearer", "token": "<first-agent-token>"},
        "poll_timeout_s": 120
      }'
```

::: danger The auth block is nested — a flat key fails silently
`auth` must be an object: `{"auth": {"scheme": "bearer", "token": "…"}}`. Passing a
flat `"auth_token": "…"` is accepted with `"ok": true` and then **drops the
secret** — the response says `"has_secret": false`, and the first delegation dies
with `HTTP 401: Unauthorized: invalid bearer token`. Always check `has_secret` in
the response.
:::

Confirm the peer is reachable and speaks a version you can:

```bash
curl -s -X POST http://127.0.0.1:7891/api/delegates/test \
  -H 'Authorization: Bearer demo-agent-token' \
  -H 'Content-Type: application/json' -d '{"name": "ava"}'
```

```json
{"ok": true, "latency_ms": 2, "protocol_version": "1.0",
 "supported_versions": ["1.0"], "detail": "agent-card OK (protoagent), A2A 1.0"}
```

That pre-flight fetches the peer's card and compares versions, so a mismatch fails
with a legible message instead of an opaque `-32009` mid-dispatch.

## 5. Make them talk

```python
import asyncio
from evals.client import AgentClient

async def main():
    c = AgentClient(base_url="http://127.0.0.1:7891", bearer="demo-agent-token")
    r = await c.ask(
        "Use the delegate_to tool to ask the delegate named 'ava': "
        "'In one short sentence, what is the A2A protocol for?' "
        "Then reply with just what ava said, prefixed by 'ava said: '.",
        timeout_s=240,
    )
    print(r.state, "|", r.text)

asyncio.run(main())
```

```
completed | ava said: The A2A protocol is designed to enable secure, asynchronous
communication and task delegation between autonomous agents.
```

Underneath, agent B sent `SendMessage` to agent A's `/a2a` with an
`A2A-Version: 1.0` header and a bearer token, polled `GetTask` until terminal,
and pulled the answer text out of the terminal artifact.

## 6. Read it as one trace

This is the part that makes a fleet debuggable instead of mysterious.

Every telemetry row records the Langfuse trace the turn ran under:

```bash
curl -s 'http://127.0.0.1:7891/api/telemetry/recent?limit=1' \
  -H 'Authorization: Bearer demo-agent-token' | jq '{
     template: .langfuse_trace_url_template,
     trace: .turns[0].trace_id, tools: .turns[0].tool_calls }'
```

```json
{
  "template": "http://localhost:3001/project/gateway/traces/{trace_id}",
  "trace": "4e8f23e36c034a24c8bb84c4b9a1361a",
  "tools": 2
}
```

Fill the template and open it — or just click the **Trace** column in the console's
Telemetry surface, which does exactly this. Here is the actual tree from the run
above, ten observations under one root:

```
a2a-stream                       [SPAN]        ← agent B's turn
├── protoagent-turn              [GENERATION]
├── protoagent-turn              [GENERATION]
├── a2a:ava                      [AGENT]       ← the outbound hop
│   └── a2a-stream               [SPAN]        ← agent A's ENTIRE turn, nested inside
│       ├── protoagent-turn      [GENERATION]
│       └── protoagent-turn      [GENERATION]
├── tool:delegate_to             [TOOL]
├── protoagent-turn              [GENERATION]
└── protoagent-turn              [GENERATION]
```

The line that matters is `a2a-stream` appearing *inside* `a2a:ava`. Agent A's whole
turn — its model calls, its cost, its duration — is a subtree of agent B's dispatch
span, not merely a sibling in the same trace. That's the `a2a.trace` convention: the
caller stamps its trace and span id into the request metadata, and the callee reads
it and parents its own trace there. It's a protoLabs convention rather than part of
the A2A spec — a peer that ignores it simply starts its own root trace, and nothing
breaks.

::: tip Why `tool:delegate_to` is a sibling, not the parent
You'd expect `a2a:ava` to sit *under* the tool span that caused it. It doesn't,
because tool observations are recorded on completion rather than wrapping the call,
so the dispatch span — opened while the tool is still running — attaches to the turn
root instead. Cosmetic: the latency and the nesting of the peer's work are both
correct. It's noted here so the tree doesn't look broken when you first read it.
:::

The practical payoff: with `a2a:ava` as its own span, you can finally separate
"the peer was slow" from "we were slow calling it" — the first question anyone
asks of a multi-agent system, and one you cannot answer from logs.

## Clean up

```bash
scripts/dev-reset.sh          # wipes only the sandbox instance
```

## Where to go next

- [A2A conformance](/reference/a2a-conformance) — what's implemented, and the known gaps
- [A2A protocol](/explanation/a2a-protocol) — the wire contract and the 0.3→1.0 traps
- [Delegates](/guides/delegates) — the other delegate types (`openai`, `acp`)
- [Fleet](/guides/fleet) — running many agents under one hub
- [Observability](/guides/observability) — tracing, metrics, and the trajectory export
