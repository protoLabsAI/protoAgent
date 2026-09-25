# Friction log

The friction log is the agent's own record of what got in its way: a tool that was
missing or awkward, an error message that pointed at the wrong cause, a shell command it
ran because it didn't use the tool it already had. Each entry lands in one append-only
ledger per agent, and you triage it from the console.

It is the `friction` plugin. It ships with protoAgent and is **on by default**.

## What goes in it

Every entry is one of two kinds:

| Kind | Meaning | What you do with it |
|---|---|---|
| `harness` | The tooling should change: a missing tool, an awkward one, a misleading error, a cap that bit | File it against the runtime (or your fork) and fix the tool |
| `model` | The agent should have known better: a wrong path it recognized, a retry it caused | Treat it as a labeled trace. It is evidence for prompt, skill or model changes |

Entries come from two places:

- **The agent.** It calls `record_friction` when it hits a rough edge. The bundled
  `recording-friction` skill tells it when to record, which kind to use, and how to write
  a summary that someone can act on.
- **Auto-capture.** Middleware logs tool errors and some shell commands without the
  agent's help. See [What auto-capture logs](#what-auto-capture-logs).

Each entry has a `severity`: `major` (it blocked the agent or produced a wrong result)
or `minor` (it cost time). It also has a `source`: `agent` or `auto`.

Taken together, the ledger is a backlog of improvements that comes from real use. Nobody
has to write it up by hand.

### Turn it off for one agent

Add `friction` to that agent's disabled list in `langgraph-config.yaml`:

```yaml
plugins:
  disabled: [friction]
```

