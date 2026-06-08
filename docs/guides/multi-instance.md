# Run multiple instances

Running several protoAgent instances on one machine needs each one's on-disk
state (conversation checkpoints, knowledge, skills, workflows, memory, scheduled
jobs, inbox) kept separate. How much you need to do depends on how you run them.
See [ADR 0004](/adr/0004-multi-instance-data-scoping) for the full rationale.

## The easy path: one container per instance

Each container has its own filesystem, so the default `/sandbox/...` paths are
**already isolated** — nothing to configure. This is the recommended way to run
a fleet. Give each container a distinct port if you expose them on the host.

## Shared filesystem: set an instance id

When several instances share one filesystem — multiple bare processes on a host,
or containers that mount the **same** volume — the default paths (and the
`~/.protoagent/...` fallback used when `/sandbox` isn't writable) would collide.
Scope each instance with a distinct id:

```bash
PROTOAGENT_INSTANCE=alice  python -m server --port 7871
PROTOAGENT_INSTANCE=bob    python -m server --port 7872
```

or in `config.yaml`:

```yaml
instance_id: alice
```

With an id set, **every** store nests under it — e.g.
`~/.protoagent/alice/checkpoints.db`, `~/.protoagent/scheduler/alice/…/jobs.db`,
`~/.protoagent/alice/memory/`, and so on — so two ids never share a file. The
env var wins over the config field.

## Don't want to name every instance? Auto-scope (#706)

Running a lot of instances on one box and don't want to set an id for each? Turn on
**auto-scoping** — each instance derives a stable id from its **working directory** so
co-located instances never silently share the root:

```bash
PROTOAGENT_AUTO_SCOPE=1   # export once (e.g. in your shell profile)
```

With it on, an instance with no explicit `PROTOAGENT_INSTANCE` scopes to
`<dirname>-<hash>` of its cwd — isolated by default, stable across restarts. It's
**opt-in** (default off) because turning it on relocates an existing *unscoped*
deployment's data to its scoped dir. Instances launched from the **same** directory
(e.g. on different ports) still need an explicit `PROTOAGENT_INSTANCE` — and the server
**warns loudly at boot** whenever it runs unscoped against a non-empty data home, so the
silent clobbering of #706 can't happen quietly.

### Opt-in — no migration

Leaving the id **unset** keeps the exact single-instance paths used today, so
existing deployments are untouched. Scoping is purely additive: set an id only
when you actually run more than one instance on shared storage. (Note: a
*non-default agent name* does **not** auto-scope — only an explicit instance id
does — so naming your agent never silently moves its data.)

One instance = one id + one port. Renaming an instance's id points it at a fresh
data root; the old data stays under the old id (no auto-migration).

## Safety interlock

Even with the guidance above, a misconfiguration (two instances sharing a
`jobs.db`) is easy to make and fails *silently* — both schedulers poll the same
table and a due job is fired by whichever ticks first, so the other never sees
it. To catch this, the scheduler takes an **exclusive owner-lock** on its
`jobs.db` at startup. If another live instance already holds it, the scheduler
logs a loud error and **does not start** (the rest of the agent serves normally):

```
[scheduler] jobs.db at <path> is already owned by another live instance —
not starting the scheduler. Run each instance with a distinct
PROTOAGENT_INSTANCE (or agent name) so they don't share a jobs.db.
```

The fix is always the same: give the instances distinct `PROTOAGENT_INSTANCE`
ids (or run them in separate containers).

## Related

- [ADR 0004 — Multi-Instance Data Scoping](/adr/0004-multi-instance-data-scoping)
- [Scheduler](/guides/scheduler)
- [Configuration](/reference/configuration)
