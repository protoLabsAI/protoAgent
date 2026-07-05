# System lifecycle events

The agent broadcasts a few **system lifecycle** events on the [event bus](/guides/plugins#the-event-bus-adr-0039) —
that it finished booting, that it just woke up from an idle stretch, that the desktop shell came
back. An operator can react to them from a config file; a plugin can react in code. Both are
**opt-in** and both are **error-isolated** — a broken webhook or a bad hook can never break boot or
a turn (ADR 0074).

## The events

| Event (bus topic) | Fires when | Payload |
| --- | --- | --- |
| `app.loaded` | Boot finished — the graph is compiled and the scheduler, surfaces, and fleet-autostart members are up. | `ts`, `agent`, `port`, `previous_state: "boot"` |
| `agent.active` | The agent went **idle → active**: a turn started after a quiet gap (the first turn since boot, or the first after ~5 min idle — **debounced**, so a busy session doesn't fire it every turn). | `ts`, `session_id`, `idle_seconds`, `previous_state: "boot" \| "idle"` |
| `system.wake` | *Reserved.* The desktop shell woke / regained focus. The bus, hook seam, and config all accept it today; the desktop emit is a follow-up. | `ts`, `previous_state` |

Every payload carries a `ts` (epoch seconds) and a `previous_state`, so a reaction knows *when* the
transition happened and *what it came from*.

## React from config (no code)

Add a top-level `lifecycle_hooks:` list to `langgraph-config.yaml`. Each entry names an `event`
and one or both of a `prompt` (enqueue a follow-up agent turn) and a `webhook` (POST the event).
**Empty by default ⇒ nothing fires** beyond the bus broadcast — this is opt-in.

```yaml
lifecycle_hooks:
  # When boot finishes, run a turn in the "ops" session catching me up.
  - event: app_loaded
    prompt: "You just started up. Check the inbox and scheduler and brief me on anything waiting."
    session: ops                       # app_loaded carries no session, so name one

  # Ping an external endpoint on every boot.
  - event: app_loaded
    webhook: https://hooks.example.com/protoagent/boot

  # When the agent comes back after being idle, review what happened.
  - event: agent_active
    prompt: "Welcome back — summarize anything that changed while you were idle."
    # no `session:` → runs in the session whose turn woke the agent
```

Fields per entry:

- **`event`** — `app_loaded` · `agent_active` · `system_wake` (the config uses underscores; the bus
  topic is the dotted form, e.g. `app.loaded`).
- **`prompt`** — the message the agent processes as a one-shot turn ([`run_in_session`](/guides/scheduler)).
  Runs in **`session`**, or — for `agent_active` — the event's own session when `session` is omitted.
  `app_loaded` / `system_wake` carry no session, so those **must** set `session`.
- **`webhook`** — a URL that receives `POST {"event": "<topic>", "data": <payload>}` (async, short
  timeout).

## React from a plugin (in code)

A plugin has two ways to react. The zero-config way is a **bus subscription** — subscribe to the
dotted topic in `register()`:

```python
def register(registry):
    registry.on("app.loaded", lambda ev: print("booted:", ev["data"]))
    registry.on("agent.active", on_active)   # ev["data"] has session_id, idle_seconds, …
```

The direct-callback way is a **lifecycle hook** (mirrors [`register_goal_hook`](/guides/goal-mode#reacting-to-a-goal)):

```python
def register(registry):
    registry.register_lifecycle_hook(
        on_app_loaded=lambda payload: ...,      # sync or async; payload is the event dict
        on_agent_active=on_active,
        # on_system_wake=...,                    # reserved
    )
```

Provide any of the three callbacks. A raising hook is logged and swallowed. Hooks are re-installed
on a config reload, so enabling/updating a plugin picks them up without a restart.

## Inspect what's wired — `/lifecycle`

Run `/lifecycle` in chat for a read-only listing of the three events, the currently-configured
config reactions, and any registered plugin hooks. (Listing only — the config file is the source of
truth; there's no runtime mutation.)

## See also

- [Plugins ▸ the event bus](/guides/plugins#the-event-bus-adr-0039) — the underlying pub/sub.
- [Schedule future work](/guides/scheduler) — `run_in_session`, the prompt-reaction primitive.
- ADR 0074 (system lifecycle events) · ADR 0039 (the plugin event bus).
