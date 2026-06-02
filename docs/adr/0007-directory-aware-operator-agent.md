# ADR 0007 — Directory-Aware Operator Primitives (enabling a "Roxy" fork)

- **Status:** Accepted (2026-06-01) — template primitives shipped (registry+fence, fenced fs toolset, fork guide); per-project subagent binding deferred
- **Update (2026-06-02):** the **fenced filesystem is now ON by default**, scoped to a default **workspace** dir (`paths.workspace_dir`) when no projects are configured — a capable, safe first run (the agent can work with files, but only inside the fence). Benchmarking OpenClaw/Hermes (both ship FS default-on) + the UX research ("anticlimactic first run", "value off by default") motivated flipping the *read/write/edit/search* default. The two **unsandboxed** power tools are *fenced cwd but arbitrary argv/code as the server user* — not a real sandbox. As of Sprint A, **`run_command` is now ON by default but gated behind HITL approval** (`filesystem.run_requires_approval`): each command pauses for the operator to approve/deny (intermediate confirmation on consequential actions, per the research) — capable, not dangerous-by-default. `execute_code` stays **opt-in** (no per-call gate yet; enable inside the hardened container, ADR 0008).
- **Date:** 2026-06-01
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** architecture, filesystem, multi-project, operator, supervisor, security, template-vs-fork
- **Supersedes / Superseded by:** subsumes the parked single-`working_dir` design noted in ADR 0005

> Accepted (design). We want an agent that **manages several project directories
> and keeps work flowing** through our ProtoMaker workspaces — with full
> read/write/management authority. **But that operator does not ship in the
> template.** protoAgent is the template; the operator becomes a **fork —
> "Roxy" — in its own repo.** So this ADR scopes what belongs *here*: the
> **generic, opt-in, off-by-default primitives** that make such a fork possible
> (a fenced multi-project filesystem toolset, a project registry, per-project
> subagent binding) — and nothing operator- or ProtoMaker-specific. The operator
> persona, the "is work flowing?" perception, and the supervision policy are
> **composed in the Roxy fork** from a `SOUL.md` + a `SKILL.md` + config + the
> existing scheduler — no template code. `allowed_dirs` stops being a
> console-only nicety and becomes the **hard fence** on every agent fs op.

---

## 1. Context & Problem Statement

The goal is a multi-project operator ("Roxy") with full write + management over
a set of ProtoMaker workspaces, keeping work flowing (features moving, PRs not
stalling, trees not drifting). The constraint added during design: **don't
package that operator into every protoAgent — it's a fork, in a new repo.**

protoAgent is a *template*: forks specialize via `SOUL.md` (persona),
`SKILL.md` (methodology), config, plugins, and MCP — the core ships neutral,
opt-in defaults (see ADR 0001, and the "OVERRIDE THIS in your fork" guidance in
`graph/prompts.py`). The operator is therefore a *specialization*, not a core
feature. What the core is **missing** is the raw capability a fork would need:

| Needed by an operator fork | In the template today? |
|---|---|
| General filesystem read/write/edit/list/glob/grep, fenced | ❌ none (only Notes tabs, `execute_code`, internal `run_command`) |
| A registry of managed project dirs + per-project scoping | ❌ runs at repo-root cwd; no registry |
| `allowed_dirs` enforced on the *agent's* tools | ❌ gates the React console only |
| Per-project subagent binding | ❌ subagents are global |
| A scheduled "sweep" driver | ✅ scheduler + inbox + event bus (ADR 0003) |
| A way to read workspace/board state | ✅ *composable* from generic fs tools (no special code needed) |

So the template work is: **add the generic primitives (opt-in), make the fence
real, and document the fork pattern.** The operator itself is built downstream.

## 2. Decision

### 2.1 Ships in the template (generic, opt-in, OFF by default)
Three primitives — domain-neutral, useful to *any* fork, and inert unless turned
on (so they are not "packaged for every agent"):

1. **Project registry + a real fence.** Config `projects: [{name, path, write}]`;
   the union of project paths + `operator_allowed_dirs` is the **enforced**
   writable allowlist, applied via `resolve_project_path` to *every* fs tool.
   Empty registry (the default) → today's exact behavior. Instance-scoped (ADR
   0004). When non-empty, the managed set is injected into the system prompt.
