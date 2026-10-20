# Build out your protoAgent with a coding agent

Create a Project Manager (PM) agent, connect it to a coding delegate, and ship a
small feature through a board, pull request, review, and CI.

## Before you start

You need a running protoAgent, a local repository, and CI checks that can reject
an incorrect change. Install these tools on the machine running the agent:

- An authenticated coding CLI and its ACP adapter; see [Coding agents](/guides/coding-agents).
- The `br` beads-rust CLI used by the project board.
- `git` and `gh`, authenticated for the target repository (`gh auth login`).

All binaries must be on the server process's PATH. For a service with a restricted
PATH, use absolute command paths and include the directory containing `br`.
The desktop server inherits the login-shell PATH.

## 1. Register and test a coder {#_1-register-and-test-a-coder}

1. On the host, open **Settings → Delegates** and add an **acp** coding delegate.
   Use the adapter command documented for your CLI; Claude Code and Codex need
   their ACP adapters.
2. Set the working directory to the target checkout and choose the permission
   policy. Use **Test** to verify the command starts and speaks ACP.
3. Keep the delegate's name for the PM's Configure step below.

You can also ask the host agent to propose a delegate. Its `propose_delegate`
tool probes the entry and asks for approval before registration. See
[Delegate setup](/guides/delegates#let-the-agent-propose-one-propose-delegate).

The board owns each feature's worktree, branch, commit, push, and PR. Its coder
edits files and runs tests. For delegation outside the board, see
[Managed git](/guides/coding-agents#managed-git-the-framework-owns-branch-commit-push-pr-adr-0076).

## 2. Create the PM {#_2-create-the-pm}

Open **Settings → Fleet → New agent** and choose **Project Manager** under
**Advanced**. Review the bundle, then fill in Configure:

| Field | Value |
| --- | --- |
| Repo this board manages | Absolute path to the checkout (required) |
| Coder delegate | The tested ACP delegate (required) |
| GitHub repo (`owner/name`) | The target GitHub repository |
| Start the build loop now | Leave off until setup is verified |
| Merge a PR yourself once review + CI are green | Enabled by default; disable to require a human merge |

Creation requires both the repo and coder. If the coder list is empty, return to
step 1. A fleet-shared delegate remains shared; an agent-scoped delegate is copied
into the member with its credentials. Later host edits do not update that copy.
The repo input registers a managed project; once projects are configured, they
form the filesystem boundary. See [Bundle Configure contracts](/guides/bundles#the-manifest).

Open the new PM's console. Confirm that:

- Runtime status has no unresolved setup or missing-capability warnings.
- The **Board** view names the expected repo and loads without a `br` error.
  The plugin initializes a board on first use if needed.
- **Settings → Capabilities → Tools** includes `github_create_issue`.
- The selected delegate tests successfully from the PM.

The PM's file tools are read-only; implementation goes through the coder and PR
gates. To create a PM from a terminal, see [Fleet workspace options](/guides/fleet#workspaces-—-a-named-isolated-agent)
and supply the bundle's required inputs. Workspace creation also needs an explicit
persona and model setup; the app performs those steps for you.

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

**The gates decide.** CI runs, and the review gate (on by default in the
archetype) runs the host's adversarial `code-review` workflow as a blocking
check. The card's labels tell you where it is: `review-pending` while the panel
runs, `changes-requested` when blocking findings went back to the coder on the
same branch, `review-clean` when none survived.

**Then it merges — the archetype's default.** `project_board.auto_merge` is the
knob, and the archetype ships it **on** (the fifth Configure field; the bare
plugin defaults it off). On, the loop squash-merges a PR the moment every gate
it owns is green and current (`review-clean`, CI green, GitHub reports the PR
mergeable, no `merge-hold` label) and the merge reconcile moves the card to
**done** and reaps the worktree — the PM preset treats a board with
`auto_merge` on as its authorization to merge *its own reviewed PRs*, and
nothing more. **Off means a human merges**: nothing merges on its own, the card
waits in **in_review** for you to merge the PR, and the merge webhook (or
`merge_poll` where GitHub can't reach you) notices afterwards. Pick one
deliberately — a board whose cards "sit in review" has almost always been
switched off without anyone taking the merge.

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

**"It's been in review for an hour."** Decode it from the card before touching
the PR: no `review-*` label and `auto_merge` off → it's waiting on *you* to
merge; `review-clean` + `auto_merge` on and still parked → the PR isn't
mergeable on GitHub's side (a **draft** the coder opened itself, a required check
still running, branch protection) — `gh pr ready` / wait / fix the rule;
`changes-requested` → a fix round is in flight; **blocked** with *"review
findings persist after N fix attempts"* → the panel out-argued the coder's
budget — read the last findings on the card, fix or dismiss them yourself, then
**Unblock**, which re-arms the gate with a fresh budget.

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
  of hand-transcribing one that rots. It also powers the *merged-state verify*:
  with a local gate declared, an open PR whose base moved under it is re-gated on
  the state that will actually land before `auto_merge` touches it; without one,
  CI and GitHub's mergeability are the only merge gates.
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

Prove each acceptance check fails on the broken case before relying on it. A
command that passes on the unmodified tree cannot establish that a feature was
implemented. Review changes to tests alongside changes to the behavior they check.

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
`record_friction` tool (on by default) logs the moment-of-pain signal — and open
friction is projected into the agent's own working state, so it acts on the backlog
instead of re-reporting it. Auto-capture stays quiet on real work: a coder's
`git`/`npm test`/`tsc` through `run_command` is not friction, only a shell command
that duplicates a tool it already has (`cat` when `read_file` is bound) is. For
one-click issue links, pin `friction.issue_repo` to the pipeline's repo; it is never
guessed from the target repo. Meanwhile the board's read-only `board_retro` tool mines the
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
  never-claim-an-untooled-action rule landed (#2273), and how three rules from
  protoEngineer's live runs followed (#2978): *a colleague, not a typist* (push
  back on a mistaken ask before boarding it), *the smallest action that solves
  the problem wins*, and *irreversible or outward-facing actions get confirmed*
  — with a board's `auto_merge` counting as the authorization for its own
  reviewed PRs and nothing else. The same pass made the preset ground-first
  through the managed-projects registry (the repo the Configure step
  registered) and taught it that an absent `list_agents` *is* the empty bench —
  not an error to retry.

That's the full loop: briefs go down, PRs come up, gates decide, friction gets
filed, and what the pipeline learns gets frozen where the next run inherits it.

## Related guides

- [Spawn CLI coding agents](/guides/coding-agents) — ACP mechanics, adapters,
  permissions
- [Delegates](/guides/delegates) — the registry and panel
- [Fleet](/guides/fleet) — workspaces and archetypes
- [Fork the template](/guides/fork-the-template) — the checklist that precedes
  all of this
