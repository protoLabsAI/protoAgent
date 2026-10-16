---
name: triaging-fleet-friction
description: >-
  Triage friction across every agent in the fleet: pull all the ledgers with
  `protoagent fleet friction`, cluster the signals that more than one agent hit,
  check what is already filed, draft a filing plan, and — once a fix has shipped —
  resolve the signal on the agents that recorded it. Read this when asked to
  triage, review or clear the friction backlog across the fleet, or to find what
  is costing the most agents the most time.
---

# Triaging fleet friction

Each agent records its own rough edges into its own ledger. One agent's ledger is a
list of complaints; **all of them read together are a map of what the harness gets
wrong.** This skill is the pass that reads the map.

It needs a running hub and a shell. `protoagent fleet friction` reads each member's
ledger through the hub, so it reaches local and remote members alike.

## 1. Pull everything

```bash
protoagent fleet friction --json --since 14d            # open friction, every member
protoagent fleet friction --json --kind harness -v      # harness only, with detail
```

The JSON has `results` (one row per agent, each with `items`), `shared` (signals more
than one agent hit), and `skipped` (stopped or unreachable members). Read `skipped`
and the per-agent `ok`/`error` first: **an agent you could not read is not an agent
with no friction.** Say so in the report.

Each item carries an `id` (8 hex chars, stable across agents for the same
kind+summary), `severity`, `count`, `first_seen`/`last_seen`, `tool` and `detail`.

## 2. Cluster before you rank

Start from `shared`. A signal several agents hit is a harness defect, not one agent's
quirk, and fixing it once clears it everywhere. Then sweep the per-agent rows for
**near-duplicates the id cannot catch** — the same cause worded three ways ("read_file
truncated", "file cut off at 50k", "large file incomplete"). Group those by hand and
say which ids you merged.

Rank groups by: blocked-someone (`major`) > agents affected > total count > recency.
A `minor ×131` from one agent is usually noise (see the plugin README on escape-hatch
classification); a `major ×2` across four agents is not.

Separate by `kind`:

- `harness` → a tool, error message or limit to change. These become issues.
- `model` → a labelled trace. Do not file these as bugs; note the pattern if it repeats.

## 3. Check what already exists

Before drafting anything, search for a filed issue — duplicates are the most common
failure of a triage pass:

```bash
gh issue list --repo protoLabsAI/protoAgent --state all --search "<distinctive words>" --limit 10
```

Use the `tool` name and a distinctive phrase from the summary. If an issue exists and is
open, link it and add the new evidence (agents, counts) rather than drafting a new one.
If it is **closed**, say whether the friction was recorded *after* the fix shipped — that
means the fix did not hold, which matters more than a new report.

## 4. Draft the filing plan — do not file

Produce, per group, in this shape:

```
### <one-line claim about the system>
ids: 3f9a21c0, 71be09d4   agents: alpha ×4, beta ×3   severity: major   kind: harness
Evidence: <the tool, the exact error, the reproduction from `detail`>
Wanted: <what would have helped — the sentence agents put in `detail`>
Existing: #1234 (open) | none found | #987 (closed, recurred after v0.180.0)
Proposed: new issue | comment on #1234 | no action (noise) | resolve (already fixed)
```

Target repo: harness friction is about the **agent runtime**, so it belongs in
`protoLabsAI/protoAgent` (or the plugin's own repo when `tool` names a plugin tool) —
never the repo an agent happened to be working in.

**Stop here and hand the plan to the operator.** Filing issues is outward-facing; do it
only when told to, and only the ones approved.

## 5. Resolve once it is fixed — not before

Resolving tells every agent's `<working_state>` to stop reporting the item. Do it only
when the fix has **shipped** (released, not just merged — `git tag --contains`), or the
signal was judged noise. Name the agents; there is no default scope:

```bash
protoagent fleet friction resolve 3f9a21c0 --agent alpha --agent beta --reason "fixed in #4321, v0.186.0"
protoagent fleet friction resolve 3f9a21c0 --all-agents --reason "…"   # every agent that has it
protoagent fleet friction resolve 3f9a21c0 --agent alpha --reopen      # undo
```

Nothing is deleted — resolving stamps `resolved_at` and the reason, and reopening clears
it. Always give a `--reason` that names the PR or release, so the next reader can audit it.

Resolve **one id at a time** from the approved plan. A bulk sweep whose scope you cannot
list is how you resolve six signals meaning to resolve one.

## Report

End with: agents read / agents not read, signals found, groups after clustering, how many
are filed already, what you drafted, and what you resolved (ids + agents). Keep the
drafts in the report, not in issues.
