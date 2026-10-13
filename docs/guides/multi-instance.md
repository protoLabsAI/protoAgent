# Run multiple instances

Give each independently managed agent its own instance root and port. This
separates config, credentials, chats, memory, knowledge, tasks, and schedules.
For agents managed together from one console, use the [fleet](/guides/fleet).

## Shared machine: set an instance id

Run these in separate terminals:

```bash
PROTOAGENT_INSTANCE=alice uv run python -m server --port 7871
PROTOAGENT_INSTANCE=bob uv run python -m server --port 7872
```

These use `~/.protoagent/alice/` and `~/.protoagent/bob/`. Each instance has a
`config/` directory and its own stores. Both inherit machine-shared defaults
from `~/.protoagent/host-config.yaml`.

Without an explicit root or id, the default instance lives at
`~/.protoagent/default/`. Changing the agent's display name does not move its
state. Changing the instance id selects a different directory; existing data
stays in the old one.

Instance identity comes from the environment. Setting `instance.id` or
`instance_id` in YAML does not select the root. `PROTOAGENT_CONFIG_DIR` is retired
as a runtime root selector.

## Choose explicit roots

`PROTOAGENT_HOME` takes precedence over the instance id when selecting a directory:

```bash
PROTOAGENT_HOME=/srv/agents/alice protoagent serve --port 7871
```

For a throwaway test instance that also needs separate machine-shared config,
credentials, and heartbeats, set a box root:

```bash
PROTOAGENT_BOX_ROOT=/tmp/pa-review PROTOAGENT_INSTANCE=review \
  uv run python -m server --port 7881
```

A fresh box root has no model connection; configure one before testing chat.

## Containers

The bundled entrypoint sets `PROTOAGENT_HOME=/sandbox`. Use a separate data volume
for each container, and a different published host port. Containers that mount the
same volume share state even if their container names differ.

Explicit store overrides such as `KNOWLEDGE_DB_PATH`, `MEMORY_PATH`, `GOAL_PATH`,
and `SCHEDULER_DB_DIR` can point outside the instance root. Keep those separate too.

## Check isolation

Run `protoagent config explain` with the same environment as each server. Confirm
that the instance roots and store paths differ. From a connected console, the
same report is available at `GET /api/config/explain`.

The scheduler takes an exclusive lock on its `jobs.db`. If another process owns
it, scheduling waits and retries while the rest of the server remains available.
Give the instances distinct roots or correct the scheduler override. The lock
protects scheduling; it does not isolate the other stores.

## Related

- [Configuration](/reference/configuration)
- [Environment variables](/reference/environment-variables)
- [Schedule future work](/guides/scheduler)
- [Instance paths decision](/adr/0065-two-tier-instance-paths)
