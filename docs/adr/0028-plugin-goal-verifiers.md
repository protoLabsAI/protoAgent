# 0028 — Plugin-contributed goal verifiers (+ safe programmatic goals)

Status: **Accepted** (sliced — D3 amended to `plugin`-only after review; see D3/D5)

> Authored from the protoTrader-in-space fork, where an autonomous agent makes
> progress out-of-band (a background engine + a scheduler tick) and surfaced the
> gaps below. Proposed for upstream so forks don't each re-invent the workaround.

## Context

The goal system lets an operator set a standing objective that the agent self-drives
toward: `GoalController.evaluate` runs after each terminal turn, a **verifier** checks
the outcome, and the agent is re-invoked with a continuation prompt until the verifier
passes (or the iteration/no-progress budget is spent). Verifiers are the ground truth —
they run *before* honoring a `<goal_unachievable>` give-up.

Two facts shape everything here:

1. **The verifier set is closed.** `graph/goals/verifiers.py` holds a hardcoded
   `VERIFIERS = {command, test, ci, data, llm}` dict, dispatched by `VERIFIERS.get(type)`.
   There is **no registration path** — `graph/plugins/registry.py` has ten `register_*`
   hooks (`tool`, `subagent`, `router`, `mcp_server`, `a2a_skill`, `thread_id_resolver`,
   `surface`, `skill_dir`, `workflow_dir`, …) and **none for goals**.

2. **Setting a goal is operator-only, by design.** A goal is set only via the `/goal`
   control message (`controller.parse_control`); the REST surface is list/clear, not set;
   there is no goal tool. This is deliberate: the `command`/`test`/`ci` verifiers execute
   **shell on the host**, so "let agents/plugins set goals" would mean "let them set
   arbitrary verifier commands" — remote code execution. The verifier docstring says as
   much: *only set goals from trusted input.*

This is fine for interactive use. It breaks down for an **autonomous or long-running
agent** whose progress happens outside a chat turn:

- **No clean way to ground-truth domain state.** To verify "reach N credits" against the
  live game, the fork shells out: a `command` verifier runs a helper that curls the
  agent's own HTTP endpoint and exits 0/1. It works, but it's clunky *and* it's the very
  shell-exec surface that forces operator-only.
- **A self-improving agent can't own its objective.** Because set is `/goal`-only (gated
  by the RCE concern above), neither the agent nor a plugin can establish or close a
  standing goal programmatically.
- **(Noted, deferred — D6)** Goals evaluate only after a terminal turn *in their session*.
  Progress made out-of-band (a background engine, a scheduler tick in another context)
  never triggers evaluation, so a met goal can sit `active` indefinitely.

## Decision

Make the verifier set **extensible by plugins**, add a `plugin` verifier type, and —
because a plugin verifier is **reviewed in-process code with declarative args and no host
shell** — safely allow a **programmatic goal-set restricted to plugin verifiers**. The
shell-exec verifiers that motivated operator-only stay operator-only.

### D1 — `register_goal_verifier(name, fn)` on the plugin registry

A new registry hook, mirroring the existing `register_*` surfaces:

```python
# in a plugin's register():
registry.register_goal_verifier("spacetraders:credits", verify_credits)

async def verify_credits(spec: dict, ctx: VerifyContext) -> VerifyResult:
    # in-process; reads live state the plugin already owns; NO shell
    have = await current_credits()
    want = int(spec.get("args", {}).get("min", 0))
    return VerifyResult(have >= want, f"credits {have:,} / {want:,}", evidence=str(have))
```

`VERIFIERS` becomes a base dict plus a registered overlay; a name collision is rejected +
logged (same posture as `register_tool`). The `fn` contract is identical to the built-in
verifiers (`(spec, ctx) -> VerifyResult`), so nothing in the controller loop changes.

### D2 — a `plugin` verifier type in the goal spec

```jsonc
{ "type": "plugin", "check": "spacetraders:credits", "args": { "min": 1000000 } }
```

The dispatcher resolves `check` against the registered verifiers; `args` are **declarative
data** validated by the plugin's verifier (never interpolated into a shell). Names are
namespaced `<plugin-id>:<name>` to avoid collisions.

### D3 — Safe programmatic goal-set (gated to the `plugin` verifier only)

Add a goal-set path (a tool and/or `POST /api/goals`) that accepts a goal **only** with a
`plugin` verifier — **never** `command`/`test`/`ci` **and never `data`**. A `plugin`
verifier is reviewed in-process code whose `args` are **declarative data the plugin itself
validates** (no shell, no `eval`, no path), so an agent or plugin can establish and close a
standing objective **without opening a code-execution surface**.

