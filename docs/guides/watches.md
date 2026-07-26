# Watches

A **watch** is a standing tripwire: *poll a condition on a cadence, and when it trips, react.*
It's the passive counterpart to a [goal](/guides/goal-mode) — a goal is what the *agent
drives* (its own turns do the work); a watch is what an *external process* moves (a deploy, a
training run, a metric climbing) while the agent supervises. Unlike a goal (one per session)
you can hold **many** watches at once — the primitive for an agent that babysits several things
in parallel (ADR 0067).

When a watch is **met**, it can run a follow-up agent turn (via `run_in_session`) and fires
`on_met` hooks. A `deadline` finishes it `expired`; `stall_after` fires `on_stalled` when the
metric stops moving.

## Configuring it

The watch tools are **on by default** (they shipped off under #2020 while the feature settled).
Both knobs live in `langgraph-config.yaml`, and both are editable from **Settings ▸ Behavior ▸
Watches**:

```yaml
watches:
  enabled: true         # bind create_watch / list_watches / clear_watch for the agent
  interval: 30          # global poll cadence, seconds (min 5)
  keep_terminal_h: 24   # retire met/expired watches after this long (0 = keep forever)
```

`enabled` is the only gate, and watches are **independent of goal mode**. It decides whether the
**agent** gets the tools; it never touches stored watches, and **the background poller runs
regardless**, so a watch created by an operator (`POST /api/watches`) or a plugin is polled even
on an instance where the agent has no watch tools at all. See
[Configuration ▸ `watches`](/reference/configuration#watches).

## What a watch is made of

`{ condition, verifier, interval_s?, deadline?, stall_after?, run_prompt?, run_session? }` — the
`verifier` is the same spec [goals use](/guides/goal-mode#verifier-types) (`plugin` / `command`
/ `test` / `ci` / `data` / `llm`). It's polled **out-of-band** on the `watches.interval` cadence
(default 30s),
verifier-only — no agent turn, no model call. A `plugin` verifier is handed the invoking
watch's identity on `ctx.invoker` (`kind="watch"`, the watch `id`, its `run_session`, and the
effective `interval_s`) so one verifier can keep **per-watch** state — see
[Plugins ▸ Goal & watch verifiers](/guides/plugins#goal-and-watch-verifiers).

## Creating a watch

- **Agent tool** — `create_watch(condition, check, run_prompt=…)`; `list_watches` / `clear_watch`
  manage them. Plugin-verifier only (like `set_goal`) — the agent can't open a shell/eval watch.
  The lifetime knobs are on the tool too: `interval_s` (this watch's own cadence floor),
  `expires_in_s` (give up after N seconds **from now** → `expired`) and `stall_after`. The
  deadline is *relative* here, unlike the operator API's absolute `deadline`, because a model
  has no reliable "now" — an ISO timestamp it guessed in the past would expire the watch on its
  first tick. Set at least `expires_in_s` whenever the thing being watched has a deadline: a
  watch created with none of these polls until something clears it.
- **Plugin (SDK)** — `sdk.create_watch(*, condition, verifier, run_prompt=…)`, and react with
  `registry.register_watch_hook(on_met=…, on_expired=…, on_stalled=…)`. The lifecycle half
  (#1638): `sdk.list_watches(prefix="")` (each `{id, condition, status, verifier}`,
  optionally id-prefix-filtered) and `sdk.clear_watch(watch_id) → bool`. A plugin that arms
  a watch **suite** under stable ids should make its arm step a *reconcile*: clear the
  `myplugin-*` ids no longer in its spec set, then create/replace the rest — stable-id
  replace alone only heals specs that still exist, so a renamed or dropped spec would keep
  polling its verifier forever (worse after uninstall, when that verifier is unresolvable).
- **Operator (REST)** — `POST /api/watches` accepts **any** verifier type (it's on the `/api`
  operator surface, gated by the [federation-token ceiling](/reference/configuration#secrets));
  plus `GET /api/watches` and `DELETE /api/watches/{id}`.

```jsonc
// operator: watch a deploy, run the smoke test when it finishes
POST /api/watches
{ "condition": "rollout complete",
  "run_prompt": "Run the smoke test and report.", "run_session": "ops",
  "verifier": {"type": "command", "command": "kubectl rollout status deploy/api"} }
```

## Reacting

On **met**, the optional `run_prompt` is enqueued as a **one-shot agent turn** in `run_session`
via [`sdk.run_in_session`](/guides/plugins) — non-blocking — and `on_met` hooks fire. A
`deadline` (ISO-8601 or epoch) finishes the watch `expired` (fires `on_expired`); `stall_after`
N consecutive **unchanged**-evidence checks fire `on_stalled` once per stall episode **without**
ending the watch. The console **Watches** panel lists every watch with its status, and toasts on
met/expired.

A finished watch sticks around so you can see *what* tripped — then the tick retires it after
`watches.keep_terminal_h` (default 24h). Only `met`/`expired` watches age out; an `active` watch
polls for as long as it needs to, however old. Set `keep_terminal_h: 0` to keep every trip
forever. Clearing a watch is different from it aging out: `clear_watch` / `DELETE /api/watches/{id}`
**deletes the file immediately**, so there is no `cleared` state to find in a listing.

**Wake-framing (ADR 0079).** The reaction turn doesn't arrive as a bare `run_prompt`. The
scheduler prepends a *why-you're-awake* header (`[Autonomous wake — a watch you set has tripped.
Orient from <working_state>, then:]`) and the watch controller prefixes the tripping
**condition** and the verifier's **evidence** — so the agent orients on wake instead of acting
blind. The evidence is load-bearing: an agent that can't re-read the source can still act on the
value the watch surfaced (e.g. a release tag carried in `Evidence:`).

**Yield-and-resume with a goal (ADR 0079).** A watch is how an [active goal](/guides/goal-mode)
hands off async work: the goal drive **pauses** while a watch on the goal's `run_session` is
live, and the watch's met-reaction **resumes** that same session — the goal's verifier re-runs on
the resumed turn, so the loop closes without the agent spinning.

## Watch vs goal — which?

| | Goal (drive) | Watch |
|---|---|---|
| Who moves the metric | the agent's own turns | an external process |
| On "not met" | re-invoke the agent | wait, re-check next tick |
| How many at once | one per session | **many** |
| Use for | "make the tests pass," "finish the README" | "watch the deploy / treasury / CI" |

Goals used to carry a `monitor` disposition for this; ADR 0067 split it into its own primitive
so a supervisor agent can hold many.
