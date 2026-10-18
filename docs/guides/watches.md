# Watches

Use a watch to check an external condition, such as a deployment or CI run, and
react when it changes or succeeds. Use a [goal](/guides/goal-mode) when the
agent itself must do the work. An instance can hold many watches.

## Creating a watch

1. Choose a condition and a verifier that can observe it. Agent-created watches
   require a registered [plugin verifier](/guides/plugins#goal-and-watch-verifiers).
2. Ask the agent to watch it. Specify how often to check, when to give up, and
   what to do when it fires: “Watch this rollout for up to an hour, check every
   minute, and run the smoke test when it finishes.”
3. Inspect the saved watch in **Watches**. Confirm its verifier, expiry, and
   reaction prompt. A watch without an expiry can keep polling indefinitely.
4. Clear it when it is no longer needed. Disabling watch tools does not stop
   existing watches.

For plugin and authenticated API creation paths, lifetime fields, and a command-check example, see [Watch creation API](/reference/automation#watch-creation-api). Shell, test, CI, data, and LLM verifiers are operator-only.

## Configuring it

Watch tools are enabled by default. Configure them in **Settings → Behavior →
Watches** or `langgraph-config.yaml`:

```yaml
watches:
  enabled: true
  interval: 30
  keep_terminal_h: 24
```

`enabled` controls agent tool availability only; the background poller and stored
watches remain active. `interval` is the global polling cadence, with a five-second
minimum. A watch's `interval_s` is a further floor, so it cannot make polling
faster than the global cadence. See [configuration](/reference/configuration#watches).

## What a watch is made of

Each watch stores a condition, verifier, cadence, optional deadline and stall
threshold, and optional reaction prompt/session. Polling runs the verifier
without a full agent turn. **Verifier work can still cost resources:** an `llm`
verifier calls a model, a command runs a process, and plugin checks may do either.
The LLM verifier receives no chat transcript during a watch check; prefer a
verifier that directly observes the external state.

Plugin verifiers receive the watch identity in `ctx.invoker`, including its id,
reaction session, and effective interval. See the
[verifier contract](/guides/plugins#goal-and-watch-verifiers).

## Tripwire or monitor

| Mode | Agent tool flags | Operator/SDK fields | Fires when |
| --- | --- | --- | --- |
| One-shot tripwire | defaults | `trigger: met`, `repeat: false` | The verifier first passes; watch finishes `met` |
| Repeating tripwire | `repeat: true` | `trigger: met`, `repeat: true` | The predicate becomes true, then must go false before firing again |
| Change monitor | `on_change: true` | `trigger: change`, `repeat: true` | Evidence or reason differs from the previous check; first check only sets a baseline |

The agent's `on_change` flag implies repeat. In the operator API and SDK, set
`repeat: true` explicitly to keep watching after a change.
They call `on_changed` and emit `watch.value_changed`; tripwires call `on_met`.
`watch.changed` is the store-update event, not the value-change signal.

### Watching for a flapping watch

Every fire can enqueue an agent turn. For excessive reactions, inspect
`check_count`, `fire_count`, and `consecutive_fires` in `GET /api/watches`.
Repeated consecutive fires produce a deduplicated warning. `/metrics` exposes
`protoagent_watch_fires_total{trigger}` and
`protoagent_watch_flapping_total{trigger}`.

Raise `interval_s`, use a steadier verifier, or clear an unwanted monitor.
Dropping `repeat` makes an operator/SDK watch one-shot, including a `change` watch.

## Editing a live watch

Use `update_watch` rather than clearing and recreating a watch when you want to
retain its id and accumulated evidence.

| Path | Partial-update behavior |
| --- | --- |
| Agent `update_watch` | Pass changed fields only; `expires_in_s` starts from now; `clear_deadline: true` removes expiry |
| Plugin `await sdk.update_watch` | Omitted fields stay unchanged; `None` clears nullable fields |
| Operator `PATCH /api/watches/{id}` | Omitted fields stay unchanged; `null` clears nullable fields; can also replace the verifier |

Only active watches can be edited. Agent and SDK updates are restricted to
plugin-verifier watches and cannot replace the verifier. Changing `stall_after`
resets its stall counter and notification flag. Full SDK signatures are in the
[SDK reference](/reference/plugin-sdk-api#sdk-update-watch).

Plugins maintaining stable watch ids should list their prefix with
`sdk.list_watches(prefix=…)`, clear obsolete ids, and create or update current
ones. Replacing current ids alone leaves renamed or removed watches polling.

## Reacting

A fire with both `run_prompt` and `run_session` enqueues a one-shot agent turn.
It includes the condition and verifier evidence so the agent knows what changed.
Hooks run even without a reaction prompt. Plugins register them with
`registry.register_watch_hook(on_met=…, on_changed=…, on_expired=…, on_stalled=…)`.

A deadline allows an unmet watch to finish `expired`. `stall_after` counts
consecutive unchanged-evidence-and-reason checks: it calls `on_stalled` once per
stall episode and leaves the watch active. Neither expiry nor a stall runs the
normal `run_prompt` reaction.

Finished watches remain visible for `keep_terminal_h` (default 24 hours; `0`
keeps them indefinitely). Active watches do not age out. Clearing deletes the
record immediately; there is no stored `cleared` status.

A watch targeting an active goal's session pauses the goal's automatic drive.
Its reaction turn resumes that session and goal verification. Set a reaction
prompt when the watch is meant to wake the goal; a watch without one does not
schedule that follow-up turn.

## Watch vs goal — which?

| Need | Use |
| --- | --- |
| Agent must make tests pass or finish a document | [Goal](/guides/goal-mode) |
| Observe a deployment, metric, or CI result | Watch |
| Run a prompt at a time or on a recurring calendar cadence | [Schedule](/guides/scheduler) |
| Execute a saved sequence of subagent steps | [Workflow](/guides/workflows) |

Design history: [ADR 0067](/adr/0067-standalone-watch-primitive) and
[ADR 0079](/adr/0079-autonomous-operating-model).