You can also use the **Enable / Disable** toggle in **Settings ▸ Plugins ▸ Installed**.
The Friction view is a console surface, so removing it needs a restart; the toggle says
so. If you only want the agent to stop *seeing* its backlog each turn, and still want it
to record, leave the plugin on and set `friction.working_state: false` (see
[How it affects the agent](#how-it-affects-the-agent)).

## Where you see it

### The Friction rail view

Open **Friction** from the console rail (the warning-triangle icon), or search for it
with the command palette. The palette entry opens the view with the search box focused.

The header shows a tally: how many **signals** (grouped rows) are listed, and how many
**occurrences** the ledger holds across them. **Refresh** re-reads the ledger.

The filter bar, left to right:

| Control | What it does |
|---|---|
| Search | Matches the summary, the detail, and the tool name |
| **This agent / Fleet** | Scope. Only shown on a hub that has fleet members. See [Fleet rollup](#the-fleet-rollup-on-a-hub) |
| **All / Harness / Model** | Channel filter |
| **Any / Major** | Severity filter |
| **Any / Agent / Auto** | Source filter. **Auto** isolates what the middleware captured |
| Sort | **Most recent** (default), **Most frequent**, or **Most severe** |
| **Show resolved** | Include rows that have already been resolved. They are dimmed and struck through, with a `resolved` badge |

Each row is one **group**: all the entries with the same kind and summary. The row
shows:

- the summary (click it to expand the detail),
- `×N`, the number of occurrences, when there is more than one,
- badges for severity, kind and source, and the tool name for auto-captured rows,
- how long ago it happened. For a repeat this is `first seen → last seen`; hover over it
  for the exact timestamps.

A group takes the **worst** severity among its entries. One `major` occurrence makes the
whole row major.

Expanding a row shows the detail. For auto-captured rows this is the captured tool
payload, pretty-printed as JSON, with a **Copy** button. The ledger caps a detail at 600
characters and a summary at 200 when the entry is written. The view tells you when a
detail was cut off.

Each row has two actions: **File issue** (or **Copy as issue**) and **Resolve** (or
**Reopen**). Both are covered in [Triage](#triage).

### `/friction` in chat

`/friction` is an operator chat command. It is handled by the server, costs no model
turn, and is **not** an agent tool, so the agent can't use it to clear its own backlog.

- **`/friction`** prints a summary: how many open signals, how many occurrences, how many
  are major. Then it lists the top 10, worst first (severity, then count, then
  recency), with exact counts.
- **`/friction <text>`** resolves every open entry whose summary **contains** `<text>`.
  It reports how many entries it resolved and names each signal. It is a substring
  match, so check the list it prints. Any row it resolved can be reopened from the
  view.

### The fleet rollup on a hub

On a [fleet](/guides/fleet) hub with members, the view shows a **This agent / Fleet**
toggle. **Fleet** asks each member for its grouped backlog and merges rows that have the
same kind and summary across agents:

- the count is summed across members,
- a chip shows which agent hit it, or `N agents`. Hover over the chip for the
  per-agent counts. One agent seeing something 12 times is a different problem from
  four agents seeing it three times each,
- the row takes the worst severity any member reported,
- the tally adds the number of agents reached, plus `(N unreachable)` if some members
  didn't answer. Hover over it to see which.

The fleet scope is **read-only**. **Resolve** is hidden and **Show resolved** is
disabled, because there is no single ledger to stamp from the hub. To resolve a row,
open that member's own Friction view. The rollup is cached for 30 seconds, and it only
lists open friction.

## Triage

A workable pass through the backlog:

1. Sort by **Most severe** and filter to **Harness**. The top of the list is what's
   blocking the agent.
2. Expand each row and read the detail. For an auto-captured row the payload shows
   exactly what the agent called.
3. For each row, decide on one of three things. **File it** if it needs a tracked fix.
   **Resolve it** if it's already fixed, or if it's noise. **Leave it open** if it's
   real but you aren't acting on it yet.

### Resolve and reopen

**Resolve** stamps `resolved_at` on every entry in that group, **in the ledger itself**.
It is the same stamp the agent's `resolve_friction` tool writes. The row drops out of
the view, out of `/friction`, out of the agent's `friction_review`, and out of its
working state. **Reopen** clears the stamp and the row is backlog again.

The ledger is the only record of what's done. Your browser keeps no separate
"dismissed" state, so the agent and the console always read the same backlog. Nothing
is deleted: a resolved entry stays in the ledger with its timestamp, and it can be
reopened.

The view's **Resolve** button affects only the row you clicked. It matches the full
summary and the kind exactly. `/friction <text>` and the agent's `resolve_friction`
match a substring, so they can clear several rows at once.

Only resolve a row when the rough edge is actually fixed, or when you've decided it
isn't one. A live problem marked resolved is worse than an unrecorded one, because
nobody is looking at it anymore.

### File a row as a GitHub issue

The ledger already holds what an issue needs: the summary, how often it happened, over
what period, and the payload. The row's action button turns that into an issue:

- With **`friction.issue_repo` set** (for example `protoLabsAI/protoAgent`, or your
  fork), the button is **File issue**. It opens a new-issue page on that repo with the
  title and body already filled in. You review it and submit it yourself.
- With **`issue_repo` empty** (the default), the button is **Copy as issue**. It copies
  the same content as markdown to your clipboard, and you paste it wherever the fix
  belongs. The same fallback applies when an entry is too long to fit in a prefilled
  URL.

Pin `issue_repo` to the repo that owns the **runtime**: upstream protoAgent, or your
fork of it. Set it in **Settings ▸ Plugins ▸ Friction Log**.

`issue_repo` is **not** taken from the project the agent is working in. Harness friction
is about the agent's own tools and framework, not about the repo it happens to be
editing. Before v0.180.0 the plugin guessed the repo from the agent's first managed
project. For a coding agent onboarded onto someone else's repo (a client's project, or a
private repo from a coding interview), that pointed the file-an-issue link at the wrong
tracker. Now it is either the repo you pinned, or the clipboard.

For filing an issue from chat with the repo's issue-gate sections, see
[File GitHub issues (`/issue`)](/guides/file-github-issues).

### Get a filing plan from `friction_triage`

For a long backlog, have the agent delegate to its `friction_triage` subagent. For
example, ask: *"Use friction_triage to go through the friction backlog and propose what
to file."* The subagent reads both channels and then:

- groups entries that share a root cause, even when their summaries differ,
- ranks the groups by how often they recur and how badly they blocked,
- drafts an issue title and body for each group worth tracking, including what would
  have helped,
- names the entries that aren't worth filing, and says why.

It is **read-only**: its only tool is `friction_review`. It can't resolve or record
anything, so the plan comes back to you, and filing and resolving stay your decision.

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
- `resolve_friction when fixed` for a row the agent recorded itself.

**Counts are bucketed.** Counts up to 9 are exact. Above that they are shown as `x10+`,
`x100+` or `x1000+`. The line is re-rendered into the prompt every turn, and an exact
count that went `x131`, then `x132`, changed the prompt text (and invalidated its cache)
without telling the agent anything new. The ledger, the view and `/friction` still show
exact counts.

### The knobs

| Key | Default | Effect |
|---|---|---|
| `working_state` | `true` | Project open friction into `<working_state>` at all |
| `working_state_limit` | `3` | At most this many friction lines per turn. The block is shared with the agent's tasks and other work |
| `working_state_repeat_threshold` | `3` | How many times a `minor` group must repeat before it is carried. `major` is always carried |

These are read live, so a change applies on the next turn with no restart.

### When to turn projection off

Set `working_state: false` when the projected lines are steering the agent wrong. The
usual case is a backlog full of stale or noisy rows that you haven't cleaned up yet (see
the [worked example](#worked-example-a-coding-agent-drowning-in-escape-hatch-noise)).
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

## Worked example: a coding agent drowning in escape-hatch noise

**What happened.** A hands-on coding agent (clone a repo, run the tests, find the bug,
commit) used `run_command` for most of its work. Before v0.180.0, auto-capture logged
*every* shell call as friction. Its ledger ended up with **131** auto entries, all with
the same summary:

```
reached for escape hatch 'run_command' — candidate for a first-class tool
```

**127** of them were git, vitest, `tsc`, npm and `mise`: normal work with no
first-class equivalent. Only **4** (`ls`, `cat`, `grep`) duplicated a tool the agent
had. Because the group repeated past the threshold, the working state told the agent
`minor x131: reached for escape hatch 'run_command'` on every turn. That pushed it away
from the tool it needed most.

The same agent had a second problem. `issue_repo` was empty, so the plugin derived it
from the agent's first managed project, which was the private repo the agent was
working in. Its **File issue** links pointed at someone else's tracker.

**What changed in v0.180.0.**

- Shell commands are logged only when they duplicate a bound tool, using the table
  above. Git, tests and builds are no longer logged.
- The entry names the tool to use, and the working-state hint says `use <tool>`.
- Working-state counts are bucketed (`x100+`), so the line stops changing every turn.
- `escape_hatch_exempt` was added.
- `issue_repo` is never derived from a project. It is either pinned, or empty and
  copy-to-clipboard.

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

## Related

- [`plugins/friction/README.md`](https://github.com/protoLabsAI/protoAgent/blob/main/plugins/friction/README.md): the plugin's own summary
- [Starter tools ▸ plugins](/reference/starter-tools): every first-party plugin and its default
- [Build out your agent with a coding agent](/guides/build-with-a-coding-agent): where friction fits in the build loop
- [Plugins ▸ Config, secrets & settings](/guides/plugins#config-secrets-settings): how a plugin's `friction:` section and Settings group work