2. **Native gated filesystem toolset** (`tools/fs_tools.py`) — `list_dir`,
   `read_file`, `write_file`, `edit_file`, `glob`, `grep`,
   `run_command(cwd=…)`; every path resolved through the fence. **Opt-in**
   (`filesystem.enabled`, default off, like `execute_code`); per-project
   `write:false` → read-only; every mutation audited; never given to subagents
   unless their allowlist names it.
3. **Per-project subagent binding** — extend `task(name, …, project="…")` to
   inject the project path into the subagent prompt + scope its fs tools' `cwd`.
   No-op when no registry is configured.

### 2.2 Composed in the fork (Roxy — separate repo, NOT here)
The operator is assembled from template primitives + standard fork mechanisms —
**no new core code**:

- **`SOUL.md`** — the operator persona ("you manage these workspaces; keep work
  flowing; escalate when judgment is needed").
- **A `project-operations` `SKILL.md`** — *how* to sitrep + keep flowing using
  the generic tools: read `.beads/issues.jsonl` (board), `.automaker/`
  (features), and `git`/`gh` via `read_file`/`glob`/`run_command`. The
  ProtoMaker-specific knowledge lives here, retrieved on demand — not in core.
- **Config** — the `projects` registry, `filesystem.enabled: true` (+ per-project
  `write`), and a scheduler job for the periodic sweep.
- **Driver** — the existing scheduler (ADR 0003) fires a "sweep all projects"
  turn; the skill + persona do the rest; escalation rides the inbox / Activity
  thread. Optionally the `protolabs_studio` MCP for richer board actions.

This is the same specialization path every fork uses — the operator is "just"
a well-equipped fork once the primitives exist.

## 3. Architecture detail (template side)

### 3.1 Project registry + the fence
- `projects: [{name, path, write: true|false}]` (top-level config). The writable
  allowlist = resolved project paths ∪ `operator_allowed_dirs`.
- `resolve_project_path(path, allowed_dirs)` (already `..`/symlink-safe) gates
  **all** fs tools; outside-allowlist paths are refused (not clamped).
- The agent's own repo is **excluded by default** (no accidental
  self-modification) — managing it is an explicit registry entry.
- Default (empty registry + `filesystem.enabled:false`) = byte-identical to
  today; a plain protoAgent fork is unaffected.

### 3.2 Native gated filesystem toolset
- Tools take a `project` (registry name) + a workspace-relative `path`, join +
  resolve through the fence. No absolute escapes.
- `run_command(project, argv, …)` wraps `graph/shell.py::run_command` with a
  fenced `cwd` (so `git`/`gh`/build commands run in the right workspace).
- Opt-in; read-only per project supported; writes audited (path + diff/size) by
  `AuditMiddleware`; git is the seatbelt (branch-first, surface diffs, no
  force-push); destructive ops inspect-before-acting (house rule).

### 3.3 Per-project subagent binding
- `task(..., project="…")` → subagent prompt gains the project path; its fs
  tools are scoped to that `cwd`. Lets the operator fork dispatch parallel
  per-project workers without bespoke wiring.

### 3.4 What the template deliberately does NOT ship
- No operator/supervisor persona, no "keep work flowing" loop, no ProtoMaker
  board/`.automaker` parsing, no sweep policy. Those are Roxy's `SOUL`/`SKILL`/
  config. Keeps the template neutral and the capability inert until a fork opts in.

## 4. Security model (the crux)

Granting write authority over several repos is a real escalation — handled the
same whether it's a fork or core:

- **`allowed_dirs` is the hard fence**, enforced in every fs tool. Outside paths
  refused; `..`/symlink escapes fail via `Path.resolve`.
- **Opt-in + per-project write toggle**; agent's own repo excluded by default.
- **Audited** — every mutation in the audit log + Langfuse/Prometheus.
- **Git as the seatbelt** — branch-first, diffs surfaced, PRs (not direct
  protected-branch writes), no force-push.
- **Inspect-before-destroy** + **bounded autonomy** (a fork's sweep takes only
  small reversible actions; consequential ones escalate to the operator —
  advise-first, per ADR 0006).

