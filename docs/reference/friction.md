# Friction capture, projection, and configuration

For reviewing and resolving reports in the app, use [Friction log](/guides/friction-log). The older-version cleanup below is migration guidance, not a setup requirement.

## How it affects the agent

Open friction is projected into the agent's `<working_state>` block every turn, under
**OPEN FRICTION** ([ADR 0079](/adr/0079-autonomous-operating-model)). The agent sees its
own backlog without having to call a tool for it, and it stops re-reporting the same
rough edge for weeks.

Not everything is projected. A group is carried only if it is:

- **`major`**, or
- **`minor` and repeated** at least `working_state_repeat_threshold` times (default 3).

The eligible groups are ranked worst first, using the same order as `/friction`, and at
most `working_state_limit` lines (default 3) are shown. An agent with no major or
repeated friction adds **nothing** to the block. Each line looks like this:

```
OPEN FRICTION
- [major] tool 'search_files' raised TimeoutError — tool: search_files
- [minor x10+] used `cat` via run_command — read_file does this — use read_file
```

The end of each line tells the agent what to do next:

- `use <tool>` for a shell command that duplicated a first-class tool,
- `tool: <name>` for a tool error,
- `resolve_friction id=<id> when fixed` for a row the agent recorded itself.

**Counts are bucketed.** Counts up to 9 are exact. Above that they are shown as `x10+`,
`x100+` or `x1000+`. The line is re-rendered into the prompt every turn, and an exact
count that went `x131`, then `x132`, changed the prompt text (and invalidated its cache)
without telling the agent anything new. The ledger, the view and `/friction` still show
exact counts.

### The knobs

