# ADR 0004 — Multi-Instance Data Scoping

- **Status:** Accepted (2026-05-30) — implemented (instance scoping + scheduler owner-lock)
- **Date:** 2026-05-30
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** architecture, deployment, state, scheduler, knowledge, multi-instance
- **Supersedes / Superseded by:** —

> Accepted. Running **multiple protoAgent instances on one machine** must not let
> them clobber each other's on-disk state. Today some stores are namespaced by
> agent name (scheduler, inbox) but several use **fixed paths** (checkpoints,
> knowledge, skills, workflows, memory) that two instances would share. The
> decision: scope **all** per-instance state under a single **instance-scoped
> data root** derived from one identifier, with the writable-fallback preserving
> the instance segment. The default (no instance id set) keeps today's exact
> paths — no migration for existing single-instance deployments.

---

## 1. Context & Problem Statement

We will run several protoAgent instances on one host (e.g. distinct personas, or
a fleet). Each instance keeps private on-disk state. An audit of how each store
resolves its path:

| Store | Resolver | Default path | Namespaced by |
|---|---|---|---|
| Scheduler `jobs.db` | `scheduler/local.py` `_resolve_db_path` | `/sandbox/scheduler/<agent>/jobs.db` | **agent name** |
| Inbox db | `server.py` `_build_inbox_store` | `/sandbox/inbox/<agent>.db` | **agent name** |
| Checkpointer db | `server.py` `_resolve_checkpoint_db` | `/sandbox/checkpoints.db` | **none (fixed)** |
| Knowledge db | `knowledge/store.py` `_resolve_path` | `/sandbox/knowledge/agent.db` | **none (fixed)** |
| Skills db | `server.py` `_resolve_skills_db` | `/sandbox/skills.db` | **none (fixed)** |
| Workflows (writable) | `server.py` `_build_workflow_registry` | `/sandbox/workflows` | **none (fixed)** |
| Memory persistence | `graph/middleware/memory.py` `MEMORY_PATH` | `/sandbox/memory/` | **none (fixed)** |

Each resolver also has a **writable fallback** to `~/.protoagent/...` when
`/sandbox` isn't writable (local dev). Critically, those fallbacks use *fixed*
names (e.g. `~/.protoagent/knowledge/agent.db`) — so even a store that scoped its
primary path would **lose the instance segment on fallback** and collide again.

### Collisions today

- **Same agent name, different ports** (what we hit in testing — two instances
  both named the default `protoagent`): scheduler + inbox share one `jobs.db` /
  inbox db. The scheduler is worse than a passive collision: *both* instances
  poll the shared `jobs.db` every second, so a due job is claimed by whichever
  ticks first and fired into **that** instance — the other never sees it. (The
  scheduler's self-invoke URL is *not* the bug; `_active_port` is set before the
  scheduler is built, so each fires to its own port correctly.)
- **Different agent names:** scheduler + inbox are isolated, but checkpoints,
  knowledge, skills, workflows, and memory still **share fixed paths** — so two
  distinct agents would cross-contaminate conversation history, long-term
  knowledge, the skill index, and saved workflows.

So **no** combination is fully isolated today.

## 2. Decision

Scope **all** per-instance state under one **instance-scoped data root**, keyed
by a single identifier, with the writable-fallback preserving the instance
segment.

### 2.1 One identifier

A single **instance id** resolved once at startup, in priority order:

1. `PROTOAGENT_INSTANCE` env, else
2. `instance_id` config field, else
3. empty → **legacy mode** (today's exact paths).

When empty, nothing changes — existing single-instance deployments keep their
current paths and data. When set, every store nests under it.

> **As implemented**, scoping is strictly opt-in via the env/config id above. We
> deliberately do **not** auto-derive it from a non-default agent identity name —
> that would silently move data for anyone who'd named their agent, breaking the
> zero-migration guarantee. Distinct instances on shared storage set a distinct
> `PROTOAGENT_INSTANCE`.

### 2.2 One scoped data root + a single path helper

Introduce a `scoped_data_dir(base) -> Path` helper used by **every** resolver:
when an instance id is set, it inserts that id as the first segment under the
data root (`/sandbox` or the `~/.protoagent` fallback) before the per-store
subpath; when unset, it returns the base unchanged. The **fallback** path goes
through the same helper, so the instance segment survives a `/sandbox`→
`~/.protoagent` fallback (the current gap).

Result, for instance `alice`:

```
~/.protoagent/alice/checkpoints.db
~/.protoagent/alice/knowledge/agent.db
~/.protoagent/alice/skills.db
~/.protoagent/alice/workflows/
~/.protoagent/alice/inbox/alice.db
~/.protoagent/alice/scheduler/alice/jobs.db
~/.protoagent/alice/memory/
```

The env-reading modules (`knowledge/store.py` `KNOWLEDGE_DB_PATH`,
`scheduler/local.py` `SCHEDULER_DB_DIR`, `graph/middleware/memory.py`
`MEMORY_PATH`) honor the same instance id when computing their defaults, so a
single knob scopes the whole process — no need to set six env vars per instance.

### 2.3 Operating model

One instance = one **distinct** instance id (and one port). Two instances must
not share an id — that's the same agent twice, and the scheduler's shared-poll
race makes it actively wrong. The launcher / docs make a distinct id (or
`AGENT_NAME`) the norm for additional instances.

## 3. Consequences

**Positive**

- Full isolation across every store with one knob; no cross-talk in history,
  knowledge, skills, workflows, or scheduled jobs.
- Backward compatible: unset id → byte-identical paths to today, zero migration.
- Centralizing path resolution removes the duplicated, drift-prone
  `/sandbox`→`~/.protoagent` fallback logic copied across modules.

**Negative / costs**

- Touches every store resolver; needs care that the fallback preserves the
  segment (the subtle part).
- A user who *renames* an instance (changes the id) leaves the old data behind
  under the old id — documented, not auto-migrated in v1.

## 4. Alternatives Considered

- **Per-store config paths only** (set `checkpoint_db_path`, `knowledge_db_path`,
  … per instance): already possible, but six knobs per instance is error-prone
  and the fallbacks still strip scoping. Rejected as the primary mechanism;
  remains available as an override.
- **Port-based namespacing** (key by bound port): guaranteed unique but not
  semantic, and state would "move" when an instance restarts on a different
  port. Rejected.
- **Always nest by agent name** (no separate id): simplest, but moves the
  default `protoagent` instance's existing data (migration). Rejected in favor
  of the legacy-preserving default; the agent identity name still *feeds* the id
  when non-default.

## 5. Implementation Sketch (follow-up)

1. `instance_id` resolution + `scoped_data_dir` helper (shared module).
2. Route every resolver (server.py + knowledge + scheduler + memory) through it,
   fallback included.
3. Tests: unset id → legacy paths byte-for-byte; set id → all stores nested and
   mutually isolated, including after a `/sandbox`→`~/.protoagent` fallback.
4. A **"Running multiple instances"** guide: set a distinct `PROTOAGENT_INSTANCE`
   (or `AGENT_NAME`) + port per instance; what's isolated; the rename caveat.

## 6. Related

- [ADR 0003 — Reactive Agent](/adr/0003-reactive-agent-activity-thread) — adds the inbox store (already agent-named).
- Scheduler: `scheduler/local.py`; Knowledge: `knowledge/store.py`; Memory: `graph/middleware/memory.py`.
