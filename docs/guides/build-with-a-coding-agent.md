# Build out your protoAgent with a coding agent

**What you'll do:** stand up a project-manager (PM) agent that owns your repo's
board, wire a CLI coding agent under it, and ship your first feature through the
pipeline — brief → board card → disposable worktree → pull request → gates →
merge — without holding the keyboard yourself.

Every command and config key below ships today; nothing is aspirational. If a
coding agent CLI is already installed, the first PR is about thirty minutes away.

## Before you start

Three host binaries have to exist *before* you create the agent — the Configure
step can't install them, and two of them gate the create outright:

- **A CLI coding agent**, installed and signed in: `proto`, Claude Code, opencode,
  or Codex. The [coding agents guide](/guides/coding-agents) lists the ACP adapter
  for each. It becomes the `acp` delegate the board dispatches to (step 2) — and
  the Configure step **refuses to create the PM until one is picked**.
- **`br`** — the [beads-rust](https://github.com/Dicklesworthstone/beads_rust) CLI
  the board is built on: `cargo install beads_rust`. It is *not* the Homebrew
  `bd`; the board checks `br` (or whatever `BR_BIN` names) at register time and
  on every loop tick and reports *"beads CLI 'br' not found on PATH — install
  beads-rust (`cargo install beads_rust`), not the homebrew `bd`, and restart (or
  set BR_BIN); the board is paused until then"* when it's missing.
- **`git` and `gh`**, authenticated for the target repo — the loop pushes
  branches with `git` and opens (and, with `auto_merge`, merges) PRs with
  `gh pr create`. Run `gh auth login` on the host; the GitHub rail resolves auth
  in this order: the plugin's `token` secret (a *Settings ▸ GitHub* field,
  github-plugin ≥ 0.6.0) wins; otherwise `gh`'s own precedence applies —
  `GH_TOKEN`, then `GITHUB_TOKEN`, then the `gh auth login` keyring.

All three must be on the PATH *of the protoAgent process*. The desktop build
hands its bundled server your **login-shell PATH**, so Homebrew, cargo, nvm, and
Volta installs resolve out of the box; a `launchd` autostart, a systemd unit, or
an unusual shell setup only sees the minimal system PATH — there, register the
coder with an **absolute** `command` path and put `~/.cargo/bin` on the
service's PATH ([details](/guides/coding-agents#configure-an-acp-delegate)).

