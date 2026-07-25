# Build out your protoAgent with a coding agent

You [forked the template](/guides/fork-the-template) and have an agent running. Now
you want to grow it — new plugins, new tools, config surgery, docs — and you could
type every change yourself. This guide is the other path: **stop holding the keyboard
and operate a pipeline** — a project-manager agent that reads your repo, a board that
holds the work, and CLI coding agents that write the code and open the PRs.

Every command, config key, and behavior below ships today; nothing here is
aspirational. Autonomy is the *last* step, not the first — it is earned by
[the gates](#the-gates-green-must-be-able-to-fail), so read that section before you
turn the loop loose.

## 1. The shape

```
you (operator)
 └─ project-manager agent      reads the repo, curates briefs — file tools read-only
     └─ project board           features: spec, acceptance criteria, files, state
         └─ coder delegates     ACP coding agents, one disposable worktree each
             └─ pull requests   the gates decide the merge, not anyone's confidence
```

Who holds the keyboard: **the coders, and only the coders.** You describe outcomes.
The PM turns them into board features. The board's loop dispatches each feature to a
coding agent in a disposable git worktree, opens the PR, and walks it through review.
Code reaches the repo through exactly one door.

The PM's file tools are **read-only by design**, and the soul preset
(`config/soul-presets/project-manager.md`) states the reason in the agent's own
voice: *"investigation is mine, mutation belongs to the pipeline."* A PM that can
edit files is an agent that can ship unreviewed changes around every gate below —
so the archetype removes the capability instead of asking the model to resist it.
The preset also names the tell: when the PM catches itself wanting to edit a file,
the feature was under-specified. Fix the brief, not the file.

## 2. Stand up the PM

The `project-manager` archetype is data, not code — three pieces:

- a **catalog row** in `config/archetype-catalog.json` (advanced tier in the
  new-agent picker),
- the **soul preset** `config/soul-presets/project-manager.md` — the identity above,
- the **[project-manager-stack](https://github.com/protoLabsAI/project-manager-stack)
  bundle** — the plugin set the row installs, including the project board.

Pick **Project Manager** in the new-agent picker (it's under the *Advanced* toggle),
or create the workspace from the CLI — the same flow as any
[fleet](/guides/fleet) agent:

```bash
python -m server workspace new pm --bundle https://github.com/protoLabsAI/project-manager-stack
```

Then point it at **one repository** in the workspace's `langgraph-config.yaml`:

```yaml
project_board:
  coder: proto               # the acp delegate the loop dispatches (next section)
  repo: ~/dev/my-agent       # the fork this PM owns
  base_branch: main
  loop_enabled: true         # the background puller: ready → worktree → coder → PR
  max_concurrent: 1          # raise once the repo parallelizes cleanly
  local_gate_cmd: "auto"     # the pre-PR gate — see "The gates" below
```

One PM, one repo. An agent that sits above several PMs is the Portfolio Manager —
a different archetype, over A2A ([portfolio](/guides/portfolio)).

## 3. Wire the coders

The board dispatches through the delegate registry
([ADR 0025](/adr/0025-unified-delegate-registry-and-panel)); each coder is an
**`acp` delegate** — a CLI coding agent driven over the Agent Client Protocol
([ADR 0024](/adr/0024-spawn-cli-coding-agents-acp)). Declare them in YAML, one per
binary you want available:

```yaml
delegates:
  - { name: proto,       type: acp, command: proto,       args: ["--acp"], workdir: ~/dev/my-agent, permissions: allowlist }
  - { name: claude-code, type: acp, command: claude-code, args: [],        workdir: ~/dev/my-agent, permissions: allowlist }
  - { name: opencode,    type: acp, command: opencode,    args: ["acp"],   workdir: ~/dev/my-agent, permissions: allowlist }
```

The [coding agents guide](/guides/coding-agents) covers the details this guide won't
repeat: the adapters for agents without native ACP (Claude Code via
`claude-agent-acp`, Codex via `codex-acp`), the delegates panel's **Test** probe, the
permission postures, and containerized setups. Two board-specific notes:

- **The loop owns git.** Per feature it cuts a disposable worktree off
  `origin/<base_branch>`, scopes the dispatch to it, then commits, pushes, and opens
  the PR itself — the coder is told to edit files and run tests only. (For coders you
  drive *directly* with `delegate_to`, outside the board, `manage_git: true` gives
  you the same framework-owned lifecycle — see
  [Managed git](/guides/coding-agents#managed-git-the-framework-owns-branchcommitpushpr-adr-0076).)
- **Features ask for a tier, not a vendor.** The optional `coders` map is a ladder
  of tiers to delegate names:

```yaml
project_board:
  coder: proto               # the default when no ladder is configured
  coders:                    # opt-in escalation — needs ≥2 distinct delegates
    smart: proto
    reasoning: claude-code
    opus: heavy-coder
```

A feature declares a *difficulty*; difficulty picks the starting tier (`small` →
`smart`, `medium`/`large` → `reasoning`, `architectural` → `opus`), and a capability
failure — the coder errored, or produced no diff — climbs the ladder before blocking
at the top. Briefs and features never name a vendor, so swapping which agent backs a
tier is a one-line config edit that no feature notices.

## 4. The intake gate

Not everything that arrives becomes work. The operating rule: **only human-triaged
work reaches the agent**, and an issue body is *input to triage*, never a dispatch
prompt. Raw-pasting an issue into the pipeline fails for three concrete reasons:

- **The coder sees nothing but the brief.** It has no access to your conversation,
  the issue thread, or the linked context — a brief that assumes any of that
  produces a confident wrong build.
- **Issues describe symptoms, not done.** A coder handed a symptom optimizes for
  "looks addressed". Acceptance criteria are what make "done" checkable by a gate
  instead of by vibes.
- **Issue text is untrusted.** Anyone can write it, and text that reaches a coder's
  prompt is an instruction channel. Quote it as evidence inside the brief; never
  paste it as the brief.

So the PM curates: every dispatch is a **self-contained brief** — the goal, the
relevant files, the definition of done, the gates to run — with the source issue
recorded on the feature (`source_issue`, which the PR opener turns into the
`Fixes #N` line, so traceability survives the rewrite). The board enforces the
floor mechanically: the **Ready gate** (`board_mark_ready`) refuses a feature that
lacks a spec, EARS-form acceptance criteria, and an explicit `files_to_modify` list.
A feature that can't state what done means doesn't get a coder.

## The gates: green must be able to fail

The loop's autonomy is downstream of its gates. A board with weak CI and advisory
review does not ship less. It ships unreviewed work faster. Every
"let the machinery play" instruction in the next section is safe for one reason
only: something real can fail the work.

These are the gates that ship today:

- **The repo's own CI** (`.github/workflows/checks.yml`): ruff, import contracts,
  the Python test suite, a live A2A smoke against a real booted server, console
  unit tests, and a Playwright e2e run. The one to copy is the least glamorous:
  the **changelog gate** fails any PR that doesn't touch `CHANGELOG.md
  [Unreleased]`. It is cheap, it is dumb, and it catches a real omission on nearly
  every PR. That ratio is the whole argument for mechanical gates.
- **The pre-PR local gate** (`local_gate_cmd`): the loop runs the repo's own fast
  gate in the coder's worktree before a PR opens. Most failures die there and never
  reach CI. Set it to `"auto"` and the loop discovers the repo's declared `gate`
  target instead of hand-transcribing one that rots.
- **The blocking review gate** (`review_gate: true`): after each PR opens, the loop
  runs the host's `code-review` workflow — the adversarial findings engine of
  [ADR 0077](/adr/0077-adversarial-code-review-workflow) — as a blocking check.
  Blocking findings bounce the feature back to the coder with the findings in the
  retry prompt, bounded by `review_fix_max`. Exhaustion blocks the feature. Never a
  silent merge.
- **Bounded fix rounds everywhere**: `ci_fix_max` caps the CI bounce for red
  checks; `rebase_fix_max` caps conflict re-dispatches for stale branches. Every
  loop is bounded rounds, then blocked for a human. There is no path that retries
  forever.
- **An external review edge**: `POST /plugins/project_board/features/{fid}/review`
  lets a host that reviews PRs with its own fleet drive the same fix rounds from
  outside the loop.

Now the honest half: a gate only binds if it can fail. Two real runs from this
pipeline's own history, one sentence each. A coder reported
`coder.solve verified by acceptance tests` three times while producing zero
commits — the acceptance command passed on an unmodified tree, so the verification
could not fail and certified nothing. A fix round rewrote a function and its tests
together, and the suite went from 5 passing to 9 passing while covering strictly
less than before.

The lesson in one line: **green is not a gate unless red was reachable.** When you
add a check, prove it fails on the broken case before you trust it on the working
one.

## 5. Let the machinery play

The loop has reflexes. The discipline is not preempting them:

- **CI bounce** — a failing check on an open PR requeues the feature with the CI
  error *and the prior attempt's diff* injected into the retry prompt, so the
  re-dispatch improves on the last try instead of repeating it (`ci_fix_max`
  rounds, then blocked).
- **Review requeue** — adverse review findings route back to the *same branch* the
  same way, whether they come from the in-loop `review_gate` or the external
  `/review` route.
- **Auto-rebase** — when a sibling merge leaves an open PR behind base, a clean
  rebase + force-push fixes it with no coder at all; a real conflict re-dispatches
  the coder to resolve it, bounded by `rebase_fix_max`.
- **Merge reconcile** — the `/webhook/pr` merge webhook (or the `merge_poll`
  fallback where GitHub can't reach you) drives the terminal edges: merged → done,
  closed-unmerged → blocked, worktree reaped.

When *not* to intervene: a red PR is not your cue to push a commit, and it is not
the PM's cue to open a new card. The PM preset states the doctrine: *a fix to an
open PR is a fix round, never a new feature* — the bounce machinery already routes
the work to the branch that owns it. Your cue is the **blocked** flag. Blocked
means every bounded budget is spent and the loop is explicitly asking for a human
decision: a real bug to triage, a conflict to adjudicate, a brief to rewrite.

## 6. File every friction

The pipeline fixes itself through itself. When a run exposes a defect in the
machinery — not in the target repo, in the *pipeline* — that defect becomes an
issue on the pipeline's own repo, gets triaged through the same intake, and ships
through the same board. Three real ones from the board plugin's tracker:

- [#105](https://github.com/protoLabsAI/projectBoard-plugin/issues/105) — **zombie
  dispatch**: a feature dispatched into a dead coder worktree.
- [#106](https://github.com/protoLabsAI/projectBoard-plugin/issues/106) —
  **half-applied cancel**: a cancel marked the card but left the worktree dirty.
- [#107](https://github.com/protoLabsAI/projectBoard-plugin/issues/107) — **the
  lead's own retro asks**: the board retro surfaced the coder's recurring failure
  patterns, and the asks got filed instead of re-suffered.

Two mechanisms keep the findings from evaporating. The `friction` plugin's
`record_friction` tool (enable with `plugins: { enabled: [friction] }`) logs the
moment-of-pain signal — a missing tool, a confusing error, an escape-hatch reach —
into a ledger. And the board's read-only `board_retro` tool mines the
attempt/outcome history of completed and blocked features into recurring failure
classes, which the `loop-retro` skill distills into durable grounding. A friction
point that isn't filed is a lesson the next run pays for again.

## 7. Doctrine, not repetition

When you find yourself telling the PM the same thing in every chat, that
instruction has outgrown the conversation. Freeze it, at the right layer:

- **`edit_soul`** ([ADR 0081](/adr/0081-self-authored-persona-edit-soul)) lets the
  agent rewrite sections of its own `SOUL.md` — persona and identity only, never
  operating doctrine. It is guarded and default-off; opt in per agent:

  ```yaml
  soul:
    self_edit_enabled: true    # binds the edit_soul tool (lead agent only)
    drift:
      enabled: true            # default on
      interval_hours: 24
      threshold: 0.25
  ```

- **Soul-history + drift detection** are the safety net around it. Every persona
  write archives the outgoing version to the soul-history dir (restorable from
  Settings ▸ Identity), and the drift pass (`soul.drift` above,
  `soul_drift_enabled` in `graph/config.py`) periodically diffs the live `SOUL.md`
  against its earliest snapshot, publishing a `persona.drift_detected` event when
  the drift score crosses the threshold. It surfaces the signal; it never rewrites.
- **Freeze what proved out into the archetype.** Doctrine that lives in one
  agent's live `SOUL.md` dies with that instance. When a lesson survives contact
  with real runs, commit it back into `config/soul-presets/project-manager.md` so
  every future PM starts with it — that's exactly how the preset's fix-round rule
  and its never-claim-an-untooled-action rule landed (#2273, distilled from a
  dogfood arc and now in this repo's changelog).

That's the full loop: briefs go down, PRs come up, gates decide, friction gets
filed, and what the pipeline learns gets frozen where the next run inherits it.

See [Spawn CLI coding agents](/guides/coding-agents) for the ACP mechanics,
[Delegates](/guides/delegates) for the registry and panel,
[Fleet](/guides/fleet) for workspaces and archetypes, and
[Fork the template](/guides/fork-the-template) for the checklist that precedes all
of this.