## 5. Ranked plan (template slices)

1. ✅ **Project registry + fence + gated fs toolset — shipped.** `config.filesystem`
   (`enabled`/`allow_run`/`projects[{name,path,write}]`); `tools/fs_tools.py`
   (`list_projects`/`list_dir`/`read_file`/`find_files`/`search_files`/
   `write_file`/`edit_file`/`run_command`) with a `ProjectRegistry` fence on
   every path; opt-in, off by default (empty registry → no behavior change);
   managed set injected into the prompt (`_build_projects_section`).
2. ✅ **Fork guide — shipped.** `docs/guides/operator-fork.md` walks a monitor
   fork (Roxy) end to end (read-only fs, persona, `project-operations` skill,
   A2A/bus/scheduler drivers).
3. **Per-project subagent binding** *(deferred)* — `task(..., project=…)`.
   Not needed for a monitor-and-unblock operator (Roxy doesn't fan out coding
   workers); revisit if a coding fork needs parallel per-project edits.

The **Roxy fork** (`protoLabsAI/roxy`, new repo) applies the guide: monitor +
unblock, not code; summoned via A2A; managed via the protoWorkstacean bus.

## 6. Consequences

**Positive**
- The capability to manage N dirs exists, but the template stays neutral —
  nothing operator-specific ships to ordinary forks (off by default).
- The fence finally makes `allowed_dirs` a real agent boundary.
- Roxy becomes a thin, declarative fork (persona + skill + config), not a code
  forklift — the template's specialization model proven once more.

**Negative / costs**
- Real authority surface — the security model must hold (§4); full-write off by default.
- The fork must own coordination with in-flight ProtoMaker agents (don't clobber).
- Generic fs tools + deferral (ADR 0005) must interoperate (search/expose them sanely).

## 7. Alternatives considered

- **Ship the operator in the template** — rejected by the explicit constraint:
  don't package ProtoMaker/operator behavior into every agent. Primitives only.
- **MCP filesystem server instead of native tools** — viable for a fork, but the
  template owning fenced+audited fs tools gives better control + a uniform fence;
  native is the template primitive, MCP remains a fork option.
- **`execute_code` + `cwd` as the only fs surface** — too blunt / hard to audit
  per-edit; kept as a complement.
- **ProtoMaker perception as core code** — rejected; it's a fork skill composed
  from generic tools, so the template carries no `.beads`/`.automaker` coupling.

## 8. Open questions (for the Roxy fork, mostly)

- Does the operator **commit + open PRs** in managed repos vs. edit-and-leave?
  (Leaning branch + PR — but a fork decision.)
- **Coordination** — how Roxy avoids clobbering an active ProtoMaker agent's
  feature (board/agent-status check first).
- Heartbeat **action budget** — autonomous vs. escalate (fork policy).
- Should the template ship a *generic* (domain-neutral) `project_sitrep` helper,
  or leave all perception to the fork skill? (Leaning: leave to the fork; the
  generic fs tools already make it a skill, not code.)

## 9. Related

- [ADR 0001 — Extensibility & Plugins](/adr/0001-extensibility-and-plugin-architecture) — the SOUL/SKILL/config/plugin specialization model the Roxy fork rides.
- [ADR 0003 — Reactive Agent](/adr/0003-reactive-agent-activity-thread) — scheduler/inbox/event bus drive a fork's sweep + escalation.
- [ADR 0004 — Multi-Instance Data Scoping](/adr/0004-multi-instance-data-scoping) — per-instance isolation for a multi-project deployment.
- [ADR 0005 — Tool Pollution](/adr/0005-tool-pollution-and-progressive-disclosure) — supersedes its parked single-`working_dir` note; fs tools respect deferral.
- [ADR 0006 — Observability](/adr/0006-observability-and-the-self-improving-flywheel) — advise-first posture + audit/telemetry for fs mutations.
- Code seams: `operator_api/paths.py` (`resolve_project_path`), `graph/config.py`
  (`operator_allowed_dirs`), `graph/shell.py` (`run_command` cwd),
  `tools/execute_code.py`, `graph/subagents/`, `scheduler/`.
