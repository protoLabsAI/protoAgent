# Friction log

The friction log is the agent's own record of what got in its way: a tool that was
missing or awkward, an error message that pointed at the wrong cause, a shell command it
ran because it didn't use the tool it already had. Each entry lands in one append-only
ledger per agent, and you triage it from the console.

It is the `friction` plugin. It ships with protoAgent and is **on by default**.

## Triage

Open **Friction** from the console rail, then work through the backlog:

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
match a substring (ignoring case, extra whitespace and curly-vs-straight quotes), so
they can clear several rows at once. The agent can also resolve one row exactly by its
id: `friction_review` prints a short `[id]` per row, and `resolve_friction(id=...)`
takes it. When nothing matches, the tool lists the open rows' ids and summaries; when
the match was already resolved, it says so instead of reporting no match.

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

`issue_repo` is not inferred from a managed project. Set it to the repository that
owns the runtime; leave it empty to copy an issue draft to the clipboard.

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
Disabling removes the plugin's tools and view; restart only if the app requests it.
If you only want the agent to stop *seeing* its backlog each turn, and still want it
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
turn, and is **not** an agent tool. The agent has a separate `resolve_friction` tool.

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

### Across the fleet, from the terminal

Use [Fleet friction triage](/guides/operating-a-fleet#fleet-friction-triage) for per-agent CLI inspection and resolution.

## How it affects the agent

<a id="the-knobs"></a>
<a id="when-to-turn-projection-off"></a>

See [Friction reference](/reference/friction) for capture, projection, configuration, and older-version cleanup.

## What auto-capture logs

<a id="silencing-a-tool-entirely"></a>

See [Friction reference](/reference/friction) for capture, projection, configuration, and older-version cleanup.

## Clear noise from older versions {#worked-example-a-coding-agent-drowning-in-escape-hatch-noise}

See [Friction reference](/reference/friction) for capture, projection, configuration, and older-version cleanup.

## Configuration reference

<a id="the-ledger"></a>

See [Friction reference](/reference/friction) for capture, projection, configuration, and older-version cleanup.

## Related

- [`plugins/friction/README.md`](https://github.com/protoLabsAI/protoAgent/blob/main/plugins/friction/README.md): the plugin's own summary
- [Starter tools ▸ plugins](/reference/starter-tools): every first-party plugin and its default
- [Build out your agent with a coding agent](/guides/build-with-a-coding-agent): where friction fits in the build loop
- [Plugins ▸ Config, secrets & settings](/guides/plugins#config-secrets-settings): how a plugin's `friction:` section and Settings group work
