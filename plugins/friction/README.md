# Friction Log

The agent records its own friction — missing or awkward tools, confusing errors, wrong
paths it recognizes — and the plugin auto-captures what a detector can see. One ledger,
split into **harness** friction (an improvement to the tools/framework) and **model**
friction (a labeled trace worth learning from). First-party, **on by default**; disable
with `plugins: { disabled: [friction] }`.

| Surface | What it does |
|---|---|
| `record_friction` / `friction_review` / `resolve_friction` | The agent's tools: record, read, and resolve once fixed |
| `<working_state>` (ADR 0079) | Open friction that is `major`, or repeated `working_state_repeat_threshold` times, is shown to the agent every turn |
| **Friction** rail view | The operator's ledger: filter, resolve/reopen, file as a GitHub issue, fleet rollup on a hub |
| `/friction` | Operator summary; `/friction <text>` resolves matching entries |
| `friction_triage` | A read-only subagent that turns the backlog into a filing plan |

The ledger is `$FRICTION_LOG` or `instance_paths().store("friction")/friction.jsonl`,
append-only and capped at the newest 2000 entries.

## What auto-capture logs

- **Tool errors** — a tool that raises (HITL interrupts, delegation and cancellation are
  control flow, not friction). Logged `major`.
- **Shell commands that duplicate a first-class tool the agent has bound.** A call to a
  shell tool (`run_command`, `execute_command`, `shell`, `bash`) is friction only when its
  command's first word maps to a tool this agent actually has:

  | Command | First-class tool |
  |---|---|
  | `cat` / `echo` / `printf` with a `>` / `>>` redirect | `write_file` |
  | `cat`, `head`, `tail`, `less`, `more` | `read_file` |
  | `grep`, `egrep`, `fgrep`, `rg`, `ag`, `ack` | `search_files` |
  | `ls`, `tree` | `list_dir` |
  | `find`, `fd` | `find_files` |
  | `sed -i …`, `awk -i inplace …` | `edit_file` |
  | `tee` | `write_file` |

  The first word is read after stripping what isn't the command: `cd X &&` / `cd X;`,
  `VAR=value`, `sudo`, `env …`, `time`, `mise exec … --` and `sh -c '…'`. For a pipeline
  only the first segment counts, so `cat f | grep x` is a read and `git diff | grep x`
  is git. The entry names the fix: *"used `cat` via run_command — read_file does this"*.

  **Everything else is not friction**: git, test runners, type checkers, builds, package
  managers, project scripts. They have no first-class equivalent, so a shell tool is the
  right tool for them. `python` / `exec` carry code rather than a shell line, so they
  keep the coarse *"reached for escape hatch 'python'"* signal.

Why it works this way: a hands-on coding agent's ledger held 131 auto entries, all
*"reached for escape hatch 'run_command'"*. 127 of them were git, vitest/tsc/npm and
mise. Only 4 (`ls`, `cat`, `grep`) duplicated a tool it had. Because the group repeated,
the agent was also told *"minor x131: reached for escape hatch 'run_command'"* in its
working state every turn, which nudged it away from its main tool.

## Config

`friction:` in the agent config, editable in **Settings ▸ Plugins ▸ Friction Log** (read
live, so there's no restart):

| Key | Default | Meaning |
|---|---|---|
| `working_state` | `true` | Show open friction in the agent's working state |
| `working_state_limit` | `3` | At most N friction lines in the working state |
| `working_state_repeat_threshold` | `3` | Repeats before a `minor` group is shown (`major` always is) |
| `issue_repo` | `""` | `owner/name` the view files issues against. Empty = copy-to-clipboard |
| `escape_hatch_exempt` | `[]` | Tool names whose calls are never escape-hatch friction (e.g. `[run_command]`) |

**Working-state counts are bucketed.** Counts are exact up to 9 (`x3`, `x7`), then shown
as `x10+`, `x100+`, `x1000+`. The line is re-rendered into the prompt every turn, and an
exact `x131` → `x132` changed the prompt text (and invalidated its cache) without saying
anything new. The ledger, the view and `/friction` keep exact counts.

**`issue_repo` is not derived from the managed-projects registry.** It used to fall back
to the first `projects[].github`. Harness friction is about the agent runtime, not the
repo the agent is working in, so an agent onboarding someone else's repo (say a private
interview repo) would have had its harness friction filed there. Pin `issue_repo`
(e.g. `protoLabsAI/protoAgent`, or your fork) to get one-click issue links.

## Upgrading: clearing old escape-hatch noise

Existing ledgers keep every row. Entries recorded before this change
(`reached for escape hatch 'run_command' — candidate for a first-class tool`) stay open
until you resolve them. From the **Friction** view, resolve that row. Or with the API:

```bash
curl -X POST http://localhost:7870/api/plugins/friction/resolve \
  -H "Authorization: Bearer $OPERATOR_TOKEN" -H 'Content-Type: application/json' \
  -d '{"summary": "reached for escape hatch '"'"'run_command'"'"' — candidate for a first-class tool", "kind": "harness", "reason": "pre-classifier noise"}'
```

Or from chat, `/friction reached for escape hatch 'run_command'` (a substring match; it
lists what it resolved). Resolved rows can be reopened from the view, and nothing is
deleted.