**What the console shows when one is missing.** The coder dropdown in the
Configure step reads *No coding (acp) delegates configured*, and the create is
refused until you register one. A missing `br` shows up on the **Board** view:
the setup card's *Underlying error* line while the board is unbound, a red
*Could not load the board* callout once it is — either way the exact `br`
message above, never a blank panel. An unauthenticated `gh` fails at the first
`gh pr create` — the error lands in the feature's attempt history, and the card
blocks once its retry budget is spent. Core ≥ 0.146 (#2977) adds the seam that
lifts all three into the operator **warning banner** (the same `warnings[]`
that carries the capability-contract notice): a plugin calls
`registry.report_setup_gap(key, message)` and the banner reads
*"Project Board: beads CLI 'br' not found on PATH …"* until the gap clears — live,
no restart. The releases the archetype pins (projectBoard ≥ 0.42.0, github-plugin
≥ 0.6.0) both report through it — the board's *setup preflight* keys `br` /
`gh` / `coder` / `repo`, and **pauses the loop** on `br` / `coder` / `repo`
with the reason instead of ticking into errors, resuming by itself once the
gap clears; the GitHub rail's status probe keys `gh` / `auth`. On a core older
than 0.146 the seam is a no-op and the Board view is where the message lands.

You also need:

- A running protoAgent fork ([fork the template](/guides/fork-the-template)) and
  a repository you want it to grow — usually that same fork.
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

Pick **Project Manager** in the new-agent picker (under the *Advanced* toggle —
it needs a repo and a coder, so it isn't a one-click archetype). The picker
installs the [project-manager-archetype](https://github.com/protoLabsAI/project-manager-archetype)
bundle — the project board, a GitHub rail with write on, a browser for
verification, the review gate's workflow runner, and the friction ledger — seeds
the `project-manager` soul preset, and then **asks** for the five things the
bundle can't guess, as a Configure step (archetype ≥ v0.5.0):

| Configure field | What it sets | Required |
|---|---|---|
| Repo this board manages | `project_board.repo` — absolute path to the git checkout | **yes** |
| Coder delegate | `project_board.coder` — a dropdown of the host's registered **`acp`** delegates (a2a peers and model endpoints are filtered out — they can't take a build) | **yes** |
| GitHub repo (`owner/name`) | `github.default_repo` — where issues and PRs go | no |
| Start the build loop now | `project_board.loop_enabled` — ships **off** until the two above are real | no |
| Merge a PR yourself once review + CI are green | `project_board.auto_merge` — ships **on**; off means a human merges every PR (step 3) | no |

**The two required fields are a hard gate** (core ≥ 0.146, #2977). A create
with either blank is refused — `400`, naming the field — and the half-made
workspace is removed so a retry doesn't collide. The Setup Wizard's install onto
the host itself refuses the same way, at *activate*: the plugins stay installed
and re-running Configure with the answer activates them. There is no "create it
anyway and fix it in Settings": a PM that boots green with no coder is exactly
the failure this guards against.

If the coder dropdown says *No coding (acp) delegates configured*, that's
honest: coding delegates are host-installed binaries, and the wizard can't
conjure one. Register one first (step 2 — Settings ▸ Delegates, or a
`propose_delegate` from any running agent), then come back and pick it.

**What the create does with your answers** (core ≥ 0.146, #2977) — nothing
here is written by hand any more:

- The picked coder is **copied into the member's own delegate registry** — its
  entry *and* its secrets — because a member resolves delegates from its own
  config ([ADR 0025](/adr/0025-unified-delegate-registry-and-panel)), not the
  host's. Before #2977 only the name travelled, and the first dispatch failed
  *not found*. It's a one-time **snapshot**: a later host edit (a rotated key,
  a new `command`) doesn't propagate — re-edit the member's delegate. A picked
  name the host doesn't actually have refuses the create too.
- The repo path becomes a **managed project**: an
  [ADR 0095](/adr/0095-managed-projects-registry) `projects:` entry (name = the
  checkout's directory, `github: owner/name` parsed from its `origin` remote,
  read-only unless `onboarding.write_default` says otherwise) that the PM's
  file tools, the GitHub rail's repo picker, and the board all read. When you
  haven't configured `onboarding:` at all *and* a GitHub remote was found, it
  also enables onboarding scoped to **exactly that repo** — rooted at the
  checkout's parent with `allow: [github.com/<owner>/<name>]` — so
  `onboard_project` resolves the typed repo and nothing wider is clonable until
  you widen the allowlist (no remote → registered only, onboarding untouched).
  The canonical wording, including the fence consequence — once `projects:` is
  non-empty it *is* the filesystem fence — is in the
  [bundles guide](/guides/bundles#the-manifest). This rides a `project: true`
  flag on the bundle's repo input — archetype ≥ v0.6.0 sets it (v0.5.0 predates
  the flag: there, the board still binds through `project_board.repo`, but the
  file tools and the GitHub picker only see the repo if you add the `projects:`
  entry yourself — [reference](/reference/configuration#projects)).

The CLI answers the same Configure step with `--input` (core ≥ 0.146, #2977):

```bash
python -m server workspace new pm \
  --bundle https://github.com/protoLabsAI/project-manager-archetype \
  --input project_board.repo=/Users/you/dev/my-agent \
  --input project_board.coder=claude-code \
  --input github.default_repo=you/my-agent \
  --soul config/soul-presets/project-manager.md
```

Each `--input KEY=VALUE` answers one of the bundle's `config_inputs` prompts —
the required ones included, so a create that skips `project_board.repo` or
`project_board.coder` is refused with the same message as the picker
(*"the bundle needs these Configure answers before the agent can work: …"*),
and a malformed `--input` (no `=`) is a usage error. The `type: delegate` answer
is copied from the **host** config, because the CLI runs inside the host — but
the CLI does **not** carry the host model over, so set the member's `model` (or
run it with `--from`) before it can chat. `--soul FILE` writes the persona the
picker would have seeded; **without it the workspace has no persona** — a PM
with the bundle's tools and a blank `SOUL.md` — and the CLI never records the
archetype's capability contract either way. The API is the third door, with the
exact body the picker sends:

```bash
curl -s -X POST localhost:7870/api/fleet -H 'content-type: application/json' -d '{
  "name": "pm",
  "bundle": "https://github.com/protoLabsAI/project-manager-archetype",
  "soul": "<contents of config/soul-presets/project-manager.md>",
  "requires_tools": ["github_create_issue"],
  "config_inputs": {
    "project_board.repo":  "/Users/you/dev/my-agent",
    "project_board.coder": "claude-code",
    "github.default_repo": "you/my-agent",
    "project_board.loop_enabled": false,
    "project_board.auto_merge":   true
  }
}'
```

For reference, this is what those answers (plus the bundle's seeded defaults)
leave in the member's `langgraph-config.yaml` — editable any time in Settings:

```yaml
project_board:
  coder: claude-code         # the acp delegate the loop dispatches (step 2)
  repo: /Users/you/dev/my-agent   # the checkout this PM owns
  base_branch: main
  loop_enabled: false        # the background puller: ready → worktree → coder → PR — flip once the coder probes green
  max_concurrent: 1          # raise once the repo parallelizes cleanly
  local_gate_cmd: "auto"     # the pre-PR gate — see step 5
  auto_merge: true           # the loop merges a reviewed, green PR itself — see step 3
github:
  write: true                # seeded by the bundle — the PM files issues and reviews PRs
  default_repo: you/my-agent
delegates:
  - { name: claude-code, type: acp, command: /opt/homebrew/bin/claude-agent-acp, workdir: /Users/you/dev/my-agent }   # copied from the host
projects:
  - { name: my-agent, path: /Users/you/dev/my-agent, github: you/my-agent, write: false }   # registered from the repo input
onboarding: { enabled: true, root: /Users/you/dev, allow: ["github.com/you/my-agent"] }   # seeded only when you had no onboarding: section AND a GitHub remote was found
```

One PM, one repo. An agent that sits above several PMs is the Portfolio Manager —
a different archetype, over A2A ([portfolio](/guides/portfolio)).

**What a clean first boot looks like.** Operator status shows no warnings. A
*capability contract* banner means the archetype declared a tool — for the PM,
`github_create_issue` — that didn't bind; the one way to get it today is
`github.write: false`, which the bundle seeds true. That banner exists **only
for fleet members**: the contract is recorded on the member's `workspace.yaml`
at create, so a PM the Setup Wizard installs onto the host itself has no record
to check and never shows it — on a host PM, confirm the tool bound by looking
for `github_create_issue` under Settings ▸ Capabilities ▸ Tools. The **Board**
view shows a setup card naming the bound repo, not a beads error; if the repo
has never had a board, the plugin runs `br init` there on first use.

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

Two ways to get one onto the host. **Settings ▸ Delegates** registers it by hand.
Or ask any running agent — the PM itself, once it exists, or the host agent
before that: *"register Claude Code as our coder"* — its `propose_delegate`
tool validates and probes the entry, then **parks for your approval** with the
command path front and center; nothing registers without you (core ≥ 0.145,
[delegates guide](/guides/delegates#let-the-agent-propose-one-propose-delegate)).

On the **command path**: the desktop build passes its server your login-shell
PATH, so a bare `claude-agent-acp` / `npx` resolves there. An *absolute* path
(`/opt/homebrew/bin/claude-agent-acp`) is the safe fallback for every other
launch — a `launchd` autostart, a Linux service, a shell whose PATH the desktop
shim didn't capture — and it passes the probe either way
([coding agents ▸ PATH](/guides/coding-agents#configure-an-acp-delegate)).
Remember the Configure step copies this entry into the new member verbatim, so
fix the path on the host *before* you create the PM.

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
