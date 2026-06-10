# Middleware

Middleware is the per-turn hook layer of the agent — the same `AgentMiddleware`
paradigm LangChain/LangGraph defines, used directly (we subclass their classes; the
hook names below are theirs). Everything the core does *around* a model call —
knowledge injection, prompt caching, tool enforcement, audit, compaction, session
memory — is a middleware, and **a plugin's middleware is a first-class member of the
same chain** (ADR 0032). If you want to observe or shape every turn — inject context,
intercept tool calls, ship summaries somewhere — this is the contract.

This page is the deep dive; the quick version lives in
[Plugins → Middleware](/guides/plugins#middleware-register-middleware-adr-0032).

## The hooks

A middleware subclasses `langchain.agents.middleware.AgentMiddleware` and overrides
any of (each has an `a`-prefixed async variant — `abefore_model`, `awrap_tool_call`, …):

| Hook | Fires | Typical use |
|---|---|---|
| `before_agent` / `after_agent` | once per agent run (turn) | setup; **terminal-turn side effects** — this is where session summaries persist |
| `before_model` / `after_model` | around every model call | inject context (return `{"messages": [...]}`), inspect responses |
| `wrap_model_call` | around the model invocation | caching, retries, fallbacks |
| `wrap_tool_call` | around every tool execution | enforcement, audit, output capture |

Return `None` from a `before_`/`after_` hook to change nothing; return a state-update
dict to merge into the run state. `wrap_` hooks receive `(request, handler)` and must
call `handler(request)` (or not — that's how enforcement blocks a tool).

Two protoAgent-specific facilities round out the contract:

- **Per-request metadata** — `current_request_metadata()`
  (`graph/middleware/request_context.py`) is a contextvar carrying the A2A request's
  merged metadata (project scope, origin, caller keys) for the duration of the turn.
  Read it from any hook; never thread it through state yourself.
- **The event bus** — `registry.emit(...)` works from any hook, including sync hooks
  that LangGraph runs in a worker thread (the bus reroutes off-loop publishes onto
  the event loop). See the worked example below.

## The chain and where you land

Core middlewares run in a fixed order (`graph/agent.py::_build_middleware`):
prompt-cache → enforcement → knowledge → tool-deferral → audit → memory →
knowledge-ingest → compaction → model-fallback → **your plugin middleware** →
message-capture. Plugin middleware is appended after the core chain and before the
internal message-capture, so all your hooks run and the turn is still captured.

Two consequences worth designing around:

- **You cannot order relative to core middlewares or other plugins.** If you need to
  see messages *before* knowledge injection, that's not expressible today — say so in
  an issue rather than working around it.
- **You run on the hot path.** Every millisecond in `before_model` is a millisecond
  of first-token latency. Do heavy work async (`abefore_model`), defer it to
  `after_agent`, or ship it off via the bus and do it in a subscriber.

## Registering

In your plugin's `register(registry)`:

```python
def register(registry):
    registry.register_middleware(lambda config: MyMiddleware())
```

The factory is `(config) -> AgentMiddleware | None` — it receives the live
`LangGraphConfig` (read your settings from `config.plugin_config`) and may return
`None` to opt out. Resolution is **degrade-safe**: a factory that raises or returns
a non-middleware is logged and skipped; it can never take the agent down.

## Worked example — summarize a turn and ship it anywhere

The pattern: a middleware **observes** at `after_agent`, **publishes** a namespaced
event, and forgets. Any consumer — another plugin, a console page, a webhook bridge —
subscribes by topic. The producer never knows who's listening (the bus's
no-cross-dependency rule, ADR 0039).

`plugins/turn_digest/protoagent.plugin.yaml`:

```yaml
id: turn_digest
name: Turn digest
description: Publishes a digest of every completed turn on the event bus.
emits: [turn_digest.completed]
```

`plugins/turn_digest/__init__.py`:

```python
from langchain.agents.middleware import AgentMiddleware


class TurnDigestMiddleware(AgentMiddleware):
    """Publish a small digest of each completed turn (ADR 0032 + 0039)."""

    def __init__(self, emit):
        super().__init__()
        self._emit = emit

    def after_agent(self, state, runtime):
        msgs = state.get("messages") or []
        last = msgs[-1] if msgs else None
        text = getattr(last, "content", "") or ""
        self._emit("completed", {
            "messages": len(msgs),
            "preview": text if isinstance(text, str) else str(text),
        })
        return None  # observe-only: no state change


def register(registry):
    # Capture registry.emit in the factory — the middleware publishes under
    # this plugin's namespace ("turn_digest.completed") and can't spoof others'.
    registry.register_middleware(
        lambda config: TurnDigestMiddleware(registry.emit))
```

And a consumer — any *other* plugin (or the same one) forwards digests to an
external service without ever importing the producer:

```python
def register(registry):
    async def ship(payload):  # {"event", "data", "seq"}
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post("https://example.com/hooks/agent-digest",
                              json=payload["data"])

    registry.on("turn_digest.completed", ship)
```

Notes that keep this robust:

- `emit` is **fire-and-forget and thread-safe** — safe from sync hooks even when
  LangGraph runs them in a worker thread. A failed subscriber is isolated and logged;
  it can never break your middleware or the turn.
- The bus is **ephemeral** (a small replay ring for SSE reconnects, no durable log).
  It's the integration spine, not a system of record — if the digest must survive a
  crash, write it to a durable store (the knowledge base, your own table) and *also*
  emit.
- Declare your topics in the manifest (`emits:` / `subscribes:`) — they're your
  public API, discoverable in `/api/runtime/status`.

## Testing your middleware

Factories make this easy — build the instance directly and drive the hooks:

```python
def test_digest_emits():
    sent = []
    mw = TurnDigestMiddleware(lambda topic, data: sent.append((topic, data)))
    mw.after_agent({"messages": [AIMessage(content="hi")]}, runtime=None)
    assert sent and sent[0][0] == "completed"
```

For the full path (plugin load → chain membership), see
`tests/test_plugin_middleware.py` for the established patterns.

## Design rules

- **Observe in middleware, distribute on the bus, persist in stores.** Middleware is
  where lifecycle facts are seen; the bus is how they travel; durable truth lives in
  the checkpointer / knowledge base / audit log.
- **Never raise for effect.** A hook that raises is a bug, not a control flow —
  enforcement-style blocking goes through `wrap_tool_call` returning a result.
- **Opt out via the factory**, not by no-op hooks: return `None` from the factory
  when your config says disabled, and the chain stays clean.

## Related

- [Plugins](/guides/plugins) — the full contribution surface (`register_*` inventory)
- [Building a plugin view](/guides/building-react-plugin-views) — surface your middleware's output in the console
- ADR 0032 (plugin middleware) · ADR 0039 (event bus)
