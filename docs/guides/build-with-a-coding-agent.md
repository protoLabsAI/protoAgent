# Build out your protoAgent with a coding agent

**What you'll do:** stand up a project-manager (PM) agent that owns your repo's
board, wire a CLI coding agent under it, and ship your first feature through the
pipeline — brief → board card → disposable worktree → pull request → gates →
merge — without holding the keyboard yourself.

Every command and config key below ships today; nothing is aspirational. If a
coding agent CLI is already installed, the first PR is about thirty minutes away.

## Before you start

- A running protoAgent fork ([fork the template](/guides/fork-the-template)) and
  a repository you want it to grow — usually that same fork.
- A CLI coding agent installed and signed in: `proto`, Claude Code, opencode, or
  Codex. The [coding agents guide](/guides/coding-agents) lists the ACP adapters
  for each.
- `git` and `gh` authenticated for the target repo — the loop pushes branches and
  opens PRs with them.
- CI on the target repo that can genuinely fail. The pipeline's safety is its
  gates (step 5); a repo with no failing checks has no gates.

The shape you're building:

```
you (operator)
 └─ project-manager agent      reads the repo, curates briefs — file tools read-only
     └─ project board           features: spec, acceptance criteria, files, state
         └─ coder delegates     ACP coding agents, one disposable worktree each
             └─ pull requests   the gates decide the merge, not anyone's confidence
```

You describe outcomes. The PM turns them into board features. The board's loop
dispatches each feature to a coding agent in a disposable git worktree, opens the
PR, and walks it through review. Code reaches the repo through exactly one door.

## 1. Stand up the PM

Pick **Project Manager** in the new-agent picker (under the *Advanced* toggle),
or create the workspace from the CLI — the same flow as any
[fleet](/guides/fleet) agent:

```bash
python -m server workspace new pm --bundle https://github.com/protoLabsAI/project-manager-archetype
```

