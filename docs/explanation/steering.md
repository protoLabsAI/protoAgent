# Mid-turn steering

A turn can take a while — tool loops, subagent delegations, long generations. **Mid-turn
steering** lets you send a message *while the agent is working* and have it folded in at
the next model call, so you can redirect without stopping (and losing) the stream.

## Why not just stop and re-ask

Stopping a turn discards in-flight work (an open delegation, a half-built answer) and
restarts cold. Steering instead **queues** your message and lets the running turn pick it
up on its next reasoning step — the agent course-corrects mid-flight, keeping everything
it's already done.

## How it works

- **A per-session queue** (`graph/steering.py`) holds messages submitted during a turn.
  `enqueue` adds, `drain` removes-and-returns all, `dequeue` cancels one before it's used.
- **`SteeringMiddleware`** (`graph/middleware/steering.py`) runs in the `before_model`
  hook: it drains the queue and, if anything's there, prepends it as a `HumanMessage` so
  the reducer appends it before the next model call. It wraps the text in an advisory —
  *"address it now if it changes the task, otherwise acknowledge briefly and keep going"* —
  so the agent treats it as guidance, not a hard interrupt. Empty queue → no-op, so normal
  turns are unaffected.

Because the middleware fires before *every* model call in the turn, a steer lands at the
next step regardless of how deep the tool loop is.

## What you see in the console

- Type into the composer while a turn streams; the message posts to
  `POST /api/chat/sessions/{id}/steer` and shows as a **pending bubble** with a ✕.
- **✕ cancels** it (`DELETE …/steer/{msg_id}`) if it hasn't been folded in yet. If it
  already shaped the reply, the console settles it into the thread instead of dropping it.
- At turn end the console reconciles against `GET …/steer`: anything that arrived after
  the turn's last model call (so it wasn't folded in) is **re-sent as a fresh turn**, so a
  late steer is never silently lost.

## Steering vs. cancelling a delegation

Two different controls:

- **Steering** redirects the *lead* turn by queuing a message (above).
- **Delegation cancel** (`POST …/delegations/{id}/cancel`) aborts a single running
  subagent `task` — the lead continues with a "cancelled" result. Contrast the composer's
  **Stop**, which A2A-`CancelTask`s the *whole* turn.

See also [ADR 0051](/adr/0051-a2a-realtime-streaming-and-component-rendering) (realtime
streaming + stop) and the [output protocol](/explanation/output-protocol).