Set `working_state`, `working_state_limit`, and `working_state_repeat_threshold`
in the [configuration reference below](/reference/friction#configuration-reference). Changes apply
on the next turn without a restart.

### When to turn projection off

Set `working_state: false` when the projected lines are steering the agent wrong. The
usual case is a backlog full of stale or noisy rows that you haven't cleaned up yet (see
the [worked example](/reference/friction#worked-example-a-coding-agent-drowning-in-escape-hatch-noise)).
Clean up the backlog, then turn it back on. Recording, the view and `/friction` all keep
working while it's off. Only the agent's per-turn view goes away.

If the lines are useful but crowd the block, lower `working_state_limit`. If one-off
minor friction keeps appearing, raise `working_state_repeat_threshold`.

## What auto-capture logs

**Tool errors.** A tool that raises is logged as `major` `harness` friction. The summary
includes the exception type, for example `tool 'task' raised TimeoutError`, so different
failures of the same tool stay in separate rows. Control flow is not friction: an
approval pause, an interrupt, a delegation or a cancellation is never logged.

**Shell commands that duplicate a tool the agent has.** A call to a shell tool
(`run_command`, `execute_command`, `shell`, `bash`) is logged only when the command's
first word maps to a first-class tool, **and** that tool is bound for this agent:

| Command | First-class tool |
|---|---|
| `cat` / `echo` / `printf` with a `>` or `>>` redirect into a file | `write_file` |
| `cat`, `head`, `tail`, `less`, `more` | `read_file` |
| `grep`, `egrep`, `fgrep`, `rg`, `ag`, `ack` | `search_files` |
| `ls`, `tree` | `list_dir` |
| `find`, `fd` | `find_files` |
| `sed -i …`, `awk -i inplace …` | `edit_file` |
| `tee` | `write_file` |

The entry names the fix, for example *"used `cat` via run_command — read_file does
this"*. It is logged as `minor`.

Before reading the first word, the classifier strips the parts of the line that aren't
the command: `cd X &&` or `cd X;`, `VAR=value`, `sudo`, `env …`, `time`,
`mise exec … --`, and `sh -c '…'`. For a pipeline, only the first segment counts, so
`cat f | grep x` is a read and `git diff | grep x` is git. A stderr redirect such as
`2>/dev/null` doesn't turn a read into a write.

If the agent doesn't have the tool, the command isn't friction. An agent with no
`list_dir` that runs `ls` is using the only tool it has.

**What is never friction:** git, test runners, type checkers, builds, package
managers, project scripts. None of these has a first-class equivalent, so a shell tool
is the right tool for them.

`python` and `exec` carry code rather than a shell line, so they have no first word to
classify. They still log the older, coarser *"reached for escape hatch 'python' —
candidate for a first-class tool"* entry.

### Silencing a tool entirely

`escape_hatch_exempt` lists tool names whose calls are never logged as escape-hatch
friction:

```yaml
friction:
  escape_hatch_exempt: [run_command]
```

Use it for an agent whose shell tool **is** the job, and where even a `cat` through the
shell isn't worth a row. Tool **errors** are still captured for exempt tools.

## Clear noise from older versions {#worked-example-a-coding-agent-drowning-in-escape-hatch-noise}

Before v0.180.0, auto-capture counted every shell call as friction and could infer
an issue repository from a managed project. Current versions only classify shell
commands that duplicate a bound tool, and require an explicit `issue_repo`.
Old ledger entries remain until resolved.

**Cleaning up after the upgrade.** The upgrade doesn't rewrite your ledger. Rows
recorded under the old rule stay open until you resolve them, and while they're open
they are still projected into the agent's working state. Clear them in any of these
ways:

- **In the view:** filter **Source ▸ Auto**, find the
  `reached for escape hatch 'run_command'` row, and click **Resolve**.
- **From chat:** `/friction reached for escape hatch 'run_command'`. This is a
  substring match, and it lists what it resolved.
- **Over the API.** The body must match the summary exactly, including the em dash, so
  send it from a quoted heredoc rather than escaping quotes by hand:

  ```bash
  curl -s -X POST http://localhost:7870/api/plugins/friction/resolve \
    -H "Authorization: Bearer $OPERATOR_TOKEN" \
    -H 'Content-Type: application/json' \
    -d @- <<'JSON'
  {"summary": "reached for escape hatch 'run_command' — candidate for a first-class tool",
   "kind": "harness",
   "reason": "pre-classifier noise: git/tests/builds, not friction"}
  JSON
  ```

  The response is `{"changed": <entries stamped>, "resolved": true, …}`. Send
  `"resolved": false` to reopen. For a [fleet](/guides/fleet) member, use that
  member's port and token.

Then, for that agent:

1. Pin `friction.issue_repo` to the runtime repo (for example `protoLabsAI/protoAgent`,
   or your fork).
2. If you turned `working_state` off while the noise was live, turn it back on. The
   classifier now keeps shell noise out of the ledger.
3. Optionally, set `escape_hatch_exempt: [run_command]` if you don't want even the
   `cat`/`ls`/`grep` duplicates recorded for this agent.

## Configuration reference

All keys live under `friction:` in the agent's `langgraph-config.yaml`. They are also
editable in **Settings ▸ Plugins ▸ Friction Log**. Every key is read live, so no restart
is needed.

```yaml
friction:
  working_state: true
  working_state_limit: 3
  working_state_repeat_threshold: 3
  issue_repo: ""                 # owner/name, e.g. protoLabsAI/protoAgent
  escape_hatch_exempt: []        # e.g. [run_command]
```

| Key | Default | Settings label | Meaning |
|---|---|---|---|
| `working_state` | `true` | Show open friction in the agent's working state | Project open friction into `<working_state>` |
| `working_state_limit` | `3` | Max friction lines in working state | At most N lines per turn |
| `working_state_repeat_threshold` | `3` | Repeats before a minor friction is carried | Repeats before a `minor` group is carried. `major` always is |
| `issue_repo` | `""` | File friction against this repo | `owner/name` for **File issue** links. A `https://github.com/` prefix is stripped. Empty means **Copy as issue** |
| `escape_hatch_exempt` | `[]` | Never count calls to these tools as escape-hatch friction | Tool names whose calls are never escape-hatch friction |

To turn the plugin off for one agent, use `plugins: { disabled: [friction] }`.

### The ledger

- **Location:** `<instance root>/friction/friction.jsonl`, or the path in the
  `FRICTION_LOG` environment variable if it is set. `protoagent config explain` prints
  the instance root. On a default install it is `~/.protoagent/default`, and on the
  desktop app it is the agent's workspace directory. A ledger from before instance
  scoping (`~/.protoagent/friction/friction.jsonl`) is moved into place the first time
  it is read.
- **Format:** one JSON object per line: `ts`, `kind`, `summary`, `detail`, `severity`,
  `source`, plus `tool` and `suggest` on auto-captured rows, and `resolved_at` /
  `resolved_reason` once resolved.
- **Cap:** append-only, trimmed to the newest **2000** entries.
- **API:** `GET /api/plugins/friction/` (grouped by default; takes `kind`, `grouped`,
  `limit` and `resolved`), `GET /api/plugins/friction/fleet`, and
  `POST /api/plugins/friction/resolve`. All three are bearer-gated. The older
  `/api/friction` paths serve the same handlers.
- **Events:** `friction.recorded`, `friction.resolved` and `friction.reopened` are
  published on the plugin event bus ([ADR 0039](/adr/0039-plugin-event-bus)), so another
  plugin can react without importing this one.