> **Why `data` is excluded (review finding).** An earlier draft allowed `data` too, on the
> reasoning that it "carries no host-shell surface." It doesn't — but it carries a different
> code-exec surface: the `data` verifier runs `eval(spec["expr"], {"__builtins__":
> _SAFE_BUILTINS}, {"data": data})` (`verifiers.py`). Restricted *builtins* is **not** a
> sandbox — attribute access is open, so a spec-supplied `expr` escapes via
> `().__class__.__bases__[0].__subclasses__()` → `os`/`subprocess` (full RCE), and `data`'s
> `spec["path"]` is an arbitrary file read. Letting an agent set a `data` goal would hand it
> that sink — re-opening, in a better-hidden form, exactly the RCE the shell verifiers are
> kept operator-only for. So `data` stays **operator-only** alongside `command`/`test`/`ci`;
> only `plugin` is safe to set programmatically.

The operator `/goal` path keeps full access to every verifier type (it's already gated to
trusted operator input). D3 only governs the *programmatic* (agent/plugin/REST) set.

### D4 — Goal lifecycle hooks (optional)

`register_goal_hook(on_achieved=…, on_failed=…)` fired from the controller's terminal
decision, so a plugin can react — push a notification, record a finding, or set the next
goal. Turns the goal system into a building block for a self-improving loop instead of a
dead-end status.

### D5 — Trust model

A plugin goal verifier is **trusted, reviewed, in-process code** — the same posture as any
enabled plugin (ADR 0027: *install ≠ enable ≠ trust*; enabling **is** the trust decision).
What makes the `plugin` verifier safe for D3 is the combination the built-in
`command`/`data` verifiers lack: **declarative, plugin-validated args + no host shell + no
`eval` + no arbitrary path.** The code-exec surfaces that justify operator-only are the
shell verifiers (`command`/`test`/`ci`) **and** the `data` verifier's `eval` (see D3), and
all of them stay operator-only. We are not loosening the trust boundary; we are giving
plugins a verifier path that never had a code-exec surface to begin with.

> **Sharp edge to harden separately:** the `data` verifier's restricted-builtins `eval` is
> escapable even for operator-set goals (it's just gated to trusted input today). A
> follow-up should replace it with a real safe evaluator (e.g. a small AST allowlist like
> the `calculator` tool uses) so `data` could eventually be eligible for programmatic set
> too. Tracked as future work, not part of this ADR.

### D6 — Out-of-band evaluation (DEFERRED / future slice)

The deeper gap — goals only evaluate after a terminal turn in their session — is **out of
scope** for this ADR but noted so D1–D4 don't preclude it. A future slice could add a
scheduler-driven "evaluate goals" tick, or `controller.evaluate_now(session)` that a plugin
calls when its state changes, so an autonomous agent's out-of-band progress closes the goal
automatically. Until then, the practical pattern is: drive a turn in the goal's session, or
keep far-off standing objectives in the scheduler/tick prompt (the goal continuation loop is
built to drive a session to done in a bounded number of iterations, **not** to poll a
distant target — using it for the latter storms the loop).

## Consequences

- Plugins ground-truth their own domain state cleanly, with no shell-out and no RCE smell.
- Autonomous / self-improving agents can **own and close** their objectives safely.
- Small, additive surface: one registry hook + one verifier type + one gated set-path +
  optional hooks. No change to the controller's evaluate loop or to existing verifiers.
- The `command`-shell-out pattern stops being the only way to verify live domain state.

## Alternatives considered

- **Status quo (`command` verifier shells out).** Works, but clunky, carries an RCE
  surface, and — being a shell verifier — keeps the goal operator-only, so the agent can't
  own it. This is exactly what the fork does today and wants to retire.
- **Loosen operator-only wholesale.** Lets agents set `command`/`test`/`ci` goals → RCE.
  Rejected.
- **Goal-as-a-tool with an arbitrary verifier.** Same RCE risk as above.
- **An external (MCP) verifier service.** Heavier and out-of-process for something the
  plugin already evaluates in-process; the registry hook is simpler and sufficient.

## Slices (vertical, smallest-useful-first)

- **PR1** — `register_goal_verifier` + the `plugin` verifier type (D1, D2). The core; makes
  ground-truthing domain state a first-class plugin capability.
- **PR2** — safe programmatic set gated to the `plugin` verifier only, via a goal tool and/or
  REST (D3).
- **PR3** — lifecycle hooks (D4).
- **Future** — out-of-band evaluation (D6).

## Reference implementation

The protoTrader-in-space fork (a live-SpaceTraders agent) is the motivating case: it
currently grounds "reach N credits" with a `command` verifier running
`plugins/spacetraders/check_credits.py` (curls its own `/plugins/spacetraders/state`
endpoint). Under PR1 that becomes a `spacetraders:credits` plugin verifier reading the same
state in-process; under PR2 the fleet-commander could own its own credits goal. The fork can
prototype PR1 behind this ADR and true up to the upstream shape once accepted.