Either path assembles the same three pieces: the catalog row in
`config/archetype-catalog.json`, the soul preset
`config/soul-presets/project-manager.md`, and the
[project-manager-archetype](https://github.com/protoLabsAI/project-manager-archetype)
bundle — the plugin set that includes the project board.

Then point the new workspace at **one repository** in its
`langgraph-config.yaml`:

```yaml
project_board:
  coder: proto               # the acp delegate the loop dispatches (next step)
  repo: ~/dev/my-agent       # the fork this PM owns
  base_branch: main
  loop_enabled: true         # the background puller: ready → worktree → coder → PR
  max_concurrent: 1          # raise once the repo parallelizes cleanly
  local_gate_cmd: "auto"     # the pre-PR gate — see step 5
```

One PM, one repo. An agent that sits above several PMs is the Portfolio Manager —
a different archetype, over A2A ([portfolio](/guides/portfolio)).

> **Why the PM can't edit files.** Its file tools are read-only by design — a PM
> that can edit files can ship unreviewed changes around every gate below, so the
> archetype removes the capability instead of asking the model to resist it. The
> preset also names the tell: when the PM wants to edit a file, the feature was
> under-specified. Fix the brief, not the file.

## 2. Wire a coder

Each coder is an **`acp` delegate** — a CLI coding agent driven over the Agent
Client Protocol ([ADR 0024](/adr/0024-spawn-cli-coding-agents-acp)) through the
delegate registry ([ADR 0025](/adr/0025-unified-delegate-registry-and-panel)).
Declare one per binary you want available:

```yaml
delegates:
  - { name: proto,       type: acp, command: proto,       args: ["--acp"], workdir: ~/dev/my-agent, permissions: allowlist }
  - { name: claude-code, type: acp, command: claude-code, args: [],        workdir: ~/dev/my-agent, permissions: allowlist }
  - { name: opencode,    type: acp, command: opencode,    args: ["acp"],   workdir: ~/dev/my-agent, permissions: allowlist }
```

Before going further, open the delegates panel and hit **Test** on the delegate
you named in `project_board.coder` — it must probe green. The
[coding agents guide](/guides/coding-agents) covers what this guide won't repeat:
adapters for agents without native ACP (Claude Code via `claude-agent-acp`, Codex
via `codex-acp`), permission postures, and containerized setups.

One board-specific rule to internalize now: **the loop owns git.** Per feature it
cuts a disposable worktree off `origin/<base_branch>`, scopes the dispatch to it,
then commits, pushes, and opens the PR itself. The coder is told to edit files
and run tests — nothing else. (For coders you drive *directly* with
`delegate_to`, outside the board, `manage_git: true` gives you the same
framework-owned lifecycle —
[Managed git](/guides/coding-agents#managed-git-the-framework-owns-branchcommitpushpr-adr-0076).)

## 3. Ship your first feature

This is the whole loop, end to end, with what you should see at each step.

**Tell the PM what you want — in chat, as an outcome.** Concrete beats clever:

> In the console repo, the Settings ▸ About panel should show the running server
> version. Done means: the version renders in About, comes from the existing
> `/api/version` endpoint, and a unit test covers the component. Files I'd
> expect: the About panel component and its test.

**The PM curates it into a feature.** It reads the repo (read-only file tools),
writes the brief, and creates the card — `board_create_feature`, then
`board_mark_ready` once the brief holds a spec, acceptance criteria, and a
`files_to_modify` list. The **Ready gate refuses** a feature missing any of
those: a feature that can't state what done means doesn't get a coder. On the
board view you'll see the card land in **backlog** and move to **ready**.

**The loop takes it from there.** Within a poll cycle the card moves to
**in_progress**: the loop has cut a worktree off `origin/main`, dispatched your
coder into it with the brief, and is waiting. When the coder finishes, the loop
runs `local_gate_cmd` in the worktree — most failures die here, before a PR
exists — then commits, pushes, and opens the PR. The card moves to **review**
with the PR link on it.

**The gates decide.** CI runs; if you enabled the review gate (step 5) the
adversarial review runs as a blocking check. When everything is green and the PR
merges, the merge webhook (or `merge_poll` where GitHub can't reach you) moves
the card to **done** and reaps the worktree.

That's one full pass: you spoke an outcome, and a reviewed, gated PR merged. Ask
the PM to `board_list` any time, or watch the board view — the card's state *is*
the pipeline's state.

> **Why you brief the PM instead of pasting an issue.** An issue body is input to
> triage, never a dispatch prompt. The coder sees nothing but the brief (no
> thread, no linked context); issues describe symptoms while acceptance criteria
> make "done" checkable; and issue text is untrusted — anyone can write it, and
> text that reaches a coder's prompt is an instruction channel. The PM quotes it
> as evidence inside the brief and records it as `source_issue` (which becomes
> the PR's `Fixes #N` line, so traceability survives the rewrite).

## 4. When it goes red or blocked

The loop has reflexes. The discipline is not preempting them:

- **CI bounce** — a failing check requeues the feature with the CI error *and the
  prior attempt's diff* in the retry prompt, so the re-dispatch improves on the
  last try instead of repeating it (`ci_fix_max` rounds, then blocked).
- **Review requeue** — adverse review findings route back to the *same branch*
  the same way, bounded by `review_fix_max`.
- **Auto-rebase** — a sibling merge that leaves an open PR behind base gets a
  clean rebase + force-push with no coder at all; a real conflict re-dispatches
  the coder, bounded by `rebase_fix_max`.
- **Merge reconcile** — merged → done, closed-unmerged → blocked, worktree
  reaped.

Your cue is the **blocked** flag — never a red check. A red PR is not your cue to
push a commit, and it is not the PM's cue to open a new card (the preset's rule:
*a fix to an open PR is a fix round, never a new feature*). Blocked means every
bounded budget is spent and the loop is explicitly asking for a human decision: a
real bug to triage, a conflict to adjudicate, a brief to rewrite.

## 5. Harden the gates

The loop's autonomy is downstream of its gates. A board with weak CI and advisory
review does not ship less — it ships unreviewed work faster. What ships today:

- **The repo's own CI** — for this template: ruff, import contracts, the Python
  suite, a live A2A smoke, console unit tests, Playwright e2e
  (`.github/workflows/checks.yml`). The one to copy is the least glamorous: the
  **changelog gate** fails any PR without a changelog entry. Cheap, dumb, catches
  a real omission on nearly every PR — that ratio is the whole argument for
  mechanical gates.
- **The pre-PR local gate** (`local_gate_cmd`) — runs in the coder's worktree
  before a PR opens. `"auto"` discovers the repo's declared `gate` target instead
  of hand-transcribing one that rots.
- **The blocking review gate** (`review_gate: true`) — after each PR opens, the
  host's `code-review` workflow
  ([ADR 0077](/adr/0077-adversarial-code-review-workflow)) runs as a blocking
  check. Blocking findings bounce the feature back to the coder; exhaustion
  blocks it. Never a silent merge.
- **Bounded rounds everywhere** — `ci_fix_max`, `review_fix_max`,
  `rebase_fix_max`. Every loop is bounded rounds, then blocked for a human. No
  path retries forever.
- **An external review edge** —
  `POST /plugins/project_board/features/{fid}/review` lets a host that reviews
  PRs with its own fleet drive the same fix rounds from outside the loop.

The honest half: **a gate only binds if it can fail.** Two real runs from this
pipeline's history, one sentence each. A coder reported "verified by acceptance
tests" three times while producing zero commits — the acceptance command passed
on an unmodified tree, so the verification could not fail and certified nothing.
A fix round rewrote a function and its tests together, and the suite went from 5
passing to 9 passing while covering strictly less than before. When you add a
check, prove it fails on the broken case before you trust it on the working one.

## 6. Grow it

**Escalation tiers.** Features ask for a tier, not a vendor. The optional
`coders` map is a ladder (needs ≥2 distinct delegates):

```yaml
project_board:
  coder: proto               # the default when no ladder is configured
  coders:
    smart: proto
    reasoning: claude-code
    opus: heavy-coder
```

A feature declares a *difficulty*; difficulty picks the starting tier (`small` →
`smart`, `medium`/`large` → `reasoning`, `architectural` → `opus`), and a
capability failure — the coder errored, or produced no diff — climbs the ladder
before blocking at the top. Briefs never name a vendor, so swapping which agent
backs a tier is a one-line config edit no feature notices.

**File every friction.** When a run exposes a defect in the *pipeline* (not the
target repo), it becomes an issue on the pipeline's own repo and ships through
the same board — real examples:
[zombie dispatch](https://github.com/protoLabsAI/projectBoard-plugin/issues/105),
[half-applied cancel](https://github.com/protoLabsAI/projectBoard-plugin/issues/106),
[the retro's own asks](https://github.com/protoLabsAI/projectBoard-plugin/issues/107).
Two mechanisms keep findings from evaporating: the `friction` plugin's
`record_friction` tool (enable with `plugins: { enabled: [friction] }`) logs the
moment-of-pain signal, and the board's read-only `board_retro` tool mines the
attempt/outcome history into recurring failure classes, which the `loop-retro`
skill distills into durable grounding. A friction point that isn't filed is a
lesson the next run pays for again.

**Freeze doctrine, at the right layer.** When you're telling the PM the same
thing in every chat, the instruction has outgrown the conversation:

- `edit_soul` ([ADR 0081](/adr/0081-self-authored-persona-edit-soul)) lets the
  agent rewrite its own `SOUL.md` — persona only, never operating doctrine.
  Guarded, default-off:

  ```yaml
  soul:
    self_edit_enabled: true    # binds the edit_soul tool (lead agent only)
    drift:
      enabled: true            # default on
      interval_hours: 24
      threshold: 0.25
  ```

  Every persona write archives the outgoing version (restorable from Settings ▸
  Identity), and the drift pass diffs the live `SOUL.md` against its earliest
  snapshot, publishing `persona.drift_detected` past the threshold. It surfaces;
  it never rewrites.
- Doctrine that survives contact with real runs belongs in the **archetype**:
  commit it into `config/soul-presets/project-manager.md` so every future PM
  starts with it. That's how the preset's fix-round rule and its
  never-claim-an-untooled-action rule landed (#2273).

That's the full loop: briefs go down, PRs come up, gates decide, friction gets
filed, and what the pipeline learns gets frozen where the next run inherits it.

## Related guides

- [Spawn CLI coding agents](/guides/coding-agents) — ACP mechanics, adapters,
  permissions
- [Delegates](/guides/delegates) — the registry and panel
- [Fleet](/guides/fleet) — workspaces and archetypes
- [Fork the template](/guides/fork-the-template) — the checklist that precedes
  all of this
