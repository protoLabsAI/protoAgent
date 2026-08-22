# Changelog

All notable changes to protoAgent are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Add your entries under [Unreleased]** in your PR. When a release is cut,
> `prepare-release.yml` rolls them into a dated, versioned section via
> `scripts/changelog.py`. See [Releasing](docs/guides/releasing.md).

<!-- archive-index -->
> 📚 **Older releases** are archived: [through 2026-07](CHANGELOG-THROUGH-2026-07.md).
<!-- /archive-index -->

## [Unreleased]

## [0.145.0] - 2026-08-22

### Added
- **ACP `plan` session updates are recorded instead of dropped (#2945).** Coding agents like Claude Code stream their live todo list over ACP; `AcpClient` now keeps the latest plan as `last_plan` (sanitized, capped, latest-wins — the same contract as `last_usage`) so consumers can sample it. The project board's live coder-monitor drawer (projectBoard-plugin v0.41.0) is the first: it renders the coder's plan as a checklist beside its streamed narration and current tool.

- **Agents can now `propose_delegate` — registration still needs your explicit approval (#2953).** An empty delegate roster was a dead end: the agent could see nobody was configured but could only describe what it needed in prose. The new tool validates the proposed entry, probes it, and pauses with an approval card showing exactly what would run (command path included) plus the probe result; only an explicit approval writes it, through the same seam as Settings ▸ Delegates, with a live roster reload. Autonomous turns fail closed — the auto-answered pause declines. Pairs with the archetype `config_inputs` delegate picker so a board agent's coder can be set up end to end without hand-editing config.

- **The Project Manager archetype is listed again — the flagship for demos — and the first-run docs now match what the wizard actually does (#2963).**
  Held on 08-21 for first-run friction, every item has since shipped (github.write seeded, setup card instead of a raw beads error, a Configure step that asks for repo / coder / GitHub repo / loop toggle, the review-gate runner in the bundle, auto-merge reachable on a default board). The build-with-a-coding-agent guide gains the Configure-step flow, the merge step (`auto_merge` off means nobody merges), and a "stuck in review" decoder; `projects:` and `onboarding:` get reference sections; the first-agent tutorial describes the archetype picker instead of the retired persona presets.

### Changed
- **Creating an agent now lands you in it (#2952).** Create-from-archetype and snapshot
  import used to drop the operator back on the fleet list — one click short of the agent
  they were obviously about to configure. Both flows now navigate straight into the new
  agent's own console (its id is the URL slug, the same full-page navigation the
  FleetSwitcher uses). One deliberate exception: a snapshot import that comes back
  incomplete (credentials missing, plugins failed) stays put on an in-page summary naming
  exactly what's still needed — navigating immediately would unload that detail before it
  could be read — with an Open button to move in once it has been. Also fixes a crash
  typing into the import panel's credential fields (the event target was read after React
  had already nulled it, so the first keystroke unmounted the panel).

- **The Project Manager persona grounds first and proposes its own bench (#2954).** Two live first-run gaps: the preset now runs `onboard_project` on first contact with a repo (registry-backed local reads instead of per-file HTTP fetches), and treats an empty delegate roster as a `propose_delegate` moment — a validated, probed entry the operator approves — instead of a dead end it can only narrate. Pairs with project-manager-archetype v0.4.0, which now ships the review gate runnable (workflows member) and the friction ledger on.

### Fixed
- **A failed conversation harvest no longer costs the thread its knowledge (#2950).** The retire path deleted checkpoints even when the harvest had failed (its swallowed `None` was indistinguishable from "nothing to harvest"), so a transient 429 at retire time — the normal case on a shared fleet OAuth account — permanently skipped capture; 8 threads were lost this way in one day. Sweep-path failures now keep the thread for the next sweep (capped at 3 consecutive failures, then a loud delete), explicit chat deletions still delete but log the loss, and sweep harvests space themselves with jittered gaps so back-to-back model calls stop manufacturing rate-limit bursts.

- **Parked (HITL) turns now appear in telemetry (#2951).** A turn that paused for operator input never recorded a row, so every model call made before the pause vanished from usage/cost numbers — the most expensive turn class, systematically undercounted. The park leg now records its own outcome with the pre-pause spend (state `input_required`, success left NULL so failure-rate queries stay honest); the resumed leg keeps recording separately as before.

- **Model fallback is no longer silent (#2956).** `ModelFallbackMiddleware` failed over without a trace — a successful fallback was indistinguishable from a normal turn, so the operator's first clue of a degraded primary was a quality drop days later. The routing wiring now uses `ObservableModelFallbackMiddleware`, a drop-in subclass that logs a WARNING naming the primary failure and the fallback model that served, and publishes a `model.fallback` event (primary exception class, fallback model, fallback index) on the event bus (ADR 0039) for plugins/telemetry to consume. A HITL interrupt (`GraphBubbleUp`) now also propagates untouched instead of triggering fallback retries, and when every fallback fails the primary exception — the failure worth diagnosing — is the one re-raised.

- **"Test connection" now works for a Claude subscription (#2957).** The OAuth connection probe streamed a bare user prompt, but Anthropic's OAuth infra refuses traffic whose system prompt doesn't lead with the Claude Code identity line — so the probe 429'd while real turns (which get the line from `ClaudeCodeIdentityMiddleware`) worked fine. The probe now sends the identity prefix as its system message for `anthropic-oauth`; Codex keeps its bare user prompt, since the Responses backend rejects system-role items.

- **Infisical host tolerates the CLI's `/api` suffix (#2958).** The Infisical CLI's
  `--domain` value includes `/api`, but `secrets_manager.host` must not — the provider
  appends `/api/v1/...` itself, so a pasted CLI value produced `…/api/api/v1/…` and a
  bare 404. The host is now normalized (a trailing `/api` is stripped,
  case-insensitively), and a 404 on universal-auth login with an `/api`-suffixed host
  gets an explicit hint in the error string.

- **A `show_component` widget is never hidden inside a folded turn (#2965).**
  A reasoning model thinks between the component and its final sentence, so the widget landed in the collapsed "Worked" timeline instead of the answer — the tool ran, the server emitted it, nothing visible happened. Components now always lead the answer, streaming or settled.

### Docs
- **The docs and marketing sites now unfurl with the same branded social card as the GitHub repo (#2942).**
  Both were serving the unbranded robot banner as `og:image`, with no `og:description` on docs and no
  `twitter:title`/`twitter:description` on either; shared links now show the 1280×640 wordmark card
  with the tagline, and the marketing card moved to a new URL so stale scraper caches refresh.

## [0.144.0] - 2026-08-21

### Added
- **Bundles can declare `config_inputs:` — setup prompts wired to plugin config (#2934).** A
  `protoagent.bundle.yaml` can now carry a `config_inputs:` list (`{key, label, type,
  required?, default?}`, same shape as MCP catalog inputs) declaring plugin config keys the
  operator should fill at create time. The SetupWizard and the fleet New Agent panel render
  them in the Configure step — text for `string`/`path`, a dropdown of configured ACP
  delegates for `delegate`, a toggle for `boolean` — and the install path writes the answers
  into the host/member config at the declared dotted key paths (declared keys only; an
  unanswered input falls back to its default without clobbering a live value). Bundles
  without `config_inputs:` behave exactly as before.

### Fixed
- **The marketing site no longer installs React for one stylesheet (#2930).** `sites/marketing` is a pure Astro site, but it pulled the entire React runtime via `@protolabsai/ui` just to import `styles.css`. It now depends on `@protolabsai/ui-css` — the CSS-only split of the `.pl-*` component classes — so `react`/`react-dom` drop out of its dependency tree entirely. No visual change.

- **Fresh installs no longer show the per-turn token/cost footer under chat answers (#2931).**
  `showChatUsage` now defaults to off, so a new install gets a clean transcript out of the
  box; Settings ▸ Chat still turns it on. Existing installs keep their current setting —
  zustand/persist hydrates the flag from localStorage, so the new default only applies where
  no persisted UI state exists.

- **The Docs view no longer 401s on first load of a token-gated host (#2933).** Same race #2926 fixed for the friction and devkit views: the first fetch fired from module scope before the console's bearer handshake reached the iframe, so a fresh desktop member opened Docs to "Could not load docs — HTTP 401". The first load now rides the handshake, with a fallback for open-mode/top-level pages.

- **Plugin views can no longer 401 on first load — the DS kit itself now waits for the bearer (#2935).** `@protolabsai/ui` 0.60.1's `apiFetch` awaits the console handshake (bounded) before its first request, covering every plugin view at the kit layer; the earlier per-view fixes (#2926, #2933) become belt-and-braces. Also picks up ui 0.60.0's React-free `ui-css` stylesheet split (no visual change).

- **One-shot scheduled tasks resume the chat that scheduled them (#2939).** `schedule_task` never passed the originating session to the scheduler, so every fire — even a "remind me at 3pm" scoped to a conversation — landed in the `system:activity` thread. One-shot ISO schedules now carry the turn's session as `context_id` (the same injected-state pattern `wait` uses), so the fire resumes that conversation. Cron schedules deliberately stay context-free and keep firing into Activity — a recurring job resuming a chat the operator closed days ago would be wrong.

### Removed
- **The Project Manager archetype is held from the new-agent picker (#2932).** An operator first-run test hit the capability-contract banner (the bundle seeded `github.write: false` against its own `requires_tools: [github_create_issue]` contract) and a raw `br init` error from the unbound board on desktop. The catalog row is parked (not deleted) with its bundle, persona, and contract intact, and returns once the companion fixes land (project-manager-archetype#3, projectBoard-plugin#192). Existing agents created from it are unaffected.

## [0.143.0] - 2026-08-20

### Added
- **Queued-steer placeholder hint (#2837).** The composer shows "Press ↑ to edit queued
  message" when a mid-turn steer is queued, improving discoverability of the edit
  affordance. Idle ("Message protoAgent…") and streaming-with-nothing-queued ("Steer the
  agent…") placeholders are unchanged.

- **Auth-gated e2e mock lane (#2886).** The console e2e mock server gains a real bearer-gate
  mode (boot with `?gated=1` — a per-context cookie keeps parallel specs hermetic) mirroring
  `a2a_impl/auth.py`'s default-deny + public allowlist: anonymous SPA/static chrome, anonymous
  plugin view page chrome (`public_paths`), 401 everywhere else, plus the bearer-gated
  `/api/sse-token` → `/api/events?token=` handshake. A new `auth-gated-views.spec.ts` boots the
  console against it with NO injected headers and proves the real auth flow end to end — auth
  dialog → chat surface → SSE connect → an authed chat turn with zero post-auth 401s — catching
  the #2884 class of bug (a view subresource not covered by the auth handshake), which the
  existing route-interception spec structurally cannot see.

- **ACP session observability (#2889).** New `GET /api/acp/sessions` route surfaces live
  coding-agent sessions (thread, agent, busy status) for delegation triage.

### Changed
- **Query keys are now slug-namespaced (#2887).** React Query cache keys include the agent slug, preventing stale cross-agent data if in-place agent switching is ever built.

- **Background report cards shrink to dismissable chips (#2923).** A finished background job's report now renders in chat as a single compact row — 📄 title + "Open" + ✕ — instead of the ~200px clamped-excerpt card. "Open" still opens the full report in the document viewer; dismissing removes the chip from the transcript (persisted per job, so a reload doesn't resurrect it) while the report stays reviewable in the Background agents panel.

### Fixed
- **Plugin form callbacks survive reload (#2889).** Pending slash-command form
  submissions no longer silently expire when plugins are hot-reloaded.

- **Background delegation dispatches collapse into a compact chip (#2896).** `delegate_to(background=true)` / `task(run_in_background=true)` tool calls no longer render as full-height cards in the chat stream — a turn that fired several buried its answer under identical "Started a background delegation…" cards. They now fold into one muted, expandable "N background jobs" chip (even a single one) and never occupy the streaming spotlight slot; foreground tools keep the existing spotlight/fold behavior. Detection parses the call args at render time, so no schema change; unparseable mid-stream args read as foreground until they complete.

- **Changelog gate now blocks merge (#2906).** A malformed changelog fragment (missing the top-level bullet) no longer slips through auto-merge.

- **`onboard_project` registers into the managed-projects registry, so the GitHub plugin sees onboarded repos (#2925).** The tool wrote its entry into `filesystem.projects` — the fs-fence projection — which the GitHub plugin never reads, so every onboarded repo was missing from `/issue` and the GitHub board. It now writes the ADR 0095 `projects:` entry (`github` binding + `default_branch` probed from `origin/HEAD`) and mirrors the fence entry only where an explicit `filesystem.projects` override is in force; a repo living only in that legacy override is promoted into the registry on re-onboard.

- **Friction and devkit plugin views no longer 401 on first load (#2926).** Both fired their first request before the console's bearer handshake had reached the iframe, so on any token-gated instance the Friction rail opened to "Couldn't load the friction ledger: 401 Unauthorized" until a manual refresh. The first load now rides the handshake (with a fallback for open-mode/top-level pages). Also fixed a stray NUL byte in `friction/view.html` that made git diff the file as binary.

### Removed
- **The Social Marketing archetype is held from the new-agent picker (#2921).** It isn't release-ready yet; the catalog row is parked (not deleted) so it returns in a future release with its bundle and persona intact. Existing agents created from it are unaffected.

## [0.142.1] - 2026-08-20

### Fixed
- **The Linux desktop release builds again (#2915).** The new webview smoke test spoke only weston 10's CLI; the release runner's weston 9 rejected it (`unknown backend "headless"`) and the all-or-nothing manifest design held the whole v0.142.0 desktop release. The smoke now version-gates its weston invocation, so both dialects boot the compositor.

## [0.142.0] - 2026-08-20

### Added
- **The desktop app ships for Linux — `.AppImage` and `.deb` are on the download page (#2866).**
  The Linux build compiled on every release but sat behind a notify-me signup pending a real
  test of it. Tested: it runs clean on a normal desktop session, Wayland or X11. What held it
  up was a WebKitGTK 2.52 crash that only fires where accelerated compositing can't initialize
  — a bare `Xvfb`, some container/CI setups, X-forwarding or VNC without DRI3 — where the web
  process sends `EnterAcceleratedCompositingMode`, the UI process never built a backing store,
  and `AcceleratedBackingStore::update()` dereferences null ([WebKit #321683](https://bugs.webkit.org/show_bug.cgi?id=321683),
  patch proposed upstream, not landed). Not our bundle: v0.132.0, v0.139.0
  and v0.140.0 all crash identically on WebKitGTK 2.52.3 and are all clean on 2.44.0. On a
  headless box, run the server directly (`python -m server --ui console`) and use the browser
  console; `apps/desktop/README.md` has the detail, including why the webview can't be
  smoke-tested under `xvfb-run` in CI.

- **CI renders the desktop app now, and the marketing site builds on PRs (#2878).** Two blind
  spots, both found the hard way. (1) Nothing ever rendered the webview — `live_smoke.py --bin`
  boots the frozen sidecar only — so an app that died seconds after the console painted shipped
  green through six launches (#2866). `scripts/desktop_webview_smoke.sh` now boots the bundled
  AppImage against a real GL compositor (weston headless + Mesa llvmpipe; no GPU or DRI device
  needed, so a stock hosted runner can do it), waits for `/app` to serve, and soaks to catch the
  crash that lands *after* first paint. Wired into `desktop-build.yml`'s Linux leg. Don't run it
  under `xvfb-run`: on a machine whose EGL loader can select a GPU vendor driver that's the
  default outcome, and the smoke fails for reasons unrelated to the build.
  (2) `sites/marketing` was only ever built by the deploy workflow on push to main, so a broken
  download page or lockfile took main and the live site down together instead of failing the PR
  — `marketing-check.yml` now builds it on any PR that touches it and asserts the page links a
  real installer for all three platforms.

- **Expose fleet autostart in Settings UI (#2880).** The "Box runtime" chip on the Fleet
  panel now includes the `fleet.autostart` field, so operators can declare which members
  start on boot without editing YAML.

- **Archetype repos have a registry.** `config/plugin-directory.yaml` gains an `archetype_repos:` section listing the published archetype repos, and a guard test cross-checks the shipped archetype catalog against it — a renamed or retired repo now fails CI instead of drifting through docs and examples.

### Changed
- **"Stack" is retired — archetype is the one product noun (ADR 0100 amendment).** A *bundle* is the mechanism (how a pinned plugin set installs); an *archetype* is who an agent starts as (persona + optional bundle); a published bundle repo shipping an `archetype:` block is an *archetype repo*, named `<name>-archetype`. The published repos were renamed (`cowork-archetype`, `social-archetype`, `project-manager-archetype`, `design-system-archetype`, `portfolio-manager-archetype`, `product-archetype`) — GitHub redirects keep existing pins and old install URLs working, and the shipped archetype catalog now points at the new names.

### Fixed
- **The download page no longer offers Android and ChromeOS an x86_64 desktop binary (#2866).**
  Both match `/linux/` in the user-agent, so the OS sniffer classified them as Linux. That was
  harmless while Linux had no build to hand out; now that it does, they fall to the
  unknown-platform block instead. The page also refuses to build unless the resolved release
  carries the Linux installers, the same guarantee the macOS and Windows links already had
  (#2514) — a visible download link can't 404.

- **Turn telemetry includes subagent model calls (#2872).** The `task` tool now propagates each subagent's model, token usage, and cost into the parent turn's telemetry, so per-turn cost reporting no longer undercounts delegated work.

- **The marketing site deploys from its lockfile again (#2875).** `sites/marketing/package-lock.json`
  was missing `react`, `react-dom` and `scheduler` — `@protolabsai/ui` declares them as peer
  dependencies, npm auto-installs peers, and the lockfile was never regenerated after that. Nobody
  noticed because `marketing-deploy.yml` ran `npm install`, which silently re-resolves rather than
  failing; the cost was that two deploys of identical source could ship different transitive
  versions, and `npm ci` was broken for anyone working on the site locally. The lockfile is now
  complete (98 entries added — the peers plus the platform-optional esbuild/sharp binaries — with
  **zero** existing versions changed) and the workflow installs with `npm ci`, so what deploys is
  what's locked. npm 11 is pinned once up front for both the marketing and docs installs.

- **Plugins see the live host config at register time on cold boot
  (#2877).** The lazy host fields (`HOST.config`, `HOST.apply_settings`)
  are now wired before plugin loading — previously a plugin that captured
  `registry.host.config` in `register()` silently got nothing on a fresh
  boot while working after a console hot-enable, so it broke on the next
  app restart (the promptlab playground incident).

- **Fix flaky Windows test for workflow run ordering (#2883).** Added a monotonic sequence tie-breaker to `WorkflowRunStore.recent()` so same-tick `updated_at` values sort deterministically.

- **The Artifact panel works again on bearer-gated instances (#2884).** Since
  the shell moved out of the inline view HTML into a real `shell.js` (#2822),
  every auth-gated instance — the whole desktop fleet — silently 401'd the
  script (a `<script src>` carries no Authorization header) and every panel
  rendered the "No artifact yet" empty state while the artifacts sat on disk
  and every data route worked. The file is now declared public chrome like
  the vendor modules beside it, and two regression tests guard the class:
  every same-origin subresource a plugin view page references must be
  auth-exempt, and the artifact chrome is asserted reachable header-less
  through the real gate while the data routes stay gated.

- **Plugin view panels surface persistent fetch failures (#2885).** The artifact panel now shows an error strip after 3+ consecutive poll failures instead of the misleading empty-state. Documented the error-vs-empty-state rule in the plugin views guide.

- **`delegate_to`'s A2A poll loop speaks 1.0 now (#2892).** The GetTask poll
  sent the v0.3 legacy `{"name": …}` param under a 1.0 version header — a 1.0
  peer rejects it, so a delegation to any peer that answers asynchronously
  (non-terminal task from SendMessage) could never converge. Latent because
  protoAgent peers answer inline; real against other A2A implementations. The
  poll now sends `{"id": …}` (pinned against the SDK proto), and a peer that
  parks on a human-input interrupt fails fast with a legible "parked waiting
  for operator input" error instead of burning the full poll timeout and then
  claiming the peer was still running.

- **Unknown `/commands` are refused inline instead of becoming agent turns (#2893).**
  A chat message like `/foobar` whose token resolves to no registered slash command
  (goal, lifecycle, plugin command, workflow, subagent, or user-facing skill) now
  short-circuits with `Unknown command /foobar. Type / to see available commands.`
  — in both the streaming and collected turn paths — instead of silently falling
  through to a normal agent turn on the raw command text. Messages that merely
  contain a `/` (paths like `/home/user/file.txt`, prose like `use uv/pip`) still
  reach the agent unchanged.

- **Delegation questions now flow back to the calling agent — and can be answered.**
  When an A2A delegate parks on operator input (`TASK_STATE_INPUT_REQUIRED`),
  `delegate_to` no longer fails the dispatch: it returns the delegate's question
  together with the parked task id and instructions to resume. A new
  `resume_task_id` parameter sends the answer back into the parked task (A2A
  resume: `SendMessage` with the parked `taskId` + `contextId`), so agent X can
  answer agent Y's question — or escalate it up its own chain via `ask_human`
  first — and the delegated work continues where it stopped instead of starting
  over. Resuming a task that already finished reports its result; resuming one
  that is still running is refused legibly.

- **Discover no longer advertises `coding_agent` as an enableable plugin.** It's a built-in library that ships through `delegates` (always on) — its catalog row carried an enable instruction that could never work. The row is now marketing-site-only, and a tightened guard test keeps any future manifest-less directory row out of the in-app catalog.

- **A delegate that parks its question inline no longer gets mistaken for an answer.**
  protoAgent peers answer `SendMessage` synchronously — a HITL park comes back inline
  with the task already `input-required` and the question in `status.message`. The
  dispatch's inline-text early-return handed that bare question back as if it were the
  delegate's final reply, losing the parked task id and the resume protocol with it.
  The park is now detected before the early-return, so the calling agent gets the
  ⏸ question + resume handle in this (the common) shape too. Caught by a live
  end-to-end smoke; the poll-path unit test alone missed it.

- **Parallel delegations no longer corrupt each other (#2899).** Firing `delegate_to` at the same coder more than once at a time interleaved every prompt into one ACP session — each caller got doubled fragments of someone else's turn, which read as coder failure and invited wasted re-dispatches. The client now serializes turns itself: concurrent callers queue, bounded by their own timeout.

- **Renamed archetype repos never double up in the new-agent picker.** Archetype
  catalog rows can carry `bundle_aliases` (former URLs of a since-renamed bundle
  repo); an agent that installed `cowork-stack`/`social-stack`/
  `project-manager-stack`/`design-system-stack` before the `*-archetype` renames
  keeps a single picker card after upgrading.

- **The marketing site no longer tells you to enable `coding_agent` (or `delegates`).** Built-in library/always-on rows now render "built-in" instead of an `enable … in plugins.enabled` CTA that could never work — the app-hidden (`app: false`) directory rows carry an explicit `enable: null` through to the site overlay.

## [0.141.0] - 2026-08-20

### Added
- **The Docs view can point at any folder of markdown (#2842).** A new
  `docs.root` config (also in the plugin's Settings) redirects the bundled
  Docs plugin — reader view, ⌘K search, and the `docs_search`/`docs_read`
  agent tools — at an operator-chosen directory tree of `.md` files:
  runbooks, a team wiki, project notes, anything. Custom trees group by
  top-level directory (no Diátaxis assumed), hidden directories are skipped,
  and a symlink escaping the root is not a doc. Empty `root` keeps today's
  bundled protoAgent docs byte-for-byte; a bad path falls back loudly instead
  of losing the Docs view to a typo.

- **`scripts/context_audit.py` — one command for "why is this thread at
  121k" (#2844).** Sizes a chat thread's checkpoint into honest categories
  (assistant text, tool args counted exactly once, tool results, injected
  memory frames) and joins telemetry to expose the fixed per-call overhead
  no checkpoint contains. Backed by `graph/message_blocks.py`, the now-
  canonical message walk — `content` already contains the `tool_use`
  blocks, and summing it with `tool_calls` double-counts.

- **The prompt viewer explains the whole window (#2847).** `/prompt` and the
  View-prompt dialog now include the conversation-history breakdown from the
  #2844 context audit — categories, top tool-arg producers, honest "(as of
  now)" framing — via a new cheap `GET /api/prompts/breakdown` that works
  even with prompt capture off.

- **The plugin devkit enforces the SKILL.md contract (#2854).** A
  frontmatter-less skill file is now refused at write time with the
  loader's own reasons, and `test_plugin`/`develop_plugin` lint every
  skill before pytest — a green suite can no longer ship skills that
  silently never load. Backed by `graph.skills.loader.skill_md_problems`,
  the loader's contract as a reusable validator.

- **A turn now provably survives the operator walking away (#2857, Swap &
  Resume S0).** Characterization tests pin the server-owned-turn contract: an
  A2A streaming turn whose SSE consumer disconnects mid-flight (agent switch,
  reload, dropped connection) keeps running to completion, and the tool frames
  it emits while nobody is watching land in the durable task history — the
  ground the reattach work builds on. No behavior change; the contract was
  true but untested.

- **Switching agents (or reloading) mid-turn now reattaches to the live turn
  (#2858, Swap & Resume S1).** The console calls A2A `tasks/resubscribe` on
  return — the primitive was served and fleet-proxied all along, just never
  called — replaying the durable task snapshot (every tool card, reasoning
  step, and text the agent produced while nobody was watching) and then
  streaming the live tail like a normal turn. A cold agent behind the fleet
  proxy is retried with backoff instead of freezing the bubble forever on the
  first 409, a turn that ended while detached replays once and finalizes, and
  the reconcile ceiling now covers the proxy's full 600s turn budget instead
  of two minutes.

- **Your half-written message survives an agent switch (#2860, Swap & Resume
  S3).** The composer draft and any queued mid-turn steers persist per session
  (per tab, per agent) and come back after a swap or reload; a draft or ready
  attachments prompt before the page unloads instead of vanishing silently —
  while a merely-streaming turn never blocks navigation, because turns are
  server-owned and reattach on return. Scroll position is remembered too:
  returning to a transcript you'd scrolled back through restores your place,
  and near-bottom keeps the default pin-to-latest.

- **A session's chat history is finally readable server-side (#2865, ADR 0104,
  Swap & Resume S5).** `GET /api/chat/sessions/{id}/turns` serves the durable
  A2A task store's record of the session — every turn's status, artifacts, and
  per-frame history (tool calls, HITL prompts) in the same wire shapes the
  console's stream dispatcher already replays, in-flight turns included.
  Until now the rendered history lived only in one browser's localStorage; a
  second device saw an empty chat for a session the server knew everything
  about. The console keeps localStorage as its primary store — this is the
  recovery and multi-device substrate.

### Changed
- **The workflow builder becomes "Outline & Focus" (#2835).** Authoring was one
  flat form — the recipe's shape and its prompts interleaved in a single
  column. Now the outline (step cards with live validation dots, the
  parallelism lanes, workflow/inputs/output entries) sits beside a focus
  editor where the selected step's prompt fills the pane, `depends_on` is a
  row of toggle pills, chips insert at the cursor, and inputs get a real
  editor for the typed-input contract (type, description, default). Server
  validation lands on the card that's wrong instead of a list at the bottom.
  Panel-sized via container query: narrow panels get a horizontal outline
  strip above the editor.

- **Builder flow polish — reorder, duplicate, Save & test (#2839, S3 of the
  Outline & Focus redesign).** Step cards drag-reorder by their grip handle: a
  strict linear chain is re-threaded so execution order follows the new visual
  order, while any other DAG keeps its `depends_on` untouched (the lanes stay
  the truth). The focused step gains a Duplicate action (unique `-copy` id,
  focus follows the clone), and "Save & test" saves the recipe and lands on
  the run form with it selected and its input defaults seeded — the tightest
  author-iterate loop.

- **The workflow builder is a node-and-edge DAG canvas (#2846).** Steps are
  nodes (validation dot, gate marker, subagent badge), `depends_on` is a real
  edge you drag to create (cycle attempts refused) or delete to remove, and
  node positions persist on the recipe — the n8n/ComfyUI shape, because a
  graph you can see is the easiest to understand. Selecting a node opens the
  editor beside the canvas; variable insertion became a grouped picker
  offering inputs and **upstream** step outputs only (a non-ancestor's output
  renders empty at run time, so the old chip wall was offering a mistake).
  Includes the interim outline-era improvements: Save & test, duplicate step,
  typed-input editing, per-step live validation.

- **Workflows is opt-in again (#2848).** v0.140.0 shipped the plugin enabled
  by default; that default is reverted — the engine, tools, and Studio load
  only when an operator adds `workflows` to `plugins.enabled`, as before.
  Nothing else from the GA batch changes. **Migration:** an instance that
  updated through v0.140.0 and wants to keep workflows needs
  `plugins.enabled: [workflows]` after this release.

- **The console stops lying about busy agents after a swap (#2859, Swap &
  Resume S2).** Sessions whose last assistant message is still streaming with
  a durable task id come back **active and locked** on load — every such tab
  reattaches (not just the focused one), the composer stays locked, and Stop
  stays visible, so a returning operator can't accidentally fire a second
  concurrent turn into a session whose first turn is still running. The event
  bus's replay cursor now survives navigation too (per-agent, per-tab), so
  retained topics — an approval request raised while you were away, a
  background completion — replay on return instead of vanishing with the page.

- **Swapping agents can't kill a working agent, and abandoned streams unwind
  (#2862, Swap & Resume S4).** The warm-cap's LRU-eviction grace now defaults
  to five minutes — and the hub refreshes a member's recency on every turn
  start through the proxy, so the grace window tracks agents that are
  *working*, not just ones the operator clicked (rapid A→B→A switching could
  previously evict A mid-turn; `grace_seconds: 0` restores pure LRU). Proxied
  SSE streams gain a 30s comment keepalive: a stream whose viewer walked away
  now unwinds within one keepalive period instead of parking on the member
  until its next write. The plugin-views guide documents the proxy's 20s read
  lane (and the SSE/WS exemptions) for plugin authors.

- **`/prompt` opens the prompt dialog (#2863).** The slash command now
  opens the same full viewer the message row's View prompt action opens —
  tabs, budget bars, history breakdown — instead of an inline note; the
  note remains only as the degrade path when nothing is captured.

### Fixed
- **A mapping-authored `inputs:` no longer breaks the Studio (#2834).** Recipes
  written with the natural YAML shape — `inputs: {topic: {required: true}}` —
  crashed the builder's edit loader (taking the whole Studio view with it) and
  silently emptied `{{inputs.x}}` validation, because `get()` handed the raw
  YAML through. The registry now canonicalizes inputs to the documented
  `[{name, required, default?}]` at load and save (so files converge on the
  canonical shape), and the builder normalizes defensively so an unexpected
  shape can never blank the surface again.

- **Fs tools see mid-turn project registrations (#2836).** The fenced filesystem tools resolved their project registry from a snapshot captured at graph build, so a project registered by `onboard_project` stayed invisible until the next turn. The tool closures now resolve the registry through the live `HOST.config` seam on every call — `list_projects`, `list_dir`, `read_file`, `find_files`, and `search_files` see a just-onboarded project on the same turn, and `onboard_project` itself merges against the live registry, so a second onboarding in one turn no longer drops the first. Both live reads are guarded: an unwired, raising, or `None`-returning seam falls back to the build-time config instead of failing the tool call.

- **`develop_plugin` no longer blocks the chat turn — and no longer refuses a
  multi-delegate roster (#2838).** The ACP coder dispatch used to run inline for
  up to 15 minutes with nothing visible; it now runs as a detached background
  job (ADR 0050) — the tool returns a job handle immediately and the coder +
  `test_plugin` + reload report drains back automatically on a later turn (the
  spine is preserved; lean/CLI contexts without a manager still fall back to
  the inline path). And with several `acp` delegates configured and
  `plugin_devkit.coder` unset, `_resolve_coder` now default-picks one (a
  `sonnet`/`claude-code`-named delegate, else the first alphabetically) and
  logs the choice, instead of returning an operator-addressed refusal that
  pushed the model into hand-writing the plugin inline.

- **`plugin_read_file` paginates by line (#2840).** The devkit read tool now
  takes optional `offset` (1-based line) and `limit` (max lines), matching the
  core `read_file` addressing from #2709: a small file still comes back whole
  in one call, and a truncated result names the next offset to continue from —
  so build loops re-read just the region they're editing instead of burning
  the full read cap after every write.

- **fetch_url strips page chrome from HTML (#2841).** `_extract_text_from_html` now decomposes banner `<header>` elements while keeping article/section headers — a header survives inside `<main>`/`<article>`/`<section>`/`<aside>`, and on pages without those wrappers only banner-position (direct child of `<body>`) or nav-wrapping headers are treated as chrome, so a title/byline `<header>` nested in a plain `<div>` is preserved. It also strips ARIA-landmark chrome (`role="navigation"`, `"banner"`, `"contentinfo"`) and `sidebar`/`menu`/`breadcrumb` widgets by exact class token (`menu-item`/`sidebar-open` survive); pages without `<main>`/`<article>` fall back to `role="main"` and then the dominant `<div>` by text (never narrowing past a kept `<header>`/`<h1>`) before the whole `<body>` — so div-soup pages (Reddit-style) no longer drag global nav and sidebars into context. The no-bs4 regex fallback is unchanged.

- **Web E2E's Playwright apt install waits for the dpkg lock (#2845).** The
  runner's unattended-upgrades kept winning an apt-lock race against
  `playwright install`, flaking 2 PRs in 2 days with exit 100 ("Could not get
  lock"). The install step now drops `DPkg::Lock::Timeout "120"` into
  `apt.conf.d` — the only channel that reaches the apt-get calls Playwright
  runs internally — and primes the package lists with an explicit lock-wait
  `apt-get update` before the retry loop, so stragglers are waited out instead
  of failing the attempt instantly.

- **Subagents declaring `tools: []` now run as text-only transforms** instead of
  being refused with "No tools available". A declared-empty toolset is a
  deliberate design (edit/summarize/classify passes — including model-pinned
  passes against gateway lanes that reject tools-bearing requests); tools that
  were declared but resolve to none still return the actionable error.

- **The Web E2E job stops fighting apt (#2864).** It now runs in the
  pinned Playwright container image — browsers and system deps
  preinstalled, zero apt at runtime — retiring the lock-contention flake
  that failed five runs in two days, with a guard that names the exact
  tag bump whenever `@playwright/test` moves.

- **`skills.top_k: 0` means "list none" again (#2869).** The #2868
  identities-never-drop index would still emit every skill name when the
  operator turned the index off entirely — off now means off.

- **The desktop updater can no longer install a stale release (#2832).** The
  update dialog could sit open while newer builds became Latest, and "Install
  and Relaunch" would install the originally-offered version. Confirming now
  re-checks the endpoint first: still-Latest installs the fresh object
  (current URLs and signature), a superseded offer re-prompts against the
  version that will actually install (never a silent swap), a withdrawn offer
  says so, and a failed re-check installs nothing rather than gambling on a
  stale download. Direct-to-Latest behavior and signature verification are
  unchanged.

## [0.140.0] - 2026-08-19

### Added
- **Workflow runs are now observable while they execute (#2829).** The workflows
  plugin gains `POST /{name}/start` (validate up front, run detached, poll the
  run record), a live per-step record (step graph snapshot, running/done/failed
  lifecycle with timestamps and engine seconds, final envelope), run history
  (`GET /runs/all` + `GET /runs/{run_id}`), a `POST /validate` endpoint for live
  builder validation, background resume with an up-front precheck, and
  terminal-run retention (`max_runs`, default 200). Grounds the Studio's live
  run timeline; the sync run route and `run_workflow` tool are unchanged.

### Changed
- **The Studio actually shows a workflow run happening (#2830).** Run starts a
  detached run and watches it live — per-step status with durations, outputs
  expanding as they land, an inline gate card for `gate: human` pauses, and a
  History list that reopens any past run in the same timeline. The builder can
  now EDIT an existing recipe, toggle operator gates, set input defaults, insert
  `{{…}}` template refs from chips, and shows the server's validation errors
  live while authoring; the recipe's parallelism renders as DAG lanes.

- **Workflows is GA — the plugin ships enabled by default (#2831).** The engine,
  the `run_workflow`/`save_workflow` tools, and the Studio console surface now
  light up out of the box instead of requiring `plugins.enabled: [workflows]`.
  Opting out is `plugins.disabled: [workflows]`.

### Fixed
- **The chat model dropdown no longer needs a Settings visit to fill in
  (#2828).** The composer mounts once for the app's lifetime, so its boot-time
  settings-schema fetch could race the server (graph still compiling, provider
  probes empty) and the menu then sat frozen on a one-model fallback until
  Settings ▸ Model happened to refetch the shared cache. The menu now refetches
  on open whenever the cached schema is stale or carries no model options.

## [0.139.0] - 2026-08-18

### Added
- **The PTC graduation bench is an eval suite (#2807, ADR 0103).**
  `python -m evals.ptc_bench` generates deterministic labeled fixtures and a
  two-lane tasks file (loop: `execute_code` forbidden; code: the bridge must
  PROVABLY fire via the `ptc:` audit prefix, direct reads forbidden), drives
  `evals.runner`, joins per-turn telemetry by pinned session ids, and judges
  the thresholds pre-registered on #2807 — rounds ≥5x collapse, tokens ≥3x or
  wall ≥2x, and verifier-checked correctness, so a cheaper-but-wrong run can
  never graduate. Prompt-kind eval cases now honor `context_id` (previously
  goal cases only), the seam the telemetry join needs.

- **File artifacts get typed previews (#2816).** The Artifact panel's
  download card now renders CSV/TSV as a real table (quoted fields handled,
  500-row cap with an honest caption), `.md` as formatted prose via the
  markdown renderer, and `.json` pretty-printed — with truncation-aware
  parsing so a clipped preview never shows a mangled row. Data and code
  files (tsv, toml, ini, xml, html, py, js, ts, sh, sql) now get verbatim
  text previews instead of a "(binary file)" note.

### Changed
- **The zero-cache-hit watcher now tells "provider ignores caching" apart from
  "provider doesn't report it" (#2772).** An OpenAI-compatible lane that omits
  `prompt_tokens_details` (e.g. vLLM without `--enable-prompt-tokens-details`)
  used to draw the same "provider is likely ignoring prompt caching" warning as
  a genuinely cache-dead route — sending operators to the wrong fix, since the
  lane may be caching invisibly (homelab-iac#242 was exactly this). Absent
  cache fields now draw a reporting-gap warning naming the vLLM flag; explicit
  zeros keep the original message. No usage-mapping change was needed: verified
  live that once a lane reports, `prompt_tokens_details.cached_tokens` already
  flows through langchain-openai's normalization into telemetry's
  `cache_read_input_tokens` (cold 0 → warm 32k of 35.7k input on
  `protolabs/reasoning`).

- **The trajectory becomes readable: `/trajectory` + the read API (#2806, ADR
  0102 S2).** `GET /api/trajectory/{session}` pages the S1 writer's event
  stream (stable absolute indices, rotation-spanning), and
  `/call/{n}` reconstructs any model call's request envelope with per-message
  availability joined against the live checkpoint — `available` (preview
  included), `rewritten` in place (a pruner stub or repair), or `missing`
  (compacted/rewound away; the hash and size still prove what was sent). The
  chat-native `/trajectory` command renders the tail timeline — calls, usage
  with cache share, and every ⚠-flagged history rewrite — plus the latest
  call's availability readout.

- **PTC reaches GA: binding-path parity for bridged tool calls (ADR 0103 S4,
  #2807).** A script's bridged call is now the same path as a model-issued
  call, with a different caller: late-tool factories receive the
  denylist-final toolset (previously a tool could be unbound from the model
  yet still bridgeable), every bridged call is checked at dispatch against
  the turn's `subagent_fence` and against an enforcement gate built from the
  same `enforcement_*` config fields (denylist exact-parity; rate limits
  apply per-path), and policy denials land as failed `ptc:` audit rows. With
  the graduated bench (rounds 5.5×, correctness 4/4) this closes the ADR —
  Accepted; the GA surface is plugin enablement + `execute_code.tools`, and
  the never-implemented `tools.ptc.enabled` flag is retired rather than
  added. The plugin also reads its live config at graph build, so an edited
  allowlist takes effect on reload.

- **The artifact plugin is nine modules instead of one 1,800-line file
  (#2819, #2817 P1).** Pure reorganization — store, previews, render
  feedback, tools, routes, and the console shell each live in their own
  module, with `__init__.py` down to `register()` plus the public surface.
  No behavior change.

- **The artifact console shell is real HTML/JS files (#2822, #2817 P2+P3).**
  `shell.html` + `shell.js` replace the 540-line embedded Python string —
  editable, diffable, and visible to tooling, with the served behavior
  unchanged and the srcdoc `<\/script>` escape discipline now guard-tested
  on both sides.

### Security
- **`plugins.sources.allow: []` now means deny-all, not open (#2743 item 1).**
  Absent-vs-explicit-empty becomes semantic, copying the `sources.official`
  pattern (#2691's lesson): key absent = any URL allowed (unchanged default);
  an explicit empty list = deny all plugin installs — the hardening stance a
  defense-minded admin writing `[]` actually means, where it previously and
  silently meant "open". **Migration**: a config carrying a literal
  `allow: []` from before this release flips from open to deny; the boot log
  warns loudly and the install error names the deny-all state — remove the key
  to stay open, or list trusted origins. The CLI path preserves the same
  distinction (it used to collapse explicit `[]` back to open).

## [0.138.0] - 2026-08-18

### Added
- **Round governance: long turns get re-grounded, and can be budgeted (#2710,
  ADR 0101 D8).** The August audits showed round count is an
  instruction-adherence lever, not just a cost one — 21 rounds into one turn,
  an agent violated its own "check the board and open PRs before creating
  ANYTHING" rule and produced a duplicate work item. New
  `RoundGovernorMiddleware`: at `model.round_nudge_after` rounds (default 25)
  it injects ONE re-grounding note per turn (re-read the request and working
  state; re-check before creating anything new; prefer finishing over
  starting); an optional `model.round_hard_cap` (default off) ends the turn
  with an honest hand-back instead of running to the recursion limit. Genuine
  mid-turn steering resets the count; machinery (context frames, stall-guard
  notes, compaction summaries) never does.

- **Context pressure is now a persisted series, and the next request's floor is
  projected (#2773, ADR 0101 D6).** Each turn's peak context-window fill
  (`context_tokens`) lands in the telemetry store (guarded migration, same
  pattern as tool durations), `recent()` rows carry a derived per-turn
  `cache_hit_ratio`, and `summary()` gains `max_context_tokens` /
  `p95_context_tokens` — so cache-discipline work (#2776/#2777) has a provable
  before/after instead of a one-turn live readout. The `context-v1` part now
  also carries `projectedTokens` (last call's prompt + completion): what the
  next request on the thread starts from before anything new is added.

- **Rolling cache breakpoints on message history (#2777, ADR 0101 D1).** We
  placed one `cache_control` breakpoint (of Anthropic's four) on the system
  prompt and none on the conversation — so every round of a long agentic turn
  re-paid the entire growing history uncached, most of the measured 31%-vs-99%
  cache-hit gap. `PromptCacheMiddleware` now also marks the newest two markable
  messages per call (view-only via `request.override` — stored history stays
  clean), so call N+1 reads call N's history from cache. Tool results are
  markable on the native Anthropic client (the block's mark is lifted onto the
  tool_result envelope) and skipped on the gateway path, where the converter
  would silently drop the mark — verified empirically for both wire shapes.

- **Oversized tool results already in history get pruned before summarization
  ever runs (#2782, ADR 0101 D3/D4).** Output caps applied at call time only —
  once in the checkpointer, a result was re-sent verbatim on every later model
  call until compaction removed the *entire* history. New
  `ToolResultPrunerMiddleware` (config `pruning:`, on by default): at 60% of
  the model's context window, tool results older than the newest 20 messages
  are rewritten to head+tail stubs in one batched pass (a single cache miss,
  not one per call), replaced by message id so tool-call pairing survives. The
  marker is honest — the middle is gone; re-run the tool if it's needed. Runs
  registered before compaction: prune near-lossless, then summarize lossy.

- **A context-window overflow is now a recovered event, not a dead end (#2783,
  ADR 0101 D4).** Nothing caught the overflow error class before: the raw
  provider error surfaced, model fallback re-sent the same oversized prompt
  elsewhere, and the next turn on the thread hit the same wall. The turn
  runner now recognizes each provider's overflow phrasing, force-compacts the
  thread once (safety-valve semantics: archive best-effort with a loud log,
  honest stub summary if the summarizer fails — the manual `/compact` keeps
  its strict never-lossy refusal), and retries a single time. A second
  failure surfaces honestly — but the thread is smaller, so the next turn no
  longer inherits the wall. Counted as `overflow_recoveries_total`.

- **The console surfaces context pressure honestly (#2787, ADR 0101 D6).** The
  per-turn hover card now shows the projected next request (the floor the next
  message starts from, from `context-v1`'s `projectedTokens`); the meter's
  wrong-axis fallback to the turn's summed input tokens is gone (a 38-call turn
  "sums" to millions while the window sits far lower — old history now shows no
  meter rather than a wrong one). The Telemetry surface gains Context p95/peak
  summary metrics and a per-turn Context column, with the per-turn cache-hit %
  beside the cache-reads cell — the before/after readout for the cache work.

- **Plugin repos can now own their eval suites (#2804).** `python -m
  evals.runner --tasks-file <path>` runs any tasks JSON with the full
  runner — audit-log, pattern, KB, and rubric channels included — with
  reports model-tagged in `evals/results/` alongside the core suite. New
  `forbidden_tools` case key asserts *selective* abstention: unrelated
  tools may fire, the named ones must stay cold (an errored attempt still
  counts — reaching for the tool is the violation). First consumer: the
  cowork pack's live eval suite (cowork-plugin#4).

- **The trajectory writer: "what did the model see" is now answerable (#2806,
  ADR 0102 S1).** Every model call logs its request envelope as references —
  message ids + content hashes + sizes, the stable-prefix hash, a bound-tools
  hash, the real per-tab model — plus a response event with usage, into a
  per-conversation append-only JSONL (`instance_root/trajectory/`). Every
  history rewrite (auto/manual/forced compaction, pressure pruning, rewind,
  fork, tool-call repair) lands as a `surface_op` event with counts and ids,
  so any rewrite is attributable and the model-visible view at any point is
  derivable. Refs hash the STORED bytes (reconstruction joins the checkpoint);
  size-capped rotation; the log retires with its thread. Read API, search,
  and the full-text mode are the ADR's next slices.

### Changed
- **The dynamic context layer rides the message stream, composed once per turn
  (#2776/#2779, ADR 0101 D2).** Recalled memory, the skills index, working
  state, and the one-shot toolset notice used to be recomposed on **every model
  call** and delivered as a second system block sitting between the cached
  stable prefix and the history — with a prefix-based cache key, that churn
  invalidated any history caching every call, most of the measured 31% cache
  hit ratio. The layer is now composed once at turn entry and appended as one
  tagged `<injected_context>` frame in the turn's input; nothing between the
  stable prefix and the history varies intra-turn anymore (the skills-index
  MRU order and working state are snapshotted per turn by construction). The
  ADR 0069 delivery contract — untrusted-reference envelope, attribution,
  incognito and goal-turn scoping, id-attributed injection log — moves intact.
  Exports render frames as a one-line marker; chat bundles exclude them.

- **Subagent delegations get prompt caching (#2778, ADR 0101 D1).** The subagent
  middleware stack omitted `PromptCacheMiddleware` entirely, so every `task` /
  `task_batch` delegation paid full uncached input on its (static-per-build)
  system prompt — acknowledged in a code comment, never fixed. The stack now
  mirrors the lead's caching (same config knobs, same reject/ignore watchers):
  repeat delegations to a subagent type, and every model call inside one
  delegation's tool loop, read the prefix from cache.

- **Prompt-cache TTL resolves by profile (#2780, ADR 0101 D7).** Fleet members
  and the packaged desktop app are long-lived agents that routinely idle past
  the 5m ephemeral tier between turns, re-paying the full stable prefix on
  every re-warm. Absent an explicit `prompt_cache.ttl`, those profiles now
  default to the `1h` persistent tier (one avoided re-warm covers its higher
  write price); interactive dev instances keep `5m`. Explicit config always
  wins, same rule as the tier-aware `filesystem.allow_run` default.

- **Auto-compaction archives before it rewrites (#2784, ADR 0101 D5).** The
  automatic path was the lossy one: the summarization rewrite landed in the
  checkpoint, per-thread pruning destroyed the pre-compaction rows, and the
  summarized-away history was simply gone — while the never-lossy manual
  `/compact` archived first. The full transcript now lands in the knowledge
  store (`chat-archive:<session_id>`, the same namespace `/compact` uses)
  before the rewrite is committed. Failure mode, operator-decided: attempt the
  archive; on failure compact ANYWAY with a loud log — the safety valve's duty
  outranks purity on the automatic path, and the manual path keeps its strict
  refusal.

- **`/compact` is generally available (#2785, ADR 0101 D5).** The `chat.compact`
  developer flag sat past its own `remove_by` date while gating the one
  NEVER-LOSSY compaction path — lossy auto-compaction ran by default the whole
  time, which was exactly backwards. The flag is removed everywhere (registry,
  route gate, console tag); the never-lossy semantics themselves are unchanged.

- **The execute_code tool bridge gets a security posture (#2807, ADR 0103 S1).**
  Starting the PTC spike surfaced that the bridge already existed — and that
  its default exposed EVERY registered tool to model-written scripts, HITL and
  delegation included. The default is now the curated read-mostly set
  (read/list/find/search/fetch/web_search/memory_recall/current_time), and
  HITL tools, `task`/`task_batch`, and `execute_code` itself are structurally
  unbridgeable even when named in config — an interrupt can't park a
  subprocess, and delegation from model-written code is out of scope by
  decision. Operators widen the set by naming tools explicitly. The spike's
  measurement lands with it: a deterministic ten-reads-one-round test (model-
  visible output <0.1% of the intermediate bytes) and `scripts/ptc_bench.py`
  for live model-in-the-loop numbers.

### Fixed
- **`install-deps` re-validates source trust at deps time (#2743).** A plugin
  installed before an allowlist or trust tightening could still pip-install its
  declared deps — "was trusted then" silently implied "is trusted now" for a
  code-adjacent step. The installer now re-checks the plugin's recorded origin
  against the CURRENT `plugins.sources.allow` on every deps install (CLI and
  console), and the console route re-runs the same one-time "this runs code"
  consent gate as install itself — an untrusted source answers `needs_ack` and the
  Plugins panel shows the familiar trust dialog, then retries. Bundled and
  hand-copied working-tree plugins (no recorded origin — nothing was fetched) are
  exempt from both checks.

- **A turn that composes no dynamic context no longer re-sends the previous
  turn's (#2774, ADR 0101).** `context` is a last-write-wins channel persisted
  in the checkpointer; when `KnowledgeMiddleware` had nothing to inject it
  returned `None`, leaving the prior call's RAG hits / memory digest in state
  for `PromptCacheMiddleware` to re-append — the model saw stale injected
  memory attributed as current. Nothing-to-inject now explicitly clears the
  channel (and its sections readout with it).

- **The per-tool context-cost chip (#2282) actually renders now (#2775, ADR
  0101).** It estimated from the SSE frame's `output` — which the server
  truncates to 800 chars (≈200 tokens), strictly below the chip's own 250-token
  display floor, so it could mathematically never appear. The `tool_end` frame
  (and its tool-call-v1 fragment) now carries `outputChars`, the true
  pre-truncation result size, and the chip estimates from that; older servers
  without the field fall back to the previous behavior.

- **MCP tool results are size-bounded at last (#2781, ADR 0101 D3).** Every
  built-in tool truncates its output at call time, but MCP results had no cap
  anywhere — a server returning 500KB put 500KB into history, re-sent verbatim
  on every later model call for the life of the thread. New
  `mcp.max_result_chars` (default `50000`, matching `read_file`'s cap; `0`
  disables; per-server `max_result_chars` overrides) rewrites an over-cap
  result to bounded head + omission marker + bounded tail — both ends survive,
  and the marker names the true size and the knob. Applies across both session
  modes and to multi-block results against one shared budget.

- **"Fork from here" gives the fork real memory (#2803).** The fork was
  display-only: the new tab showed the transcript while the agent's checkpoint
  was empty, so the branch's first reply came from an agent that remembered
  none of it. Forking now copies the checkpoint prefix through the clicked
  message onto the new session's thread server-side — rewind's non-destructive
  sibling, same content/occurrence resolution and tool-pair-safe cut, source
  untouched, never clobbers an existing thread. When there is genuinely no
  server history to fork, the branch says so with a visible note instead of
  silently pretending.

- **PTC S2+S3: the bridge shows its schemas and its calls (#2807, ADR 0103).**
  S2: the execute_code description now renders real call signatures for every
  bridged tool (params + defaults + each tool's first description line,
  budgeted at 25 lines) — the model writes kwargs instead of guessing against
  a name-only proxy. S3: every bridged call lands in the audit log and the
  per-tool Prometheus series as `ptc:<name>`, with duration, success, and the
  run's session id (InjectedState) — a script's tool calls were previously
  invisible to the operator entirely.

### Docs
- **ADR 0101 — Context lifecycle: log, surface, pressure (#2772).** The first
  decision record governing how a session's context grows, shrinks, and gets
  priced. Prompted by a 30.9% measured prompt-cache hit ratio against the ~99.9%
  that cache-disciplined harnesses achieve: the stable prefix was already
  byte-stable by design, but no breakpoints covered message history, the per-call
  volatile context block churned between prefix and history, and subagents were
  entirely uncached. Decides cache discipline as a contract (rolling history
  breakpoints, per-turn context composition in the message stream), a uniform
  tool-result size/pruning policy (MCP outputs were completely uncapped),
  prune-before-summarize with one-shot overflow recovery, archive-first
  auto-compaction, persisted context-pressure telemetry, TTL tiering, and adopts
  the #2710 round-count circuit breaker. Implementation tracked phase-by-phase
  under #2772 (#2773–#2787).

- **ADR 0102 — The trajectory: append-only session log + derived surface
  (#2806).** The log half of ADR 0101's deferred log/surface split, giving
  #2786 its home. A per-session append-only JSONL of request-envelope
  REFERENCES (message ids/hashes/sizes, stable-prefix hash, surface-op events
  for every history rewrite) makes "what did the model see on turn N"
  answerable — honestly bounded where pruning destroyed bytes, with an opt-in
  full-text mode for deep forensics and fork-from-any-point. Takes DeepSeek
  Harness's "model-visible means logged" invariant without its event-sourcing
  architecture tax. Slices: writer → read API/console view → search →
  full-text flag → the telemetry-gated derived-view surface.

- **ADR 0103 — Programmatic tool calls: agent tools callable from
  execute_code (#2807).** The CodeAct/DSH-"code mode" pattern, designed
  against protoAgent's constraints: a ten-read investigation becomes one model
  round because the model's script batch-calls tools and only its
  stdout/return value re-enters context. Opt-in twice, single-run bearer
  token over loopback dispatched through the SAME binding-layer path as a
  model call (ADR 0089 posture preserved), curated read-mostly allowlist with
  HITL hard-denied, a generated per-run stub module, and full
  audit/trajectory/telemetry visibility for bridged calls. Spike-gated: Slice
  1 measures rounds/tokens/wall-clock on a real multi-read task, and the ADR
  graduates or dies on those numbers.

- **Evals guide documents the plugin-suite seams (#2811).** `docs/guides/evals.md`
  gains the negative assertions (`forbidden_patterns`, `forbidden_tools`) and the
  `--tasks-file` plugin-owned-suite flow from #2804; the cowork directory listing
  catches up to v0.3.0 (`/daily-brief`, drop-folder watches, `verifier` chip).

## [0.137.2] - 2026-08-17

### Fixed
- **Chat streaming is smoother — text trickles instead of filling in ~10-word blocks
  (#2766).** The A2A executor batched answer/reasoning deltas by size alone
  (`_FLUSH_CHARS = 60`, no time dimension), so the console bubble filled in visible
  lurches — and a slowly-generating model parked its tail in the buffer indefinitely,
  since nothing flushed until the next delta tipped the size check. The flush is now
  size-OR-time: the threshold is back to 24 chars (the original value, whose
  teardown-race concern was already re-tested and disproven), and a non-empty buffer
  older than 100ms flushes on the next delta regardless of size — fast producers still
  batch, slow producers trickle word by word, and the first words of a turn flush
  immediately.

## [0.137.1] - 2026-08-17

### Changed
- **The A2A flush-granularity regression test now locks the 60-char frame granularity (#2672).**
  `tests/test_a2a_flush_granularity.py` streamed a 2.7KB answer and only sanity-checked
  "more than 20 frames" — a silent regression back to coarse batching (or a per-token
  flood) would still pass. The test now streams a ≥4KB answer through the real
  `SendStreamingMessage` SSE path and asserts the artifact frame count lands within
  ±20% of `answer_len / _FLUSH_CHARS`, alongside the existing intact-text and
  no-teardown-grace-warning checks.

- **The console is on the current design system (#2761).** `apps/web` had been sitting on
  `@protolabsai/ui@^0.57.0` / `@protolabsai/design@^0.5.1` while published was 0.59.2 / 0.9.1
  — the last stale DS consumer in the repo, and the one where it matters most, since the
  console is the surface that ships the theme picker and therefore owns the `[data-theme]`
  force the design system documents. Everything in the gap is additive, so nothing in the
  console had to change to accommodate it: `Button` gains a dense `xs` size, `Drawer` gains
  top/bottom sheet sides (`width` kept as a deprecated alias for `size`), `ThemePanel` gains
  a DTCG token export and live WCAG contrast guardrails, segmented `Tabs` scroll instead of
  spilling, and interactive `Tr` gets a focus-visible ring. Verified past CI: typecheck and
  build clean, 963 tests across 105 files pass, the built console boots rendering a correctly
  themed DS shell, and theme forcing resolves in both directions on the new tokens.

### Fixed
- **The docs site follows your OS colour scheme now, instead of always being dark (#2760).**
  `agent.protolabs.studio/docs` stayed black for light-mode readers while the marketing
  pages one path segment up adapted — same domain, opposite behaviour. It needed two
  fixes, either alone being insufficient: `.vitepress/config.mts` set
  `appearance: "force-dark"`, which the shared theme documents as legacy ("locks to dark,
  disables light mode"), and `@protolabsai/design` was pinned at `^0.3.0` — a version that
  predates light mode entirely (it landed in 0.5.0), so there were no light token values
  to switch to even once appearance allowed it. `appearance` is now `"auto"` (following
  the OS, plus VitePress's own toggle), and design moves to `^0.9.1` alongside
  `@protolabsai/vitepress-theme` `^0.3.11`. The two package bumps are deliberately paired:
  vitepress-theme 0.3.11 pins design 0.9.1 exactly, so bumping the theme alone would have
  pulled a nested second copy of the token layer while the root stayed on 0.3.x — and
  `--pl-*` are global custom properties, so whichever stylesheet loads last would decide
  the palette. Bumping both together deduped the tree instead.

- **`anthropic-oauth` agents with string-shaped system prompts no longer fail every
  call with a fake 429 (#2763).** Anthropic's OAuth enforcement now requires the
  Claude Code identity line to be the system prompt's first block **byte-exactly** —
  a block that merely starts with the line, or the merged `"{line}\n\n{persona}"`
  string the identity middleware used to emit for string prompts, is refused with a
  generic `429 rate_limit_error` carrying no rate-limit headers (quota untouched —
  verified at 7% utilization while every call "rate limited"). Which shape an agent
  emitted depended on whether its prompt flowed as a string or a block list, which is
  why some oauth agents worked while others hard-failed on the same account seconds
  apart. The middleware now always emits the exact-first-block shape: string prompts
  become block lists, an oversized first block is split (keeping `cache_control` on
  the remainder), and the old merged shape is repaired rather than skipped as done.

## [0.137.0] - 2026-08-16

### Added
- **The bundle pin-bump PR lifecycle now has automated test coverage (#2669).** The
  `bump` job's dedup/approval-flagging logic (ADR 0049, #2645) lived entirely as inline
  bash in `examples/bundles/template/.github/workflows/verify-bundle.yml` — verifying a
  change to it meant a manual dry-run against a real GitHub repo. It's now a standalone,
  unit-tested script (`scripts/bump_pins_pr.sh`), exercised by `tests/test_bump_pins_pr.py`
  against a real throwaway git repo with `gh` stubbed on `PATH`. No behavior change — the
  workflow YAML now just calls the script instead of inlining its body.

- **Agent boot is now instrumented, and there's a `/perf` chat command for a live
  performance snapshot (#2245, #2674, #2677, #2678).** Agent construction — checkpointer,
  knowledge store, plugins, MCP, graph compile — is timed phase-by-phase and reported both
  to Prometheus (`*_boot_phase_seconds{phase}`) and as a single structured `[boot] phase
  timings: ...` log line, so a slow boot now says *which* phase is slow instead of just
  "boot took a while." `/perf` surfaces the existing turn-level telemetry (cost, p50/p95
  latency, cache-hit rate, top models, flagged outliers) directly in chat — no round-trip
  through Settings ▸ Telemetry needed. That same telemetry rollup (`TelemetryStore.summary`)
  now also computes p99 duration, plus p50/p95/p99 duration *per model* — both durable, from
  data already recorded per turn, no new capture pipeline required — and the console's
  Telemetry surface shows them alongside the existing per-model cost/token breakdown.
  Per-*tool* latency isn't included: unlike the LLM-call path, `server/chat.py`'s tool-call
  handling has no timing seam yet, so that's tracked separately rather than bolted on here.

- **Plugin lifecycle latency is now instrumented per plugin (#2675).** The three
  lifecycle stages — load (module import), config (schema/resolved-config binding),
  and registration (`register(registry)`) — are timed with `time.monotonic()` per
  plugin and emitted to a `*_plugin_lifecycle_seconds{plugin,phase}` Prometheus
  histogram, following the `AuditMiddleware`/boot-phase (#2674) timing idiom. A
  stage crossing 500ms logs a WARNING naming the plugin and stage, so a slow plugin
  dragging boot or reload shows up in the log by name instead of hiding inside the
  aggregate boot-phase `plugins` bucket. Instrumentation point #3 from #2245.

- **Knowledge-store op latency instrumentation (#2676).** The hybrid knowledge store's three hot paths — hybrid query (RRF k=60), ingest (`add_document`/`add_chunk`), and embed round-trips — are now timed with `time.monotonic()` deltas and exposed on `/metrics` as the `*_knowledge_op_seconds{op=query|ingest|embed}` histogram (a failed embed is timed too — the transport-timeout case is exactly the sample worth having). Ops that run inside an A2A turn additionally attribute to that turn's telemetry row under `knowledge:{op}` keys in the same per-tool durations blob as tool calls (the #2697 pipeline — no schema change), so the by-tool percentiles pick them up for free. Instrumentation point #6 from #2245.

- **Publish a chat thread to a shareable, read-only link — pre-release (#2179 P2, #2682, #2683).**
  A `Publish…` gesture (`/publish` slash command, tab context-menu item) previews exactly
  what will become public — the redacted structured bundle, rendered with the real chat
  components — before anything leaves the instance, then POSTs it to a configured hosted
  endpoint and gets back a public link. Behind the `chat.publish` developer flag (off by
  default): the hosted service this publishes to (#2685) doesn't exist yet, so every
  publish attempt honestly reports "not configured" until an operator points
  `publish.endpoint_url` (Settings ▸ Publish) at a real one. See ADR 0099.

- **Published links can now be listed and revoked — Settings ▸ Publish (#2179 P2, #2684).**
  Every successful publish is recorded locally; a new Published Links card (Settings ▸
  Publish, behind the `chat.publish` developer flag) lists them with a revoke action per
  row. Revoking presents the stored token to a separately configured
  `publish.revoke_endpoint_url` and only marks the link revoked locally once the hosted
  service confirms — never before, so the console can't claim a link is dead while it's
  still live. The publish success note no longer carries the raw revoke token now that
  there's a real place to manage it. Also fixed: the `publish.*` endpoint-URL fields added
  alongside the publish/preview routes were unreachable in Settings (mapped to a category
  nothing renders) — they now live in their own Publish section. See ADR 0099.

- **The marketing changelog page collapses prior months (#2695).** `/changelog` had grown to
  144 releases across 3 months of unbroken scroll. The current month still renders flat; each
  prior month now collapses into a native `<details>` section showing a release count, expanding
  on click — no new JS dependency, matching the marketing site's React-free design.

- **Telemetry breaks down latency by tool, not just by model (#2697).** The turn loop now
  stamps each tool call's execution time (`on_tool_start`→`on_tool_end`), durably recorded as
  a per-turn JSON blob and aggregated into p50/p95/p99 duration per tool — a "By tool" table
  on the Telemetry surface, and a "Slowest tools" line in the `/perf` chat snapshot, both
  sorted slowest-first so the tools worth investigating lead.

- **`read_file` can page past its 50K-char cap (#2707).** A new `offset` param lets
  a file larger than one chunk be read in full across a few calls — a truncated
  result now names the offset to pass next, instead of permanently capping the file
  at its first chunk in every call.

- **`read_file` can skip a redundant re-read within one turn (#2708).** New
  `tools.memoize_reads.enabled` flag (off by default): when on, a repeat
  `read_file` call for a file this turn already read — and hasn't since been
  written to — returns a short pointer instead of the content again, saving real
  context tokens rather than just a disk read. Found via a live-session audit
  where the same file was read twice, byte-identical, for ~12.5K wasted tokens.

- **The project-manager persona now nudges toward `task` delegation for wide
  multi-repo investigation (#2711).** Found via a live incident where a broad
  board-cleanup ask ("unblock where it makes sense... tackle do next bucket")
  ran entirely in the lead agent loop — 38 sequential LLM calls, $58.83 in one
  turn — because the persona never mentioned `task()` delegation at all. The
  board-decision step itself stays single-threaded (it genuinely needs
  cross-card judgment); only the per-repo code-reading that informs it is
  pushed out to parallel `codebase-mapper` subagent calls.

- **A bundle's `archetype:` block can now name a `soul_preset`, and typo'd keys warn at
  install (#2715).** The catalog path always supported `soul_preset` / inline `soul`;
  the bundle path read inline `soul` only, so a preset-naming bundle silently shipped
  the base persona. The block was also cached into `plugins.lock` unvalidated — an
  unknown key (`souls:`, `require_tools:`) vanished with no signal. Both seams are now
  loud: an unknown preset warns and falls back, unknown keys warn (never fail), and the
  reference bundle template documents the full field set.

- **Bundles now have a lifecycle past install — update, re-pin, uninstall as one
  action (ADR 0049 D4), and a real guide (#2718).** A bundle was first-class at install
  and never again: `check_updates`/`sync` ignored the lock's `bundles` rows (a published
  stack's pin never moved on an installed host) and removing one meant hand-uninstalling
  members against a provenance row that went stale. Now the update check covers bundles
  (`GET /api/plugins/updates` → `bundles[]`), `POST /api/plugins/bundles/{id}/update`
  re-resolves the ref (release-tag pins move to the newest semver tag), re-pins every
  member, re-applies the declared enable set *without undoing an operator's explicit
  disable*, and retires members the new manifest dropped; `DELETE /api/plugins/bundles/{id}`
  removes exclusively-owned members + the lock row and hot-reloads (shared members
  stay). CLI: `plugin update-bundle` / `uninstall-bundle`, with the out-of-process
  liveness warning. The new `docs/guides/bundles.md` documents the whole lifecycle —
  manifest reference, install semantics per surface, updating, publishing a stack with
  the verify/pin-bump CI.

- **The devkit now covers the entire plugin lifecycle (#2719).** It stopped at
  authoring (scaffold/edit/test/enable/reload) — installs, updates, disables, and
  removals had to be handed back to the operator. New tools: `install_plugin` (console
  semantics — enables + hot-reloads, load failures surfaced, allowlist enforced,
  `activate=False` for fetch-only), `update_plugin` (release-tag pins move to the
  newest semver; a bundle id updates the whole bundle incl. retiring dropped members),
  `disable_plugin`, `uninstall_plugin` (live teardown — the half the CLI can't do),
  and `verify_bundle` (read-only pre-install peek: members, seeds, archetype-block
  problems). All route through the same ops/installer layers the console uses.

- **The one-time "this runs code" consent is real (ADR 0071 D3 S4–S6, #2721).**
  Installing from a source that is neither official (`plugins.sources.official`,
  fork-overridable) nor previously acked now answers `needs_ack` — before anything is
  fetched — and the console asks with a proper confirm: the install-by-URL dialog and
  the Discover one-click path (which previously had **no confirm at all**) share the
  new TrustAckDialog; confirming persists the exact repo into `plugins.sources.acked`
  via `POST /api/plugins/ack`, and "don't ask again" flips `plugins.trust_unverified`.
  Fetch-only installs (`PROTOAGENT_PLUGIN_INSTALL_NO_ENABLE=1`) skip the gate — no
  code runs, nothing to consent to yet. The docstrings #2720 corrected now describe a
  dialog that actually exists.

- **Trust & consent foundation (ADR 0071 D3 S1/S2, #2721).** The config now carries
  `plugins.sources.official` (auto-trusted source globs — default the protoLabsAI org,
  fork-overridable, with explicit-empty meaning "no official sources"),
  `plugins.sources.acked` (sources the operator confirmed once), and
  `plugins.trust_unverified` (the don't-ask-again switch), all persisting through the
  config write path — the exact drop the June audit warned would make an ack re-ask
  forever. `graph/plugins/trust.py` resolves them (`source_trusted`, spelling-normalized
  globs, exact-repo ack grants). Plumbing only: nothing asks yet — the ack API and
  console consent dialog are the follow-up slices.

- **A bundle lifecycle smoke joins the #912 single-plugin one (#2724).**
  `tests/test_bundle_lifecycle_smoke.py` drives one host agent through a bundle's whole
  life against the real installer/loader/config layers: install (fan-out + provenance
  row) → enable the declared set → seed (config-defaults overlay + declared `mcp:`
  server) → use a member's tool → update (a moved member re-pins, a
  dropped-from-the-manifest member is retired, #2718) → one-action uninstall with
  nothing dangling. The seams between the per-layer tests now have a regression net.

- **Installed bundles are actionable from the console (#2737).** The Installed tab
  gains a compact strip listing each installed bundle with its freshness (the updates
  poll's bundle rows — "update available" when the bundle repo's manifest moved),
  an Update action that re-pins members and retires ones the new manifest dropped,
  and an Uninstall action behind a confirm that says exactly what goes
  (exclusively-owned members) and what stays (shared members). Toasts report every
  failure the backend declares — a failed retirement, enable-reload, or hot-reload
  is never dressed up as success.

- **One repo-owned fast gate script, shared by local devs and PR CI (#2746).**
  `python scripts/gate.py` now runs the quick deterministic pre-PR checks in one
  command — `ruff check .`, `lint-imports`, `gen_attribution --check`,
  `uv lock --check` (skipped with a warning when `uv` is absent), and
  `pytest tests/ -q` — sequentially and fail-fast, with `--lint-only` for a
  pre-commit smoke without the test suite. CI's `lint` job invokes the same
  script instead of its previous four inline steps, so the local and CI gates
  can't drift. Pure stdlib and argv-only (no `/bin/sh`), so the one command
  works on Windows and POSIX alike.

### Changed
- **The marketing site is rebuilt on the shared protoLabs design system (#2702).** It had
  drifted into a look protoLabs.studio doesn't ship — centered 6xl bold headings, a radial
  hero glow, lavender-filled CTAs, card grids with lift-on-hover — and a dark-only palette
  hand-rolled from `zinc-*` utilities and literal `#0a0a0c`, all while half-importing
  `@protolabsai/design` and then ignoring it. Tailwind is dropped entirely; the site now
  loads the same two stylesheets protolabs.studio's own frontend does (`@protolabsai/design`
  for the `--pl-*` tokens + element base, `@protolabsai/ui` for the `.pl-*` components), so
  bumping those packages moves the design instead of the two drifting apart. Card grids
  became `.pl-row` lists, which scan far better at 39 plugins and 12 features than a
  two-column card wall; the changelog timeline is now the design system's `.pl-changelog`,
  the same DOM the React `<Changelog>` emits, with month grouping and the collapsed archive
  intact. The site also follows the reader's OS colour scheme now rather than forcing dark —
  light mode works because nothing is a hardcoded hex any more. Three latent bugs went with
  it: the hero badge and two form fields referenced `--color-surface-1`, a variable that was
  never defined in the Tailwind `@theme` map and so rendered transparent, and the roadmap and
  changelog carried hardcoded `#4ade80` / `#52525b` status colours that are now token-driven
  `.pl-badge` variants.

### Fixed
- **`auth: {token: ""}` now correctly disables bearer auth even when `A2A_AUTH_TOKEN`
  is set in the environment (#2691).** The three-state contract documented in
  `a2a_impl/auth.py` — `None` (unset) = fall back to env, `""` (explicitly empty) = auth
  off with no env fallback — was not honoured at boot or reload. Both paths collapsed any
  falsy config value to `None` before reaching `configure()` / `set_bearer_token()`,
  so an operator who set `auth: {token: ""}` to disable bearer auth would have it
  silently re-enabled by a stray `A2A_AUTH_TOKEN` in the environment. Fixed by changing
  `auth_token` / `federation_token` in `LangGraphConfig` to default to `None` (absent =
  unset) rather than `""`, and threading the raw config value through both the boot
  (`server/__init__.py`) and reload (`server/agent_init.py`) paths without collapsing it.

- **A finished background job now has a durable, tab-independent way to be noticed
  (#2692).** Two structural gaps let a completed `delegate_to(background=True)` job go
  unseen even though the backend delivered it correctly: the origin session had to
  already be an open browser tab for the live report to render (a machinery-driven
  session, or one aged out of the 50-tab local cap, silently got nothing but a vague
  toast), and `background.*` events shared one 128-slot process-wide replay ring with
  every other retained event on the instance — busy enough, and a `background.completed`
  could get evicted before a reconnecting client replayed it. The Background-agents
  panel's unread badge is now durable (localStorage-backed, keyed by job id) and
  reconciles from `GET /api/background` on every hydrate/reconnect — not just from the
  live bus event — so a missed push still surfaces once the panel next syncs. Its rows
  also gained a "jump to chat" action when the origin session is already an open tab.
  The fallback toast ("open the chat to read it") no longer lies when there's no chat to
  open — it now points at the panel. `background.progress` (the noisiest, most frequent
  background event) is now published unretained, and the shared replay ring grew from
  128 to 512 slots, both reducing eviction pressure on `background.completed`. The same
  silent-drop pattern in `ChatResumeWatch` (a scheduled/watch-triggered turn resuming
  into a session that isn't a local tab) now degrades to a toast instead of producing
  nothing at all.

- **The boot-phase timing test no longer fails at random on native Windows CI (#2703).**
  `test_timed_boot_phase_records_the_measured_elapsed_time` (until this change,
  `test_timed_boot_phase_records_into_the_sink`) slept 10ms and asserted the recorded
  duration was `> 0`, but under Python 3.12 `time.monotonic()` is backed by
  `GetTickCount64()` with ~15.6ms granularity — so on the Windows runner a 10ms sleep
  legitimately measures as exactly `0.0` and the assertion fails through no fault of the
  code under test. It now scripts the clock instead of sleeping, which makes it
  deterministic on every platform and tightens the assertion at the same time: the
  recorded value is pinned to the exact delta between the helper's two clock reads, so
  a `_timed_boot_phase` that recorded the raw end timestamp instead of the elapsed time
  would now fail, where `> 0` accepted it. Test-only — the helper is untouched.

- **Telemetry's `tool_calls` count was silently 2x inflated (#2705).** The turn loop
  legitimately announces a tool call twice — once early (empty args, so the console
  shows "running" immediately) and once more when the model finishes streaming (same
  id, full args, filling the card in). The executor counted both as separate calls,
  doubling `tool_calls` everywhere it's read: the Telemetry dashboard, the `/perf`
  chat snapshot, and cost/success correlations keyed on call volume. Now deduped by
  tool-call id — a real call counts once, regardless of how many times it's announced.

- **Console-created agents now receive their archetype's capability contract (#2713).**
  The backend has persisted and boot-checked `requires_tools` since #2315, but the
  console dropped the field at the type boundary — so an agent created from
  Settings ▸ Agents got no contract in `workspace.yaml`, and a persona could command
  tools the agent doesn't have without any warning (the original #2277 failure).
  The `Archetype` type, `createAgent` client, and NewAgentPanel now forward it, with
  tests pinning both halves of the seam (route → `workspace.yaml`, panel → request body).

- **The setup wizard now has the same bundle Configure step and runtime warning as the
  fleet new-agent picker (#2714).** Picking Cowork/Social/PM on first run used to install
  the archetype's bundle with no prompt for its declared MCP inputs or secrets, and no
  warning when the managed Python runtime the archetype needs isn't provisioned — both
  existed only in Settings ▸ Agents (#2041/#2186), while the wizard is the surface a new
  user actually hits first. The wizard's persona step now renders the same collapsible
  Configure form (skip = env-only seeding) and choose-time runtime notice, and the
  collected values ride the install exactly like fleet-create.

- **Installing a plugin that fails to load no longer toasts "enabled and live" (#2716).**
  The loader skips a plugin whose import fails and the reload still succeeds, so the
  install response said `reloaded: true` for code that never ran — the truth appeared
  only later as a red pill on the Installed table. `install_and_activate` now reads the
  post-reload roster and returns `load_errors`; the install dialog, the Discover
  one-click path, and the setup wizard all surface it, and
  `GET /api/plugins/installed` rows now carry the plugin's `error` too.

- **CLI uninstall of an enabled plugin now warns that a running server keeps it live
  (#2717).** `plugin uninstall` runs out-of-process: it scrubs disk and config, but a
  running server keeps the plugin's tools and routes serving until its next restart or
  reload — previously with no signal at all. The CLI now detects live servers on this
  data root (the #818 heartbeats) and, when the plugin was enabled, names the process
  and points at the console uninstall (which does the live teardown) instead of
  silently diverging.

- **Bundle-lifecycle corrections from the #2732/#2736 review findings (#2718).**
  `uninstall_bundle` now buckets honestly — a member uninstalled individually earlier
  reports as already-gone instead of "kept (shared)"; the shared ownership scan is one
  helper instead of three copies. `POST /api/plugins/bundles/{id}/update` answers 404
  for an unknown bundle (matching the DELETE route). In `ops.update_bundle`, an
  explicitly passed ref is never silently replaced by the newest semver tag, retire
  failures accumulate instead of overwriting each other, and every graph-rebuilding
  apply now runs off the event loop. The bundles guide separates what the CLI update
  shares with the console path from what is console-only, and the lifecycle smoke
  reads the declared enable set from the lock row and exercises the shared-member
  keep leg.

- **Devkit lifecycle-tool fixes from the #2735 review findings (#2719).**
  `install_plugin` no longer claims "fetched only (activate=False)" when the
  enable-reload actually failed; `_live_apply` catches a raising reload and returns a
  clean failure like its sibling `_live_enable`; `disable_plugin` refuses builtins
  instead of reporting a false "✓ disabled" for a plugin the loader keeps live;
  `uninstall_plugin`'s bundle branch routes through the shared `ops.uninstall_bundle`
  instead of reimplementing it; and the bundle-preview cache is keyed by (url, ref)
  so two refs of one bundle stop sharing a preview within the TTL. The
  single-plugin update success branch (release-tag → newest semver → purge → live
  reload) is now tested.

- **The plugin-install docs no longer claim a consent dialog that doesn't exist (#2720).**
  `operator_api/plugin_routes.py` asserted the console "flashes a one-time 'this runs
  code' confirm for unofficial sources" — no such dialog ships (the install dialog has a
  static warning; the Discover one-click path has none), and the devkit skill's
  "installing only fetches code, never runs it" is CLI-true but console-false. Both now
  describe the shipped behavior and point at the ADR 0071 D3 consent layer as the
  tracked follow-up, so the trust posture reads honestly until it lands.

- **Trust-matcher hardening from the #2733/#2735 review findings (#2721).** The
  prefix fallback in the trust matcher (and the byte-identical installer allowlist)
  widened every exact entry into a bare-`*` glob — acking `github.com/x/y` silently
  trusted `github.com/x/y-evil`, and an allowlisted `github.com/org` admitted
  `github.com/org-evil`; both now widen only at a path boundary (`/*`).
  `ssh://git@…` spellings normalize correctly (scheme and `git@` strip together), a
  string `"false"` for `plugins.trust_unverified` no longer reads as *enable* the
  don't-ask switch, and `peek_bundle` refuses a manifest member id that isn't a
  single safe path component — a `..`-bearing id could previously resolve outside
  the peek's temp directory before the fetch wrote.

- **Bundle/archetype docs truth pass + ADR 0100 ratifies the archetype system
  (#2722, #2723).** ADR 0040 is amended to describe the shipped install behavior (the
  console/ops and workspace-create paths enable and seed — `config:`/`mcp:`/`secrets:` —
  the CLI stays fetch-only) and the full manifest field set; `docs/guides/fleet.md` no
  longer describes the two-row fallback as the shipped six-row catalog and documents
  every `archetype:` field; the reference bundle template gains the missing `secrets:`
  block; the operator-API reference gains the preview endpoint; version-coherence's
  rotted line refs now cite functions. Terminology is settled in one place (ADR 0100 +
  the fleet glossary): a **bundle** is the mechanism, a **stack** is a published
  archetype-carrying bundle repo — CLI/devkit strings that said "stack" for the
  mechanism (one of which also falsely promised CLI install enables) now say bundle.

- **`list_verifiers` no longer implies core types are usable by `set_goal`/`create_watch`
  (#2744).** Both tools only ever accept a plugin-registered verifier name; core types
  (`command`, `test`, `ci`, `data`, `llm`, `plugin`) are operator-only. The tool's output
  now says so explicitly, instead of letting the model try a core type and hit
  `unknown plugin verifier` — found live when an agent read `ci` in the list and tried
  it on `create_watch`.

- **A `wait()` rescheduled mid-fire no longer gets silently deleted (#2749).** The
  `wait` tool reuses one stable `wait:<session>` job id so a second `wait` in the same
  session supersedes the first, but the scheduler's post-fire cleanup deleted by that
  id alone — if the resumed turn called `wait` again *before* the original POST
  returned, the fresh reschedule shared the just-fired id and was deleted seconds
  after being created, breaking the "retry, then back off longer" chain. Found live
  when a protoEngineer wait-retry-wait(longer) backoff silently stopped after the
  second `wait`. The post-fire delete is now scoped to the exact row (`next_fire`)
  that actually fired, so a mid-flight reschedule survives.

- **`read_file` is now line-addressed, and `search_files` supports regex + context
  lines (#2755).** `read_file(project, path, offset, limit)`'s `offset`/`limit` are
  now LINE numbers, the same addressing `search_files` already returns — a hit at
  `file.py:342` now reads straight in as `read_file(path="file.py", offset=320,
  limit=60)` instead of paging through 50,000-character chunks from the start of
  the file guessing where line 342 falls. Leaving `limit` unset still returns the
  whole file (or whole remainder) in one call whenever it fits the char safety
  cap, regardless of line count — a small file's old one-call guarantee, unchanged.
  `search_files` gained `regex` (opt-in — literal substring stays the default, with
  a real match timeout since a model-supplied pattern has no other bound against
  catastrophic backtracking) and `context_lines` (grep `-C`-style, merges
  overlapping windows, separates non-adjacent ones with `--`, and is itself capped
  on total output, not just match count). Found via a live-session transcript
  audit: a 100KB file got read twice, non-consecutively, in one turn, each call
  re-truncated to the identical first 50K chars, for zero new information.

- **A2A skill seam hardening — validated registration, owned ids, live served card (#2757).**
  A plugin skill spec missing `description` no longer passes registration and then
  `KeyError`s the boot-time card build — `register_a2a_skill` (and YAML `a2a.skills`)
  now require `id`+`name`+`description` and reject duplicate ids with an attributed
  warning. Cross-plugin skill-id collisions are rejected visibly at load (first wins,
  ownership recorded per skill), and the served agent card now follows hot reloads
  instead of staying frozen at its boot-time build while the structured finalizer
  moved on. (#2754)

## [0.136.0] - 2026-08-13

### Added
- **The federation token (ADR 0066) is now manageable from Settings, and rotates live (#1504).**
  `auth.federation_token` — a second A2A credential scoped to `/a2a` + `/v1`,
  denied the `/api` operator surface — was config/env-only since it shipped:
  no Settings field, and a saved edit wouldn't take effect until a full
  restart (the reload path updated the bearer token but never the federation
  one). Both gaps are fixed: it's now a redacted secret field alongside
  `auth.token`, and a Settings-drawer save rotates it live like every other
  credential. The delegate config panel's `auth.token` field now also points
  operators at it directly — when a peer has a federation token, use that
  value instead of their operator token. `docs/reference/configuration.md`
  gained a concrete rotation runbook (issue/set/rotate/verify) — setting a
  federation token protects nothing until each peer is individually rotated
  onto it; the operator bearer keeps working everywhere until then.

- **A structured, artifact-aware chat-bundle export now exists alongside the Markdown one (#2680, #2681).**
  `graph/chat_bundle.py` builds a versioned JSON manifest of a thread — ordered text/tool-call
  parts mirroring the console's own message model, with inline artifact content (HTML/SVG/
  Mermaid/React/Markdown) resolved via a new `plugins.artifact.resolve_for_bundle` seam.
  Binary file-artifacts stay a placeholder (kind/size/filename, never bytes) in this slice.
  Packaged as a zip (`manifest.json` + a `REVIEW.md` disclosure of redactions and any
  artifacts not fully included), the same shape as the ADR 0091 agent-snapshot bundle. This
  is the foundation issue #2179 (P2's hosted viewer) needs; nothing consumes it yet by
  design — the pre-publish review UI and publish client are separate, still-open issues.
  See ADR 0099.

- **list_verifiers tool (#2686).** Agents can now discover registered goal/watch verifiers before attempting set_goal or create_watch.

### Fixed
- **The self-building loop now closes in the packaged desktop app (#2636).**
  Scaffolding a plugin and running its tests worked on a source run and failed on
  desktop, which is the build a new user actually downloads. Two causes, both
  predicted by ADR 0096 §8 and left open: `test_plugin` resolved a valid managed
  interpreter and then died on a bare `No module named pytest` — the app bundle
  ships no pytest and the managed runtime's baseline is the document stack
  (docx/xlsx/pptx/pdf) — while `coder`'s verifier still returned the pre-ADR-0094
  flat refusal that ADR 0096 had already declared superseded. Both spawners now
  share one `infra.python_runtime.pytest_interpreter()`, which checks the managed
  runtime for pytest *before* spawning so the failure names its own remedy instead
  of leaking the child's traceback; `plugin-devkit` and `coder` declare
  `pytest>=8`, so the Plugins panel's **Install deps** button can satisfy it. A
  frozen build still never falls back to a discovered system Python.

- **The desktop self-building loop actually runs its tests now (#2638).**
  #2637 declared `pytest` as a plugin dependency so the Plugins panel could install it.
  Built as a real PyInstaller sidecar and driven end to end, that could never work:
  PyInstaller bundles pytest transitively, so the dep gate saw it importable in the
  host and reported it satisfied — the console then hid the **Install deps** button
  (it renders only when something is missing) and the CLI printed
  `✓ installed 1 dep(s)` having installed nothing, while the managed runtime, the only
  interpreter that can ever spawn it, stayed empty. Tightening the gate wasn't the
  answer either: a bundled dep satisfying `requires_pip` is deliberate (ADR 0058 D2 —
  it's why cowork works on desktop unmodified). And pytest alone was never enough — a
  scaffolded suite also needs `pyyaml` and `langchain-core`, because every real plugin
  does `from langchain_core.tools import tool`. The test runtime now installs into the
  managed Python on first use, only the missing pieces, so provisioning stays lean for
  the majority who never author a plugin. Verified on a frozen build: a plugin
  scaffolded by the packaged binary runs its suite green.

- **An agent is told when its own tools change (#2640).**
  Capabilities appear at runtime here — a scaffolded plugin, an `enable_plugin`, a
  plugin update, an operator settings change all rebuild the graph with a different
  toolset — but nothing told the running agent, so one mid-session kept operating on a
  stale belief about what it could do. The failure is worse than a missed opportunity:
  the agent **refuses work it is capable of**, politely and with reasoning, which reads
  to an operator as a missing feature rather than a stale toolset. Observed live: a tool
  was deployed and bound, with a description matching the need almost word for word, and
  the agent still reported it didn't exist — it had concluded that before the deploy and
  written the conclusion into its friction log, so the stale belief sat in its own
  context reinforcing itself. The bound toolset is now recorded at every graph build, and
  a change leaves a one-shot note for the next turn naming what appeared or vanished, and
  telling the agent to re-check any conclusion it reached for lack of a tool. Silent on
  boot and silent when nothing changed, and it composes with the knowledge/skills block
  rather than replacing it.

- **Multi-turn tool-call conversations no longer 400 in DeepSeek-style thinking mode (#2642).**
  Once a tool call happened anywhere in a conversation, every subsequent turn failed
  with `The reasoning_content in the thinking mode must be passed back to the API`.
  `langchain_openai`'s outbound message builder silently drops the model's
  `reasoning_content` — a non-standard field it explicitly says integrations must
  handle themselves — so the assistant messages protoAgent sent back on the next
  turn were missing the field DeepSeek requires once thinking has been used.
  `_ReasoningChatOpenAI` now restores it on every assistant message when thinking
  is enabled, including an empty string for a turn that captured no reasoning of
  its own (an absent key still 400s; only a present key, even empty, satisfies
  the API). Gated on the existing `thinking` config flag, so this only affects
  models that set it — every other provider is unchanged.

- **Inbound A2A delegate turns now appear in the receiving agent's Activity feed (#2644).**
  protoAgent stamps peer-delegation provenance independently of tracing and preserves the
  receiver's real A2A context, task ID, stimulus, terminal state, and failure detail, so a
  successful or failed sister-agent handoff no longer leaves the receiving console looking idle.

- **The bundle template's pin-bump PRs no longer pile up unverified (#2645).**
  `verify-bundle.yml`'s scheduled `bump` job opened a fresh `bump-pins-<date>` PR every
  week with the repository `GITHUB_TOKEN` — which GitHub never auto-runs a `pull_request`
  workflow for, so the promised `verify` check never started and each week's PR just
  joined the unverified pile (17 open across the four published stacks). The job now
  pushes one stable `bump-pins` branch and updates that stack's single open pin-bump PR
  in place instead of opening a duplicate, and — since GITHUB_TOKEN-authored PRs are an
  explicit-approval model here, not a bug, given this template has no GitHub App/PAT
  provisioned — polls after pushing and fails loudly (PR comment, `needs-approval` label,
  red job) if the run it just queued sits `action_required` with nothing started, so an
  unapproved candidate reads as a maintenance failure instead of rotting silently. The
  contract is written down in the template's own README. Template-only: migrating the
  four already-published stacks and reconciling their existing backlog is tracked
  separately on the issue, each being its own repo.

- **`python -m server --config <path>` now fails loudly instead of silently ignoring the path (#2647).**
  The flag was declared but never consumed by any startup path — every boot,
  including `--setup`, silently used the default instance's config and
  credentials regardless of what file was given. An operator who deliberately
  scoped a model, delegate roster, or network policy via `--config` could
  unknowingly boot with the default instance's settings instead, with no
  indication anything was wrong. `--config` now exits with a clear error
  pointing at `PROTOAGENT_HOME` — the existing, ADR-0065-sanctioned mechanism
  for running against a genuinely isolated instance's config, secrets, and
  data stores, which already fully covers the need. A separate config-path
  override was deliberately not implemented: it's architecturally the same
  shape as `PROTOAGENT_CONFIG_DIR`, which ADR 0065 removed after a
  double-scoping bug once deleted a live instance's gateway key.

- **The "isolated" live-smoke run no longer probes real local protoAgents (#2651).**
  `scripts/live_smoke.py` sets a scratch `PROTOAGENT_HOME`/`PROTOAGENT_BOX_ROOT` for its
  spawned server, but that only isolates the instance/box config tiers — the server's
  normal at-boot fleet-discovery sweep still port-scanned `127.0.0.1:7860-7910`
  regardless, so on a dev host with a real protoAgent on 7870 the "isolated" smoke
  contacted it (`GET /.well-known/agent-card.json` + `boot sweep cached 1 peer(s)` in the
  smoke's own log). The smoke script now sets `PROTOAGENT_DISCOVERY_DISABLE=1` on its
  spawned server, and `graph/fleet/discovery.py`'s `start_boot_sweep()` — the entry point
  server boot calls — skips scheduling the sweep entirely when it's set. Scoped to that one
  boot-time entry point: manual discovery (`GET /api/fleet/discover`) and normal
  (non-smoke) server boot are unaffected.

- **A completed tagged Desktop Build now refreshes the public download page (#2655).**
  Marketing redeploys only after the desktop matrix and updater fan-in succeed, so a release
  no longer remains stuck on the previous macOS and Windows installers.

- **The markdown smoke e2e no longer races Streamdown's code-block highlight swap (#2659).**
  `markdown-smoke.spec.ts`'s multi-line fence guard read a TypeScript `<pre>` element's
  computed `white-space` and `innerText` as two separate Playwright round-trips. Streamdown
  renders that block through a Suspense boundary — a synchronous raw fallback `<pre>`, swapped
  once for a freshly-mounted node from the lazily-loaded highlighter, which then updates its
  own children in place as Shiki's async tokenizer resolves. A read landing on that swap could
  catch an already-detached node: `getComputedStyle` reporting an empty `whiteSpace`, or
  `innerText` collapsing every source line onto one, on an otherwise-unchanged base (3/5 under
  `--repeat-each=5 --workers=1`) — noise that already cost real reviewer time on unrelated PRs.
  The spec now polls inside the page for the first render carrying real (non-fallback) Shiki
  token colors, waits for it to read identically three times in a row, and takes
  `whiteSpace`/`innerText`/span-count from that single coherent snapshot — no more reads
  split across the node-replacement boundary. Verified locally with 40 serial repeats green,
  and confirmed the guard still fails red against both a reverted `white-space: pre` and a
  reverted line-span `display: block`.

- **Goal and Watch form data verifiers now resolve relative paths under the managed workspace (#2660).**
  UI/chat checks see the same files as structured filesystem tools; direct operator specs keep legacy CWD-relative behavior, while untrusted chat cannot use absolute or escaping paths.

- **Native Windows CI now retries one transient Node syntax-check timeout (#2662).**
  Persistent stalls and JavaScript syntax failures still fail the bundled plugin-view sweep.

- **Chat answers stream in smaller, more frequent blocks (#2672).**
  The console's live answer text was batched to a 240-character flush threshold,
  raised there in the past over concern that more frames would race the A2A
  teardown-cancel grace window. Re-tested that concern directly (30x through the
  real streaming path at the original, much lower threshold, with an 11KB answer
  forcing ~480 frames) and found no teardown-grace warnings and no dropped text —
  the grace window only fires on a genuinely hung producer, not frame count.
  Lowered the threshold to 60 chars so the console fills in noticeably smaller,
  more frequent blocks instead of ~40-word jumps.

- **`model.max_iterations` now actually caps the agent loop (#2679).** The
  Settings field ("Max tool iterations — Hard cap on the agent loop per
  turn") was dead: the real LangGraph `recursion_limit` was hardcoded at
  `200` in two places in `server/chat.py`, and the non-streaming `/api/chat`
  path set no limit at all, silently falling back to LangGraph's own
  default. All three call sites now read `model.max_iterations`, whose
  default is bumped `50 → 2000` (≈250 tool calls/turn) so nobody's
  effective limit regresses now that the field is live. Raising or lowering
  the per-turn ceiling is a Settings edit, not a code change. Existing
  installs get a one-time, automatic migration on their next boot: since the
  field was previously unused, a live config still holding the old dead
  default of `50` is corrected to `2000` — an install that had already
  hand-picked some other value is left untouched.

- **Goal/watch tools hidden when no verifiers registered (#2686).** `set_goal` and `create_watch` no longer appear in the agent toolset on instances with no plugin-contributed verifiers, removing the guaranteed-fail trap.

### Security
- **A2A delegate calls now trust what the OS trusts, not just certifi (#2643).**
  httpx's default verification never discovered the OS trust store — a private CA
  an operator installed there (Windows cert store, macOS Keychain, a Linux distro
  bundle) was invisible to it even though the OS's own HTTP clients trusted it
  fine —
  breaking A2A delegate probe/dispatch to a peer behind an internal CA or a
  TLS-terminating proxy, hit hardest on the packaged Windows desktop. Delegate
  calls (and every other outbound httpx client in the process) now verify through
  the native OS trust APIs via `truststore`, matching what Chrome/PowerShell
  already accept on the same machine. A chain the OS itself doesn't trust still
  fails closed — no `verify=False` path was introduced.

- **Locked web dependencies with active advisories have been refreshed (#2649).**
  The browser console's Markdown and diagram dependency chain and the shared CSS
  build path now resolve to patched Mermaid, DOMPurify, PostCSS, and Nano ID
  releases. The test-only Undici lock also advances to its patched release;
  package ranges and runtime behavior are unchanged.

### Docs
- **A Windows operator now has one guide covering install through recovery (#2656).**
  The packaged Windows build was public with no single task-oriented page: the
  source-checkout tutorial is Unix-flavored, the console guide is architecture not
  procedure, and the download page's three steps link nowhere. The new
  [Windows desktop app](/guides/windows-desktop) guide covers the supported
  system + how to verify the release asset, the current unsigned/SmartScreen posture,
  how the desktop shell / frozen server / console at `127.0.0.1:7870/app/` relate, where
  writable config/data/logs live (`%APPDATA%\studio.protolabs.protoagent\`), what
  uninstall/reinstall "keep data" actually means, update behavior and how to confirm your
  running version, when the managed Python/Node runtimes are needed, and safe recovery
  steps ordered from a restart up through a non-destructive backup before any destructive
  reset — routing to the existing deep-dive pages instead of duplicating them. Linked from
  both the Windows download block and the docs sidebar.

## [0.135.0] - 2026-08-12

### Fixed
- **The A2A agent card now describes the credentials the server actually accepts, and
  multiple credentials are genuinely alternatives (#2620).** A bearer-gated agent
  advertised `X-API-Key` in `/.well-known/agent-card.json` even with no API key configured,
  so a standards-driven A2A client that picked the advertised scheme got 401 from a healthy,
  correctly configured agent — it reads as offline, and blocks fleet onboarding and
  third-party interop. Investigating it surfaced a second, larger problem: the guard checked
  a legacy API key and a bearer in sequence, which made them a conjunction. With both
  configured, **neither credential alone was accepted** — so enabling the legacy key
  silently broke every bearer client. Nothing had ever specified that; the two checks were
  added years apart, and it survived only because protoAgent's own internal callers always
  send both. Configured credentials are now what they always read as: **alternatives** —
  present any one. The card is derived from the guard that enforces them rather than from a
  parallel re-reading of config and env, which is what let the two drift apart in the first
  place; the same single source now backs the scheduler and console self-invocation paths,
  replacing four separate derivations with different precedence.
- **Note for anyone who set both `A2A_AUTH_TOKEN` and `<AGENT>_API_KEY`:** requests that
  previously had to carry both headers now succeed with either. This is a deliberate
  loosening of an accidental restriction — if you relied on both being required, treat the
  two credentials as one and remove the one you don't want honoured.

- **A fleet member that picks its own model no longer inherits an incompatible provider
  from the host.** The config cascade merged `model.name` and `model.provider` as
  independent fields. That was survivable while every provider spoke the same
  OpenAI-compatible dialect — a mixed pair still built — but native OAuth (ADR 0097) ended
  it. When a host switched to `anthropic-oauth`, the host layer published that provider,
  and every member that had overridden only `model.name` with a gateway alias inherited the
  OAuth provider on top of its own model id. `anthropic-oauth` + `protolabs/smart` cannot
  be built, so those agents crash-looped at boot with "isn't running" in the console, while
  members that overrode neither key kept working — which is why it presented as random. The
  model identity now travels as one decision, the same coupling reset already used: an
  agent that names either key supplies both, and the one it leaves out comes from the App
  defaults rather than from a provider it never chose. `api_base` still inherits — it's an
  endpoint, not an identity, and members legitimately override the model while using the
  box's gateway. The error a bad pair produces now says the two halves came from different
  layers and to set them together, instead of just naming the model id.

### Removed
- **The Hermes ACP runtime preset is deprecated (#2633).** `protoagent hermes` and
  `agent_runtime: acp:hermes` made Hermes Agent the *brain* of a protoAgent instance — an
  experiment that ACP **delegates** superseded, where an external agent is a worker the
  native runtime dispatches to, so goal continuations, telemetry and the whole plugin
  surface stay intact. Hermes no longer appears in the Settings runtime picker, the setup
  wizard, `runtime list` or `/api/acp-agents`, and its guide is gone from the docs.
  **Existing installs keep working**: an agent already on `acp:hermes` still resolves its
  launch command and boots, and selecting it still succeeds — it now prints a deprecation
  warning naming ACP delegates as the replacement. Removal will be a separate change.

### Deprecated
- **The legacy `<AGENT>_API_KEY` credential is deprecated (#2632).** It still authenticates
  exactly as before — dropping a credential someone deployed against would lock them out of
  their own agent — but setting it now logs a warning once at startup naming the
  replacement. Use the bearer token (`auth.token` in `langgraph-config.yaml`, or
  `A2A_AUTH_TOKEN`): it is settable from Settings and the wizard, rotatable at runtime,
  routed through `secrets.yaml`, and it is what every protoAgent client already sends. The
  API key had none of that — env-only, invisible in the console, unrotatable — which is the
  worst state for a credential to be in. Removal is tracked in #2632; the guides no longer
  present it as a current option.

## [0.134.1] - 2026-08-12

### Added
- **The friction ledger has a console surface — a rail icon, not just an API (#2595).**
  `friction_review` and `GET /api/friction` (#2607) turned the write-only ledger into
  something *readable*, but nothing rendered it — the entries a friction-enabled agent
  records still sat unread unless an operator called the tool or curled the route by
  hand. The `friction` plugin now declares a console view (`Settings`-adjacent rail icon,
  ADR 0026): grouped, de-duplicated entries newest-first, a harness/model kind filter,
  severity/source/tool badges, an expandable detail per entry, and a per-browser
  dismiss/restore marker for lightweight triage. Opt-in, same as the plugin itself
  (`plugins: { enabled: [friction] }`) — nothing changes for instances that don't enable
  it. A fleet-wide rollup and "file as issue" are still deliberately deferred follow-ups.

### Fixed
- **Chat code blocks render their line breaks again (#2612).** A multi-line fence was
  collapsing onto one line while keeping its indentation and its syntax highlighting.
  Re-asserting `white-space: pre` (#2546) fixed only half of it: that preserves whitespace
  which exists in the DOM, and streamdown emits one `<span>` per line with no newline text
  nodes between them, so the lines flowed together regardless. Each line span is now a
  block. The e2e fixture gained an indented block and the guard now asserts the rendered
  text keeps its line breaks — the previous assertions (computed `white-space`, span count)
  passed whether or not the bug was present.

- **The HITL response textarea now sends on Enter, matching the chat composer (#2614).**
  The free-text box shown for `ask_human` interrupts treated Enter as a plain newline
  insert with no keyboard way to submit — the operator had to reach for the mouse and
  click Send every time. It now follows the same convention as the main chat composer:
  bare Enter submits the response, and ⌘/Ctrl+Enter inserts a newline instead.

- **The friction list shows the newest signal first, even on a coarse clock (#2616).**
  Entries recorded in one burst share an identical timestamp on Windows, and a stable
  sort then handed back read order — oldest first, the inverse of what the grouped view
  promises. Bursts are the normal case here (the same rough edge hit five times in a
  turn), so ties now break on ledger position: the ledger is append-only, so a later
  record is the newer one. This also unbreaks `Windows tests (native)`, which had been
  red on `main` since the friction log landed.

- **The published PyPI wheel now ships `plugins/`, so every ACP-backed turn works on a real install (#2624).** `runtime/acp_runtime.py` imports `AcpClient` from `plugins.coding_agent.acp_client`, but the wheel's asset list never actually included the `plugins/` tree — only source/editable installs worked, because the repo root stays on `sys.path` there, masking the omission. A clean `uv tool install protolabs-agent` could boot and serve, but the first Hermes/Codex/Claude-style ACP turn failed with `ModuleNotFoundError: No module named 'plugins'` before the configured agent even launched. Fixed by mirroring the desktop sidecar's already-correct `("plugins", "plugins")` bundling, plus a regression test pinning the invariant so this class of asset-list omission can't silently ship again.

- **The published PyPI wheel now ships the docs corpus, so `docs_search`/`docs_read` and the Docs view work on a real install (#2626).** The `docs` plugin's corpus reader (`plugins/docs/corpus.py`) resolves `docs/` beside the installed `plugins/` tree, but the wheel's asset list never included it — only source/editable installs worked, same root cause as #2624's `plugins/` omission. The reader degrades silently (an empty corpus, not an error) rather than failing loudly, so this shipped invisibly. Fixed by bundling the five Diátaxis sections + ADRs the corpus actually reads (`docs/tutorials`, `docs/guides`, `docs/reference`, `docs/explanation`, `docs/adr`) — deliberately not the whole `docs/` tree, which would also sweep in the internal `docs/dev/` and the gitignored, potentially large built VitePress site (`docs/.vitepress/dist`) if a release happened to be cut on a machine that had built the docs site locally.

## [0.134.0] - 2026-08-12

### Added
- **Project onboarding config (#2555).** `onboarding` config section: `enabled`, `root`, `allow` globs, `write_default` — the operator-consented space for agent-driven project onboarding.
- `onboard_project` tool: clone a GitHub repo and register it as a managed project, bounded by the operator-consented `onboarding` config section. Refusal paths name the bound crossed.

- **Mix a gateway, a Claude subscription and a ChatGPT subscription across model slots.**
  Every slot that takes a model name — fallbacks, the auxiliary model, compaction, goal
  evaluation, subagents, and your `/model` favorites — can now name its own provider:
  `anthropic-oauth:claude-sonnet-5`, `openai-codex:gpt-5.6-sol`, `gateway:protolabs/coder`.
  Slots used to inherit whatever `model.provider` was, so holding all three accounts
  still meant running everything on one of them. Now you can put Claude on review, Codex
  on code and the gateway on cheap bulk work at the same time — and because the `/model`
  quick-switch resolves favorites the same way, a pinned favorite from another provider
  switches the chat's lane too. Existing unprefixed slot values are untouched.

- **The model dropdowns now offer every provider you're signed in to.** Slot settings —
  fallbacks, the auxiliary model, compaction, goal evaluation and your `/model`
  favorites — used to list only the currently-configured provider's models, so holding a
  gateway key *and* a Claude subscription *and* a ChatGPT subscription still meant
  picking from one of them. They now list all three, each entry naming its lane
  (`anthropic-oauth:claude-sonnet-5`), which is also what makes a saved choice survive a
  later provider switch. A lane you can't use yet is shown with the reason rather than
  quietly left out, and one expired credential no longer blanks the whole list — the
  lane that's down says so and the others still work. `model.name` is unchanged: the
  main model still belongs to `model.provider`.

- **Pick any signed-in provider's model straight from the chat.** The composer's model
  menu and `/model` used to offer only the configured provider's models; they now span
  every lane you're signed in to, so you can put this tab on Claude while the agent's
  default stays on the gateway. The menu groups models under a heading per account with
  a divider between, and each row is just the model name — the routing prefix stays
  behind the scenes, so favorites pinned from different providers read as one clean
  list. `/model claude-sonnet-5` finds the right one without you typing the prefix.

- **Recorded friction is readable at last — `GET /api/friction` (#2595).** The `friction`
  plugin gives agents `record_friction`, and they use it: the entries land in
  `friction.jsonl` and, until now, nothing ever read them. One agent's log held two
  filable defects and a capability request repeated five times, all sitting unseen for
  weeks — found only because an operator eventually opened the file by hand. The text the
  plugin itself writes is *"candidate for a first-class tool"*, so it was always meant to
  feed a triage loop that didn't exist. The new endpoint returns entries newest-first and,
  by default, **grouped**: five identical escape-hatch reaches read as one item with
  `count: 5`, which is what makes the list actionable rather than noise. Each group keeps
  the worst severity seen and a first/last-seen window. The ledger is also **bounded** now
  (it grew forever), and it is written and read as UTF-8, so a summary quoting an error
  with an em dash or a non-ASCII path survives on Windows.

### Fixed
- **Marking a draft PR ready for review now triggers the Windows tests job (#2455).** The
  `checks.yml` `pull_request` trigger had no `types:` key, so it only fired on `opened`,
  `synchronize`, and `reopened` (GitHub's implicit defaults) — not `ready_for_review`. A PR
  whose last push happened while it was a draft would permanently show "Windows tests (native) →
  skipping", and once #2455 promotes that job to required, such PRs could merge with the
  required check never having run. Fixed by adding `types: [opened, synchronize, reopened,
  ready_for_review]` to the trigger; the existing draft guard and `concurrency` cancel-in-progress
  are unchanged.

- **Agent names with non-ASCII characters no longer render as mojibake (#2520).** Saving an
  agent called `PA Windows Lifecycle Café` stored the name correctly, then showed
  `PA Windows Lifecycle CafÃ©` in the header, the agent switcher and the Fleet API. The
  config file was fine; the *read* was not — `graph/config.py` opened it in text mode
  without naming an encoding, so on Windows it was decoded with the locale code page
  (CP1252) and `Café` became `CafÃ©` in memory. That string then flowed into the fleet
  label and was re-encoded on the way out, producing the double-encoded bytes an operator
  finally traced by hand. The read now names UTF-8, as does the sibling read of
  `secrets.yaml` — where the same defect would have silently mangled a credential
  containing a non-ASCII character into one that no longer matches.
- **The whole class is now a build failure, not a bug report.** Text IO without an explicit
  encoding has produced four separate user-visible defects, each fixed one call site at a
  time. Ruff's `unspecified-encoding` rule is enabled repo-wide and the remaining 36 call
  sites are fixed, so the next one fails CI instead of shipping to a Windows user.

- **`protoagent config set` no longer writes credentials in plaintext into the tracked
  config YAML (#2575).** Setting `auth.token` or `model.api_key` with no server running
  took a disk-only path that merged the value straight into `langgraph-config.yaml` and
  never created `config/secrets.yaml` — the opposite of the documented contract that those
  keys are *"never written to the tracked config YAML"*. The belt-and-suspenders half
  ("every config save also strips any secret keys the main YAML might still carry, so a
  checkout converges to secret-free") never fired on this path either, so a hand-seeded key
  stayed inline through every later save. The headless write now takes the same route a
  live server takes: secret-typed keys are split out and merged into the owner-only (0600)
  `secrets.yaml`, the main document is scrubbed of any secret it still carried, and the
  command reports which file each key actually landed in instead of always claiming
  `config.yaml`. A blank secret value still means "leave the stored one unchanged", and now
  says so rather than reporting a write it didn't make.

- **`protoagent up` and `protoagent status` no longer mistake a stranger's listener for
  your server (#2576).** Both decided "is this instance's server running?" with a bare TCP
  probe of the port, so any unrelated process holding it — a container publishing 7870, a
  hand-started `serve` — made `up` print `already running` and exit **0 without starting
  anything**, and `status` report `running … (pid ?, v?)` with no pidfile on disk at all.
  Following the CLI guide on a host that already used the port therefore looked like a
  clean install right up until the first turn. Both verbs now agree with `down`, which
  already got this right: running means *the pidfile names a living process*. A foreign
  listener makes `up` refuse with a non-zero exit and `status` report stopped, each saying
  plainly that the port is held by something it didn't start. `up` also refuses to launch a
  second server when this instance already tracks one on another port, rather than
  clobbering the pidfile and orphaning the first; and `status` distinguishes a server that
  is still binding (live pid, port not yet accepting) from one that is down.

- **`/v1/chat/completions` now reports a failed turn as an HTTP error instead of a
  successful completion (#2578).** When a turn raised, the endpoint answered **HTTP 200**
  with the exception text as the assistant's `content` and `finish_reason: "stop"` — no
  `error` object, no non-2xx status. Every OpenAI SDK client therefore counted a hard
  upstream failure as a real answer: a gateway rejecting the API key came back as an
  assistant message reading `**Error:** Error code: 401 …`, and anything downstream
  (an eval harness, a LiteLLM route, OpenWebUI) stored or acted on it as the model's reply.
  Failures now return an OpenAI-shaped `{"error": {message, type, code, upstream_status}}`
  body with a real status: a rate limit is **mirrored as 429** so client backoff works,
  any other upstream HTTP failure becomes **502** — deliberately not the upstream status,
  because a 401 from this endpoint already means *your protoAgent bearer is bad* and
  echoing one would send callers to re-check the wrong credential — and a fault with no
  HTTP status of its own is a **500**. `upstream_status` carries the original code. The
  streaming path is covered too: the turn completes before the stream opens, so a failed
  streaming request gets the same status rather than an SSE frame that reads as success.
  The console is unchanged — it still renders the error inline as a chat bubble.

- **`protoagent model use` no longer invents an API key for a gateway that needs a real one
  (#2579).** Pointing at any endpoint without `--key` wrote the literal placeholder
  `api_key: "local"` — correct for a keyless server on this machine, actively harmful for a
  remote one. Because the value was non-blank, `protoagent setup` then read it as "a key is
  configured", printed `setup: complete` and wrote `.setup-complete`; nothing warned at any
  point and the first user turn failed with a 401. Following the CLI guide against a keyed
  gateway therefore produced an agent that called itself fully set up and could not answer a
  single message. The placeholder is now written only for a loopback base URL. For anything
  else with no credential in the config, `secrets.yaml`, or `OPENAI_API_KEY`, the endpoint
  is still saved but the command says plainly that no key is configured and how to set one —
  and `setup` fails with an actionable reason instead of certifying a broken agent. Re-running
  `model use` against a remote endpoint also clears a placeholder left behind by the old
  behavior, so an instance already in this state heals itself.
- **`protoagent model use --key` and the Hermes preset store the credential in
  `secrets.yaml`, not the tracked config YAML (#2575).** Both wrote a real API key inline
  into `langgraph-config.yaml` — the file operators fork, export and check in. It now goes
  to the owner-only (0600) overlay like every other secret.

- **An agent on a Claude subscription no longer dies when its own coders refresh the login
  (#2582).** The OAuth access token was resolved once, at graph build, and frozen into the
  client for the life of the process. Anything that rotated the shared Claude credential
  then broke the agent permanently — and this setup rotates it *by doing its job*: a
  board/PM agent that dispatches Claude Code coders shares their keychain login, and each
  coder run can refresh it, invalidating the access token the agent is still presenting.
  Every model call came back `401 … OAuth access token has been revoked`, while
  `GET /api/config/oauth-status` — which reads the credential store live — went on
  reporting a healthy signed-in session. The introspection surface and the running graph
  disagreeing is what made this so hard to diagnose; the only cure was restarting the
  agent. The credential is now re-read before each request and pushed into the live client,
  so a rotation is picked up within seconds and no restart is needed. Resolution is cached
  briefly, because reading the store can shell out to the macOS Keychain and a turn makes
  tens of calls; a transient failure to read it keeps the working token rather than taking
  down a live turn.

- **Purging a fleet member no longer fails with a bare 500 after already stopping it
  (#2583).** `DELETE /api/fleet/{member}?purge=true` stops the member and then deletes its
  workspace. On Windows a member's own files can stay open for a moment after its process is
  gone — handles are released asynchronously, and a scanner can hold them a little longer —
  so the delete lost that race and `rmtree`'s error escaped as a generic 500. The member was
  by then stopped, its port freed and its record cleared, with only the workspace left behind:
  a terminal-looking failure on a half-completed destructive operation, which reproduced in
  three separate Windows test rounds and always succeeded when repeated by hand. The delete
  now retries with a short backoff (and clears the read-only bit, which makes `rmtree` raise
  on Windows even when nothing holds the file), so the ordinary case just works. If the tree
  genuinely won't go, the endpoint answers **409** with a message naming exactly what
  happened — the agent is stopped, the workspace remains, retrying finishes it — instead of a
  status that says only "something failed".

- **`POST /api/restart` restarts the server on Windows instead of killing it (#2585).** The
  endpoint answered `202 Accepted` and logged that it would drain and re-exec, then the
  process vanished and nothing came back — port free, no server, recovery only by launching
  the binary by hand. The drain was triggered by the server signalling itself
  (`os.kill(getpid(), SIGINT)`), which is correct on POSIX and fatal on Windows: `os.kill`
  there delivers only `CTRL_C_EVENT`/`CTRL_BREAK_EVENT` as signals and turns every other
  value, `SIGINT` included, into `TerminateProcess`. So the server was killed outright at
  exactly the point it was meant to wind down, its serve call never returned, and the
  re-exec that follows it never ran. The re-exec logic itself was fine and already
  frozen-aware, which is what made this look mysterious. The restart now asks the live
  uvicorn server to exit — what uvicorn's own signal handler does, minus the signal — so
  the drain, the graceful timeout and the re-exec behave identically on every platform, and
  the path can be exercised in tests off Windows for the first time.

- **`write_file` writes the line endings it was asked for, and `read_file` no longer hides it
  when it didn't (#2586).** Both tools used Python's text mode, which rewrites `\n` to
  `os.linesep` on write and `\r\n` back to `\n` on read. On Windows those cancelled out, so an
  agent told to write LF-only content and verify it read its own request back verbatim and
  reported a byte-exact match — while every requested `0A` had become `0D0A` on disk. The
  masked verification was the worse half: the tool actively confirmed a corruption it had just
  introduced. Reads and writes are now verbatim on every platform, so content round-trips
  byte-for-byte and a check against the real bytes means something. Content that genuinely
  wants CRLF still gets CRLF — nothing is normalized in either direction.
- **`edit_file` no longer converts a whole CRLF file to LF as a side effect of one edit.** With
  verbatim reads, a CRLF file's text carries `\r\n` while a model's `old` almost always uses
  bare `\n`. Rather than normalize the file to make the match work — which would rewrite every
  line ending in it — the needle is matched in the file's own convention, so a one-line edit
  changes one line. The uniqueness guard counts matches in that same converted form, so an
  ambiguous edit is still refused.

- **A busy fleet member can no longer freeze the whole console (#2590).** The hub's slug proxy
  built its HTTP client with no read timeout, so once a member accepted a connection the hub
  would wait forever for a response. A member that stalls briefly — a board agent running the
  repo's gate, say — was enough: the board view re-fetches on a ~3s poll, every poll parked
  another connection, and once six were parked the browser's per-origin connection cap meant it
  could issue **no** request to the console origin at all. A transient member stall became a
  frozen app whose only recovery was force-quitting, which then had its own cost — the
  relaunched hub found its port still held and silently fell back to an ephemeral one. The
  comment above the client claimed the finite *connect* timeout prevented exactly this; it could
  not, because "accepts then stalls" is the read phase, not the handshake.
  Proxied requests now pick a read timeout per request, because httpx applies one to every
  socket read — including the wait for the next SSE event, so a single global value cannot serve
  every shape of traffic. Streams (`/a2a`, `/api/events`, or any request asking for
  `text/event-stream`) stay unbounded as before; a whole agent turn posted to `/api/chat` gets a
  long bound; everything else — plugin views, API reads, the polls that caused this — gets a
  short one. A member that stalls now returns **504** so the panel can show a real error,
  instead of parking the socket.

- **A turn that fails is now recorded in the conversation, instead of vanishing from it
  (#2593).** When a turn died on an exception the graph had already checkpointed your
  message, the error went back to the caller, and nothing ever wrote it to the thread — so
  the session held the request with no answer and no sign one had been attempted. An export
  of that conversation read `message_count: 1`. The practical cost was worse than a missing
  error bubble: a later turn on the same session had no idea the earlier one never ran, so
  an instruction that died to a gateway timeout was silently dropped and only resurfaced
  hours later when the session was reused. The failure is now appended to the thread as an
  assistant entry, so it shows in history, in `/export`, and in the context the next turn
  reads — tagged so a surface can render it as an error rather than as the agent's answer.
  Both the streaming and non-streaming paths record the same thing, so an exported
  transcript no longer depends on which one ran. Recording is best-effort by construction:
  it happens on the failure path, so a problem there can never replace the error it
  describes.

- **Config and registry files written on Windows are no longer silently converted to CRLF
  (#2596).** `atomic_write` — which writes every config, registry, workspace record and
  snapshot protoAgent produces — opened its temp file in text mode with the default
  newline handling, so every `\n` became `\r\n` on Windows regardless of what the caller
  composed. Nothing broke, since YAML and JSON parse either way, but identical input
  produced different bytes per platform: whole-file diffs on a checkout shared between a
  Windows and a mac machine, churn in agent snapshots moving between them, and a spurious
  difference to anything comparing or hashing a config to decide whether it changed. Line
  endings now pass through exactly as the caller wrote them. This completes the pair with
  the `write_file` fix (#2586) and the encoding fix before it (#2521), which corrected the
  same function's encoding but left its newline translation in place.

- **A config section that would be invisible in Settings now fails its test with a message
  saying so (#2598).** `FIELDS` drives the console Settings surface, so a section that
  exists in the config and round-trips through YAML but has no `FIELDS` entries renders
  nowhere — the feature ships and no operator can find or enable it without hand-editing
  YAML. The golden that should have caught this asserted a set *equality*, with the
  exemption list written inline in the test body, so the cheapest way to make it green was
  to append the new section name to that literal — which is exactly what happened once, and
  the feature was caught by review rather than by any test. The exemption list now lives
  next to `FIELDS` as `SETTINGS_EXEMPT_SECTIONS`, where widening it is a deliberate,
  reviewable edit that asks you to justify why the shape can't be a `Field` (a nested dict
  or list of dicts — the four current members are all genuinely one of those). The
  assertion is split in three so each failure names its own consequence — unreachable from
  Settings, declared but never emitted, or an exemption for a section that no longer
  exists — instead of printing a set diff that reads as "make the sets match".

- **The changelog gate now checks a fragment will actually reach the release notes, not
  just that it exists (#2600).** `Changelog entry` only asserted that a
  `changelog.d/<pr>.<kind>.md` file was added. A fragment whose text isn't a top-level
  `- ` bullet passed that check, collated into `[Unreleased]` fine, and then contributed
  nothing to the marketing changelog — `scaffold` derives entries from top-level bullets
  only, and a release whose bullets all fail to parse is omitted from `/changelog`
  entirely. So one malformed fragment could cost a whole version its entry, with green CI
  at the PR and the failure surfacing days later to whoever cut the release. The gate now
  validates each fragment the PR adds with the collator's own parser
  (`changelog.py lint-fragments`, stdlib-only so the job still installs nothing), catching
  a missing bullet, an unknown `<kind>`, and an empty file — each named, with the fix. It
  also stops counting a *deleted* fragment as this PR documenting itself.

- **Release PRs pass the changelog gate again.** `prepare-release.yml` creates
  `prepare-release/vX.Y.Z`, which the gate's `release/*` escape hatch never matched —
  those PRs had been passing by accident, because they delete every fragment and the old
  check counted any changed `changelog.d/` path including deletions. Tightening that in
  #2600 removed the accident and left release PRs failing a gate they were always meant to
  skip. The hatch now matches both spellings, with a test that drives a real
  `prepare-release/*` branch through the script.

## [0.133.0] - 2026-08-11

### Added
- **Fleet telemetry: hub-side read-only rollup across members (#2539).**
  `GET /api/telemetry/fleet` reads the fleet roster and fans out pure GETs over each
  member's existing `/api/telemetry/{summary,insights}` reads via the ADR 0042 slug
  proxy (operator-tier auth reused — no new trust surface), merging one entry per
  member: reachability/running state, turns / cost USD / success rate / cache-hit
  ratio, and the member's advise-only flagged problems with per-flag evidence
  (member, per-turn row, `trace_id`, resolved Langfuse trace link, timestamp). An
  unreachable member is reported `reachable: false` — never restarted, and never
  fails the rollup. A single-box install (no members) degrades to today's
  single-instance telemetry unchanged.
- **Fleet section in the Telemetry console surface (Slice 2, #2539).** Settings ▸
  Telemetry now renders the hub-side rollup: a read-only member grid
  (reachability/running badge + turns / cost / success / cache-hit) and a
  "Flagged problems" list where each flag carries its evidence — member, per-turn
  row, `trace_id`, resolved Langfuse trace link, and timestamp. Unreachable members
  show an informational "did not respond" state with no action controls; there are
  no controls that change/restart/mutate anything. Each flag row shows the member's
  display **label** (resolved from the members map, never the routing slug — a host
  keyed `host` renders `main`, a peer keyed `protoEngineer-ba4c` renders
  `protoEngineer`). A single-box install renders no Fleet section, leaving the
  per-instance view unchanged.
- **"Operating a fleet" core docs guide (Slice 3, #2539).** `docs/guides/operating-a-fleet.md`
  covers four operator procedures for a multi-member fleet: the health-check pass (fleet
  telemetry rollup, reachability/flag states, evidence fields), upgrade + rollout/rollback
  (every step approval → act → verify-it-worked), incident triage (locating the offending
  member/turn/trace via `evidence.turn`, `trace_id`, `trace_url`), and recovery planning
  (decision table + unreachable-after-restart runbook). Terminology and fetch paths verified
  against the Slice-1 backend rollup (`operator_api/telemetry_routes.py`) and the Slice-2
  console surface. No operator persona packaged in the template (ADR 0007).

- **The agent can read its own configuration (#2540).** A new read-only `show_config`
  tool returns the effective, merged settings — model, gateway, enabled plugins,
  filesystem roots, and each plugin's own section — so an agent diagnosing odd
  behaviour can tell a misconfiguration from a bug. It couldn't before: its own
  `langgraph-config.yaml` sits outside every filesystem fence, and one agent spent two
  sessions chasing a board bound to the wrong repo before its operator found the answer
  in a single grep. Secrets are masked as `«redacted»` — including every value in an
  MCP server's `env` and `headers` — so the agent learns a credential is set without
  reading it. Drop it with `tools.disabled: [show_config]`.

- **"View prompt" now tells the wire truth — and reads like a document
  (#2527).** The prompt viewer renders as formatted markdown by default (a Raw
  toggle keeps the byte-exact view; Copy always copies exact bytes), and it now
  reports what each model call *actually carried*: if a provider transform
  changed the delivered text (like the Claude Code identity line) you can see
  it, and if nothing reached the wire at all — the failure class behind the
  Codex no-system-prompt bug — the viewer says so in red instead of showing a
  prompt the model never received.

- **A subscription credential's remaining life is now visible to monitoring (#2549).**
  `GET /api/config/oauth-status` reported a plain `signed_in: true` for three
  materially different situations — a login protoAgent owns and refreshes, a vendor
  CLI's login it merely borrows, and an env token it can neither refresh nor inspect —
  so the first sign of an expired credential on a headless agent was a failed job. Each
  provider now reports `expires_at`, `refreshable`, and a `durability` of
  `managed`/`borrowed`/`static`, which is something you can alert on. The sign-in hints
  lead with the operator-API flow that gives a headless agent an owned, self-refreshing
  credential instead of steering toward `CLAUDE_CODE_OAUTH_TOKEN`, and the headless
  guide now documents that flow end to end.

- **An agent on a Claude or ChatGPT subscription can now fall back to the gateway
  (#2550).** Picking a subscription provider applied it to *every* model slot, and a
  gateway alias in one of them raised instead of routing — so a subscription-backed
  agent had exactly one lane and no degrade path at all: a rate limit or an expired
  credential was a hard stop, where the same agent on a gateway alias would have
  retried elsewhere. Slot names are now read the way they already read everywhere else:
  a namespaced one (`protolabs/coder`) routes through the gateway, a bare one
  (`claude-sonnet-5`) stays on the subscription. That makes
  `routing.fallback_models: [protolabs/coder]` work under `provider: anthropic-oauth`,
  and lets cheap auxiliary calls stay off your subscription entirely.

### Changed
- **The delegate health prober stops relaunching every delegate every two minutes
  (#2542).** Each probe of an ACP delegate is a whole subprocess — spawn, handshake,
  teardown — and the existing backoff only slowed *failing* delegates, so a healthy
  seven-delegate setup paid roughly 5,000 process launches a day to keep a status badge
  warm. Delegates that keep answering now relax toward a 15-minute cadence, and snap
  back to the tight one the moment they fail or someone opens the panel — so the badge
  behaves exactly as before while you're looking at it.

### Fixed
- **A one-off schedule can no longer be quietly created for 09:00 (#2159).** If the New
  Schedule dialog's Time field ended up empty — as reported on Windows, where typing
  `23:59` left the field showing it but the preview stale, then snapped back on blur —
  the builder silently substituted 09:00, previewed 09:00, accepted the submit, and
  reported success. The agent got scheduled at a materially different time than the
  operator entered. A missing time is now treated as missing: the preview says which
  field is incomplete and submit stays disabled. The time inputs also re-commit their
  value on blur, so a settled entry that the browser didn't report while typing is still
  picked up.

- **Escape in Settings really does close only the dropdown now (#2466).** The earlier
  fix looked right and didn't work: it asked "is a dropdown still open?" from the
  dialog's close handler, but the dropdown has already unmounted by then, so the answer
  was always no and one Escape still took the whole Settings dialog with it — losing
  your section and any unsaved edits. The check now samples the state at the start of
  the keypress, before anything can dismiss itself, and there's a browser test walking
  the two-step behaviour so it can't silently regress again.

- **`search_files` no longer feeds compiled bytecode and cache dirs to the model
  (#2541).** A search could return match lines from `__pycache__/*.pyc`,
  `.pytest_cache`, and `node_modules` — kilobytes of marshalled bytecode dumped into
  the agent's context mid-task, and matches that could quote a stale copy of source
  that had since been edited. The tool now skips binary files (`grep -I` semantics)
  and generated/vendored trees by default, names those exclusions in its own
  description so the model knows what it isn't being shown, and says so instead of a
  bare "(no matches)" when a search comes up empty. Pass `include_generated=true` to
  search them anyway.

- **Running the test suite from inside a live agent no longer pollutes that agent's
  chats (#2543).** A board gate or coder that ran `pytest tests/` handed the suite its
  own environment, so test fixtures resolved their stores from it and wrote into the
  running agent's data — 17 junk sessions (`hold-2`…`hold-6` "deploy the service",
  goal-kickoff `g1`/`g2`, `sess-BB`…) turned up in a fleet member's chat list, twice, on
  every full-suite run spawned that way. The suite now pins every store to a per-test
  temp root regardless of what the spawner exported, with a tripwire test that plays the
  hostile spawner and fails if a single byte reaches the agent's roots.

- **Chat code blocks keep their line breaks again (#2546).** A design-system
  attribute rename (`code-block` → `code-block-body`) had orphaned the chat
  stylesheet's code-block overrides, losing `white-space: pre` — multi-line
  code rendered as one endless horizontal line. The selectors now track the
  current markup, and an e2e guard fails if a future schema drift collapses
  code lines again.

- **The Connected-account card now sits at the top of Settings → Model.** It
  was buried at the bottom of the panel — easy to miss for the one control
  that reconnects a signed-out agent — and its mid-switch copy could
  contradict the signed-in status shown right below it. Moved up, separated
  from the model fields, and the copy now reads correctly whether you're
  signed in or not.

- **Subagents work on Claude and ChatGPT subscriptions (#2552).** On a
  subscription-backed instance the agent could chat, but every delegation —
  `task`, `task_batch`, `/dream`, `/distill`, the QA tier — failed, because the
  subagent path sent its system prompt in a shape those backends reject. Both
  the main agent and subagents now share one wire-shaping seam, so delegations
  run on your own plan just like chat does.

- **Saving work folders can no longer silently strip the ones you didn't mention
  (#2556).** `POST /api/settings/filesystem-projects` replaces the whole list, so a
  caller that meant "add this folder" and posted a single entry quietly removed every
  other root — and that list *is* the filesystem fence, so the agent lost its reach
  into its own checkout, with `{"ok": true}` either way. A request that would drop a
  configured folder is now refused with a 409 naming exactly which ones, unless it
  says `"replace": true`; acknowledged removals are logged and echoed back. The
  console's work-folder editor is unaffected — it always meant replace-all.

### Deprecated
- **ACP as the agent's main runtime is deprecated (#2548).** The "Coding agent
  (ACP)" option is gone from the Setup Wizard and the runtime selector is gone
  from Settings — ACP remains fully supported for the delegate pattern
  (`delegate_to` a registered coding agent). Existing `agent_runtime: acp:*`
  configs keep working and are labeled deprecated in the console; re-running
  setup or picking a native brain switches them off the mode.

### Docs
- **Docs: the coding-agent guides now match the product.** With the ACP runtime
  deprecated, the guides that walked you through selecting it are marked
  deprecated and point at coding-agent *delegates* — the supported way to hand
  a coding job to protoCLI, Claude Code, or Codex — instead of describing a
  setup option that no longer exists.

- **Docs: the extensibility guides tell you what to do first.** Plugins, Skills,
  Workflows, and Middleware each opened with an essay about the subsystem; they
  now lead with the short path to actually doing the thing, with the reference
  material after it. The two different features both called "skills" — SKILL.md
  procedures and A2A card skills — now point at each other so you land on the
  right page.

- **Docs: run, deploy, and expose guides lead with the command.** Running
  headless, wiring up the deploy pipeline, setting a goal, and exposing an agent
  to the internet now open with the steps instead of the background reading —
  and the deploy guide's setup steps were corrected to match what the workflows
  actually gate on.

- **Docs: agent snapshots are findable again.** The guide for exporting,
  sharing, and duplicating an agent's recipe wasn't listed in the guides index
  at all — you had to know the URL. It's now indexed alongside the other
  fleet/portability guides.

## [0.132.0] - 2026-08-11

### Fixed
- **The download page now links the newest desktop release, not the newest version number (#2514).**
  Source-only releases (PyPI/Docker) no longer produce broken installer links: the
  page resolves the newest GitHub release that actually carries the macOS DMG and
  Windows setup.exe and links those assets directly. If no desktop-complete release
  can be verified, the site build fails rather than publishing a dead link.

- **Deleting a chat from the mobile session sheet now confirms and cleans up properly (#2512).**
  The responsive sheet's ✕ used to remove the chat instantly — no "Delete this
  chat?" dialog, no Harvest choice, and no server-side purge, which left the
  session-summary memory behind. It now runs the same deletion lifecycle as the
  desktop tab strip, including the Stop-vs-Detach choice for goal-driving chats.

- **Switching to a Claude/ChatGPT subscription in Settings no longer dead-ends on gateway models (#2522).**
  Settings ▸ Model's "Get models" now probes the provider selected on the form —
  a native OAuth provider lists your subscription's models instead of the old
  gateway's — a provider flip clears the stale list, and a Primary model the new
  provider doesn't offer is swapped for one it does before you save. Previously
  every save failed validation ("not a Claude model id") and rolled back.

- **Windows `run_command` no longer runs approved PowerShell through a hidden
  cmd.exe (#2518).** The tool now takes an explicit `shell` grammar — `default`
  (cmd.exe on Windows, `/bin/sh` elsewhere), `powershell`, `cmd`, `sh` — and
  PowerShell executes via a Unicode-safe `-EncodedCommand` contract, so paths
  and content with spaces, brackets, accents, or Japanese text survive on the
  first approved attempt (no more `[char]` reconstruction). The approval dialog
  also names the real runner ("runs via: …") instead of showing only the inner
  command, so what the operator approves is what actually executes.

- **An OAuth disconnect no longer looks like a broken startup (#2513).** The
  console now treats signed-out as a first-class state: no more ~45s
  "Starting protoAgent… / Continue anyway" gate after disconnecting (or
  relaunching while signed out) — the app opens immediately with a visible
  signed-out banner, the composer swaps for a reconnect strip instead of
  accepting sends that could only fail, and both surfaces deep-link to
  Settings → Model where the reconnect control lives. Everything self-clears
  the moment reconnect rebuilds the agent.

- **On ChatGPT/Codex accounts, the agent's system prompt now actually reaches
  the model (#2519).** Every tool-bearing turn on the `openai-codex` provider
  was silently sent with no system prompt at all — persona, operating doctrine,
  and knowledge context were dropped by a langchain re-bind after the middleware
  moved them into the Responses `instructions` field, while "View prompt"
  (captured upstream) still displayed the full prompt. Instructions now ride the
  factory's supported `model_settings` channel, verified all the way down to the
  wire payload by a new regression test. If your Codex-backed agent felt like it
  ignored its SOUL.md — this was why.

- **HITL cards format their text now.** When the agent pauses to ask you
  something, the question renders as proper markdown — bold, code, numbered
  lists — instead of raw `**sigils**`, and a long question scrolls inside the
  card rather than growing it unbounded. Shell-command approval details stay
  verbatim on purpose.

- **Windows: fresh agents can export snapshots again (#2521).** Newly created
  fleet members wrote their config in the Windows locale encoding (CP1252),
  which crashed the strict-UTF-8 snapshot exporter with `UnicodeDecodeError`
  before the review even built. All config and registry writes are now pinned
  to UTF-8, and reads tolerate the legacy encoding — an agent created on an
  affected build exports cleanly after upgrading, with a log line naming any
  legacy-encoded file it healed around.

- **Agent names show exactly as you typed them (#2520).** Renaming an agent to
  something like `PA Windows Lifecycle Café` no longer silently renders as
  `PA_Windows_Lifecycle_Caf` in the header, agent switcher, and Fleet page —
  those surfaces now display the verbatim name, while the charset-restricted
  addressing handle (and the immutable agent id/URL) keep doing their job
  underneath.

- **"Reset to inherited" actually works on fleet members now (#2528).** The
  model name, provider, and API base reset as one group (they only validate
  together), and the host now mirrors its own model setup into the box-level
  inherited layer — so resetting a member's model overrides lands on what the
  box actually runs, instead of an unusable app default that rolled every
  reset back and left overrides looking permanent.

### Docs
- **The build-with-a-coding-agent guide is now a task-first walkthrough (#2510).**
  It opens with prerequisites, walks standing up the PM and wiring a coder, and
  adds the previously missing core: shipping your first feature end to end, with
  the board state you should see at each step. Doctrine moved into callouts and
  a closing "Grow it" section; every command and config key re-verified against
  what ships.

## [0.131.3] - 2026-08-11

### Fixed
- **Switching a running instance to Claude or ChatGPT works from the console (#2508).**
  On macOS the Claude Code login lives in the Keychain, not the credentials
  file, so a live switch to `anthropic-oauth` failed "no credential found" with
  Claude Code signed in right there — the resolver now reads the Keychain too.
  And because a failed switch left the saved provider dangling with no sign-in
  UI, the Settings account card now keys on the saved provider and a completed
  sign-in finishes the pending switch automatically — no re-save, no restart.

## [0.131.2] - 2026-08-10

### Changed
- **Install from URL moved to the Plugins panel header (#2506).**
  The button sat at the end of the search/filter toolbar where it read as
  another filter; it's now a header action on the Installed tab, where primary
  panel actions live.

### Fixed
- **"Rewind to here" no longer appears on the latest message (#2505).**
  The action offered to "discard everything below" the conversational tail —
  an empty set — so its irreversible-sounding confirmation did nothing. It now
  hides on the last user/assistant message; an assistant message followed by a
  stranded (errored) user bubble keeps it, since discarding that trailing
  message is exactly what Rewind is for.

## [0.131.1] - 2026-08-10

### Fixed
- **Config-route hardening: no more event-loop stalls or ambiguous responses (#2486).**
  The gateway model probe and setup reset ran blocking work inline in async
  handlers (every Settings model-list fetch stalled the whole server for the
  probe's duration); both now run off the event loop. An unknown SOUL preset
  returns 404 instead of an empty-string 200 indistinguishable from a blank
  preset. Coverage added for finish-setup, config-explain, and the pre-setup
  projects route (which was verified None-safe, contrary to the review claim
  that spawned this issue).

## [0.131.0] - 2026-08-10

### Added
- **OAuth account lifecycle controls now live in Settings ▸ Model (#2460).**
  Connect status, Disconnect, and the native device-code/paste-code Reconnect
  flows were wizard-only — after setup there was no supported way to inspect or
  repair the connection, which turned any signed-out state into a dead end.
  Settings and the Setup Wizard now share one lifecycle implementation
  (`useOauthLifecycle`); a Settings reconnect rebuilds the agent graph inline
  and a disconnect signs the agent out cleanly, both converging every visible
  model/provider surface without a restart. The section renders only for
  subscription providers.

### Fixed
- **An OAuth disconnect no longer makes the server unbootable (#2458).**
  Disconnecting Codex/Claude and restarting hit a recovery dead end: the
  disconnect marker made startup graph construction raise `OAuthCredentialError`,
  the backend exited before binding its port, and the reconnect routes the user
  now needed were unreachable — recovery meant hand-deleting the marker. Signed-out
  is now a first-class state: the server boots graphless (same pattern as
  pre-setup), `/api/runtime/status` carries a `graph_auth_error` block naming the
  provider and fix, chat answers "Signed out — reconnect" instead of "finish
  setup", and a completed in-console sign-in rebuilds the graph inline — no
  restart, no manual file surgery. The cache warmer is not built while signed
  out, so a disconnected instance sends no provider requests.

- **OAuth disconnect now signs the live graph out too (#2459).**
  Disconnecting Codex/Claude revoked and deleted the stored credential but left
  the running graph's in-memory client holding the just-revoked token — the
  composer stayed usable and the next prompt reached the provider and surfaced a
  raw `401 token_revoked`. Disconnect is now an application-level auth
  transition: when the live graph runs on the disconnected provider, the cache
  warmer is stopped and the graph is unloaded into the same signed-out state a
  disconnected boot produces, so no further provider request is possible and
  chat reports "Signed out — reconnect" until the in-console sign-in rebuilds
  the graph.

- **Setup Finish no longer leaves stale model/provider UI until a restart (#2462).**
  Finishing the wizard refetched only runtime status, so the composer model chip
  and Settings ▸ Model could keep showing pre-setup values (while requests
  correctly used the new provider) until the desktop app restarted. Finish now
  invalidates the entire query cache — config, settings, model, and runtime
  state converge immediately.

- **Subscription-provider dollars are now labeled as estimates, not charges (#2463).**
  ChatGPT/Codex and Claude subscription turns showed a plain `$0.03` under the
  message — a pricing-table estimate that a normal user reasonably read as an
  extra bill. Subscription-backed turns now render `~$0.03` with the tooltip row
  relabeled "Est. cost · API-equivalent — not an additional charge" and an
  explanatory note; API-key/gateway turns keep the plain cost wording. Tokens
  and elapsed time are unchanged.

- **Catalog and theme reads no longer depend on the Windows locale codepage (#2464).**
  `/api/archetypes` served `â€”` where the archetype catalog says `—`: the frozen
  Windows build decoded the repo's UTF-8 catalogs with the active locale (cp1252).
  All operator-API catalog/theme reads (`archetype-catalog.json`, `mcp-catalog.json`,
  `plugin-catalog.json`, `theme.json`) now pass `encoding="utf-8"` explicitly, and a
  sweep test keeps bare `read_text()` out of `operator_api/`. On multibyte locales
  (cp932) the old path could even raise an uncaught `UnicodeDecodeError` — that
  failure class is gone too.

- **A legacy non-UTF-8 catalog override degrades safely instead of 500ing (#2465).**
  With catalog reads now explicitly UTF-8, a local override file written in a
  Windows codepage raised an uncaught `UnicodeDecodeError`. All four operator-API
  catalog/theme readers now treat a decode failure like any other unreadable
  catalog — log and fall back. Thanks to @RomeoRaven, whose #2465 caught this
  gap (the UTF-8-read half of that PR landed via #2472).

- **Escape in a Settings dropdown no longer closes all of Settings (#2466).**
  One Escape press dismissed both the open model picker and the entire Settings
  dialog — the dialog's document-level Escape listener fired alongside the
  dropdown's own dismiss. Settings now yields to the topmost layer: the first
  Escape closes only the dropdown (form state and section intact), the next one
  closes Settings. Tooltips never hold the dialog open. Consumer-side
  arbitration until the design-system Dialog owns it.

- **The Keyboard settings hint now speaks the viewer's platform (#2467).**
  The browser-reserved-shortcut note hard-coded macOS glyphs (`⌘T`, `⌘1–9`,
  `⌃Tab`) while the Windows bindings below it correctly said `Ctrl+…` — a
  Windows user had to translate Mac symbols to know what conflicts. The hint is
  now derived from the same platform-aware `formatCombo` the binding rows use,
  so the copy can't drift from the displayed controls on any OS.

- **Telemetry timestamps now render in the operator's local timezone (#2468).**
  Recent-turn and flagged-insight stamps sliced the raw UTC string, so a
  non-UTC operator read every time hours off with no timezone label. Both
  surfaces now parse the ISO instant and render local wall-clock time, with the
  full timestamp + timezone name in the tooltip/accessible text; offsetless
  legacy rows are treated as UTC, never as local.

- **Windows Settings no longer speak macOS (#2470, #2469).**
  The Project-directory picker showed a POSIX `~/Documents` example and
  Autostart promised to "install/remove the boot LaunchAgent" — a macOS-only
  mechanism — on Windows hosts. Path examples are now platform-native, and the
  autostart description tells the truth per platform (macOS: LaunchAgent;
  elsewhere: not yet supported). Also (#2469) the Auto-inject-namespaces help
  now matches its parser: comma- or newline-separated, quoted `""` for the
  un-namespaced sentinel — the old "empty line" instruction described behavior
  the parser drops.

- **The bundled Docs view parses again (#2471).**
  v0.130.0 shipped the Docs plugin view dead — empty list, dead search — because
  a `\\'` escape written for cooked-string semantics sat inside the view's RAW
  Python string and reached the browser verbatim, terminating a JS string
  literal (`SyntaxError: Unexpected identifier 't'`). The copy no longer needs
  an escape, and a new sweep test extracts every bundled plugin view's inline
  `<script>` (via `ast`, so raw/cooked strings yield exactly what the browser
  receives) and runs it through `node --check` — this class of dead-view
  regression can't ship silently again.

- **`/model` and the composer picker now list native-OAuth subscription models (#2473).**
  With ChatGPT/Codex or Claude OAuth configured, Settings ▸ Get models discovered
  the full model list but `/model` offered exactly one card labeled "gateway
  model": the settings schema — the source `/model` and the composer read —
  always probed the gateway. The schema now branches to subscription discovery
  for native OAuth providers (offloaded off the event loop, both paths), and the
  picker labels those cards "ChatGPT subscription" / "Claude subscription".

- **The chat-focus shortcuts work from any surface (#2474).**
  `/` and `Ctrl+1` — both labeled "Focus chat composer" — silently did nothing
  outside the Chat surface: they bumped a focus signal only the (hidden) chat
  panel observes. Both bindings now navigate to Chat first, then focus the
  composer, so the documented fastest path back to chat works from Knowledge,
  Settings, or anywhere else.

- **Rewind works on Responses-API / reasoning models (#2480).**
  "Rewind to here" accepted its destructive confirmation and then removed
  nothing: on models whose messages carry list-of-block content (ChatGPT/Codex
  Responses API, reasoning models), the checkpoint matcher compared the bubble
  text against `str(content)` — a Python repr that can never match — so every
  content-targeted rewind returned not-found. The matcher now normalizes block
  content to its text (concatenated like the stream; reasoning/non-text blocks
  ignored), so the clicked message resolves and the discard actually happens.

- **Deleting a chat now deletes its session-summary memory too (#2482).**
  The delete dialog promises the chat "and its history will be removed", and the
  flow purged checkpoints, attachments, and prompt snapshots — but the per-session
  summary survived and kept riding `<prior_sessions>` into future model prompts:
  a deleted conversation's id, topic, and message count were still being sent to
  models. Chat deletion and the memory inspector now share one deletion seam
  that removes every file the summary may live under (encoded + legacy names).

- **`/btw` asides are truly saved nowhere now (#2483).**
  The aside question and answer — labeled "not saved to the conversation" — were
  written into the persisted browser chat store and came back after a reload.
  Aside overlays are now marked ephemeral and stripped at persist time: they stay
  on screen for the session, and a reload forgets them, which is what the label
  promises.

- **Incognito turns no longer offer a View prompt that 404s (#2484).**
  Incognito retains no prompt snapshots by design, but the turn toolbar still
  offered View prompt — clicking produced a 404 and copy blaming retention
  trimming, making active privacy look like data loss. The action is hidden on
  incognito sessions.

- **Regenerate replaces the turn everywhere, not just on screen (#2491).**
  Regenerate visually swapped the answer while the server appended a second
  identical user/assistant pair — history, session-summary memory, `/export`,
  and future model context silently carried duplicates the chat never showed.
  Regenerate now rewinds the server checkpoint past the old pair (an exclusive
  `before` cut on the rewind op) before resending, so every copy of the
  conversation agrees with the screen.

- **The bare `/effort` (and `/model`) picker is mouse-usable again (#2492).**
  Clicking Send on a bare slash command left the autocomplete menu mounted over
  the picker form it opened, intercepting every click — Submit couldn't be
  pressed. The slash popover tracks the textarea's live token via keyboard/focus
  events, and a mouse click on Send fires none of them; Send now clears the
  popover along with the draft. Regression-tested end-to-end on the exact
  reported path.

### Security
- **Disconnect no longer remotely revokes a credential borrowed from the Codex CLI (#2461).**
  When protoAgent bootstraps from the CLI's `auth.json`, that token set is a
  *shared* login — remotely revoking it on disconnect could sign the Codex CLI
  out too. Token stores now record provenance (`cli_bootstrap` vs
  `device_login`); disconnect revokes at OpenAI only for logins protoAgent's own
  in-console sign-in minted, and scopes borrowed (or legacy, provenance-unknown)
  credentials to local-delete-plus-marker. Refreshes preserve provenance.

## [0.130.0] - 2026-08-10

### Added
- **A persona that commands an action no tool backs now warns instead of failing silently (#2443).** A SOUL.md commitment with no registered tool behind it (e.g. "file issues" with `github.write` off) used to produce narrated success — the model reports the action as done because it never calls the missing tool. The host now audits the persona against the bound tool set at boot and on every reload, logs each untooled commitment, and publishes `persona.untooled_action_detected` on the event bus. Warn-only: a persona is never blocked from loading. Closes #2276.

### Changed
- **The New/Edit schedule dialog is a real builder, with validation (#2159).** The live
  "when it runs" preview is now a sticky banner at the top of the form showing the plain-
  English description, the raw cron, and the timezone — with a red error line when the input
  is invalid. A one-off scheduled in the past is flagged and **blocks submit** (it would have
  silently never fired). One-off runs show your detected local timezone; recurring schedules
  default to your local zone instead of UTC. And editing an existing job now opens the same
  Once/Repeat/Cron builder (pre-filled by parsing the stored schedule) instead of a raw cron
  text box.

- **More of the test suite runs natively on Windows CI (#2412).** Two files
  (`test_instance_paths`, `test_store_tier_resolvers`) came off the Windows-native
  exclusion burndown — one needed a forward-slash path assertion made separator-agnostic,
  the other was already portable after the ADR 0098 process-tree migration. The Windows PR
  job now gates them too (exclusion ledger 18 → 16).

- **`CHANGELOG.md` is now split into dated monthly archives (#2437).** The root file kept
  growing without bound (600 KB+), so it now holds only `[Unreleased]` and the current
  month; older releases move verbatim into dated `CHANGELOG-YYYY-MM.md` files (starting with
  `CHANGELOG-THROUGH-2026-07.md`), cross-linked from the root. Release collation still writes
  only `CHANGELOG.md`, and `changelog.py notes <version>` reads the archives so old desktop
  rebuilds still resolve their notes. A new `changelog.py archive` command handles the
  once-a-month rollover (see the releasing guide).

- **`find_files` and `search_files` return forward-slash paths on Windows (#2446).** The
  managed-filesystem search tools emitted OS-native separators (`src\main.py`) on Windows;
  they now return a stable forward-slash form (`src/main.py`) on every platform, matching
  what `read_file`/`edit_file` already accept. Part of the ongoing #2412 work: ten more test
  files (config, agent-snapshot, fs-tools, workspaces, egress, media, and memory path
  handling) now run under the native-Windows CI gate instead of being skipped.

### Fixed
- **Native OAuth sign-in now has cancel, disconnect, and revocation (#2440).** The wizard's
  Cancel now aborts the *server-side* sign-in flow (not just the browser timer), and a signed-in
  provider has a **Disconnect** action: it best-effort revokes protoAgent's token at the provider,
  always deletes protoAgent's own stored credential even if revocation fails, and stays
  disconnected until you sign in again — it never re-imports from (or touches) the Codex/Claude
  CLI's own auth file. Credential and disconnect-state files keep the Windows owner-only ACL.

- **Concurrent Codex credential resolution no longer races the single-use refresh token (#2441).**
  `create_llm` resolves credentials per turn and for subagent slots; two in-process consumers could
  both refresh the same expiring token, and one would fail with an HTTP 400. Refresh/bootstrap is now
  serialized per instance store — the first caller refreshes, the rest re-read and reuse the rotated
  token — while warm reads stay lock-free.

- **The review panel can no longer publish its own reasoning to a pull request (#2447).** The
  synthesizer's reply was posted verbatim as a PR comment, so when a serving lane left
  deliberation in `content` instead of on the native reasoning channel, the entire
  chain-of-thought shipped to the author under the bot's identity — some 2,000 words of
  "Actually, let me reconsider…", including a draft dispositions block the model had already
  revised away. The model wasn't misbehaving, so no prompt could have prevented it. The comment
  is now **built** from parsed blocks instead of echoed: the synthesizer delimits its prose brief
  (`<!-- brief -->` … `<!-- /brief -->`), and anything outside a recognized block is unpublishable
  because no code path publishes it. A brief that misses its delimiters is reported as unreadable
  rather than quietly falling back to raw text. Also corrects a contradiction that had been making
  review bodies inconsistent: the role prompt asked for the brief *after* the findings JSON while
  both recipes asked for it first. The publisher half ships in pr-reviewer-plugin v0.25.0.

- **The coder plugin's test verifier now runs on Windows (#2450).** `run_tests` spawned
  `python -m pytest` with a scrubbed environment that dropped `SystemRoot` (and
  `TEMP`/`TMP`/`COMSPEC`/`PATHEXT`) — vars a freshly spawned `python.exe` needs to
  initialize — so verification silently failed on Windows. Those OS-essential vars are now
  preserved (the secret-scrub is otherwise unchanged). Part of the ongoing #2412 work: four
  more test files (delegates ×2, coder, workflow-run-state) now gate on the native-Windows
  CI. (coding-agent needs an acp_client runtime fix first — #2452; execute_code — #2449.)

- **The media URL-signing key is now owner-only on Windows (#2451).** `infra/media` created
  `media/.signing-key` — the HMAC secret that authenticates media URLs — with a bare
  `os.chmod(0o600)`, which on Windows only flips the read-only bit, so unlike every other
  credential file the key inherited its parent directory's ACL. It now goes through the
  ACL-hardening funnel (`atomic_write` → `harden_private_file`), giving it an owner-only ACL
  on Windows and `0o600` on POSIX.

- **`execute_code` runs on Windows (#2453).** The plugin's tool-RPC bridge — which lets a
  sandboxed script call host tools — was built on POSIX fd-inheritance (anonymous
  `os.pipe()` + `pass_fds` + fd-number handoff), so it couldn't run on Windows at all. It now
  bridges over a **loopback socket** authenticated with a per-run token, works identically on
  both platforms, and kills the child's **whole process tree** on timeout (ADR 0098) instead
  of orphaning grandchildren.

- **Coding-agent death errors are accurate on Windows (#2454).** When an ACP coder
  subprocess died mid-turn, Windows reported *"process still running or never started"*
  instead of the real exit code, and a dead child's stderr pipe could raise an unhandled
  `ConnectionResetError`. The exit status is now reaped before the error is built, and a lost
  pipe is treated as end-of-stream. This also **completes the Windows-native test burndown**
  (#2412): the exclusion ledger is now empty, so the whole test suite gates on Windows.

## [0.129.0] - 2026-08-09

### Added
- **Run Claude or ChatGPT on your own coding-agent subscription, natively (#2420).** Two new
  `model.provider` values authenticate protoAgent's native pipeline with an OAuth
  subscription instead of a gateway API key — no gateway, no ACP. `anthropic-oauth` drives
  Claude Pro/Max through a Claude Code OAuth token (read live from `~/.claude`), and
  `openai-codex` drives ChatGPT/Codex through the Responses API (bootstrapped from
  `~/.codex`, then self-refreshed). Everything downstream — tool loop, streaming,
  compaction, subagents — is unchanged, because the graph already treats the model as a
  plug-in client. Opt-in per agent; see ADR 0097.
- **Setup wizard: pick a subscription brain, no config-file editing (#2420).** The
  first-run wizard's "brain" step now offers Gateway · Claude subscription · ChatGPT
  subscription · Coding agent as one choice. The subscription options auto-detect your
  Claude Code / Codex sign-in ("✓ Signed in — Max plan" or the exact sign-in command),
  populate the model dropdown from your account's real model list, and Test connection
  runs a real turn on your plan — no API key, no api_base, no hand-picked model id. The
  `model.provider` setting is now a dropdown everywhere.
- **Sign in to Claude / ChatGPT from the console — no terminal (#2420).** The subscription
  cards now have a "Sign in" button that runs the OAuth flow in-app: ChatGPT/Codex uses a
  device code (enter it at the opened page; the console polls until you approve), and Claude
  opens the approve page and takes the code you paste back. Tokens are stored and refreshed
  for you. See ADR 0097 for the Claude client-id note.

## [0.128.0] - 2026-08-09

### Added
- **The system-prompt viewer answers "what changed?" and "what's next?" (#2415).** P3 of
  the viewer: each call shows a section-level diff vs the previous call or turn ("Injected
  memory +312 chars"), subagent prompts are captured and nest as tabs under their turn,
  and an explicit preview route speculatively composes the next call's prompt — retrieval
  included, no model call, no injection-log write.

- **PRs are now gated by a native Windows test job (#2419).** `checks.yml` runs the
  Python suite on `windows-latest` minus a named 41-file POSIX-only backlog
  (`tests/windows_native_exclusions.txt`, the #2412 burndown — shrink it, never grow
  it), so ~4650 tests must pass natively before merge. Until now the only Windows
  feedback was the desktop release leg or a contributor's own machine.

- **One cross-platform process-tree lifecycle — `infra/proc` (#2424).** ADR 0098: spawn
  anchoring (`group_kwargs`/`detached_kwargs`) and tree teardown
  (`kill_tree`/`akill_tree`/`terminate_tree`) work on Windows (`taskkill /T`, bounded
  waits) and POSIX (`killpg`) alike, with the proven `pid_alive` probe alongside. The
  agent shell tool is migrated; ACP delegates, fleet members, and `protoagent up/down`
  follow — burning down the Windows test-exclusion list as they land.

### Changed
- **The console now uses the canonical `--pl-*` design tokens everywhere (#2414).** The
  legacy `--bg`/`--fg`/`--border`/`--accent` bridge aliases are retired from
  `theme-base.css` (status-tone compat aliases remain, as pinned by the token guard);
  zero visual change by construction.

### Fixed
- **Plugin views fail loudly when the DS kit is missing instead of silently 401ing (#2409).**
  docs, orgchart, and artifact substituted a bearer-less fetch when the plugin-kit failed to
  import — on gated instances docs rendered as empty and orgchart blamed auth. All three now
  name the missing `/_ds` bundle, and docs distinguishes an absent doc (404) from an auth
  failure.

- **Windows shell and desktop paths are native (#2416).** Timed-out agent commands
  terminate their whole process tree via `taskkill /T /F` (with a bounded wait), the
  fenced `run_command` tool uses `cmd.exe` on Windows instead of `/bin/sh`, and the
  desktop sidecar keeps all writable box-tier state (`.data-version`, host-config,
  commons, cache) inside its app-config directory instead of leaking into
  `~/.protoagent`. Contributed by Dennis F (@RomeoRaven) in #2413.

- **Pure-Python wheel installs now work on Windows and keep dependency pins in the canonical lock schema (#2417, #2418).**
  Valid nested wheel members use path-aware containment instead of POSIX separators, legacy top-level dependency pins migrate into the `plugins` list, and the Windows desktop release leg exercises the wheel flow.

- **The desktop sidecar now serves the console at `/app` — mobile QR pairing works from
  desktop installs (#2423).** The frozen sidecar bundled no console build, so `--ui
  console` logged a missing-build warning on every launch and a paired phone got a 404
  (pairing loads the SPA from the sidecar, not the desktop webview). The sidecar now
  bundles `apps/web/dist`, the build hard-fails if the console build is missing, and the
  per-platform desktop smoke asserts `/app` serves.

- **Stopping an ACP delegate on Windows no longer leaks its backend (#2425).** Delegate
  teardown killed by POSIX process group only; on Windows the spawned backend survived a
  stop. All three teardown paths (graceful close, hard stop, cancel) now go through the
  ADR 0098 `signal_tree` primitive — `taskkill /T` on Windows, `killpg` on POSIX — and the
  orphan-reaping regressions run natively on the Windows CI gate.

- **Rapid persona saves can no longer prune the wrong history snapshot (#2426).** Soul
  history relied on microsecond timestamps for ordering; on hosts whose clock ticks
  coarser (Windows), two rapid saves could share a stamp and the content hash decided
  which snapshot the cap deleted. Snapshot stamps are now strictly increasing.

- **Fleet stop and `protoagent down` work on Windows — and take the whole member tree
  (#2427).** Fleet lifecycle referenced POSIX-only `signal.SIGKILL`/`os.WNOHANG` and
  crashed with `AttributeError` on Windows; liveness checks and stops could not work.
  Member/server teardown now goes through the ADR 0098 tree primitives on both
  platforms, and a stopped member's children (managed MCP servers, node runtimes) die
  with it instead of relying on parent-death watchdogs.

- **Concurrent metric writes can no longer be starved into "database is locked"
  (#2429).** SQLite's busy wait is a retry loop, not a queue — a tight write loop on
  one engine thread could re-win the file lock until a sibling thread's 5s budget
  expired and its sample was dropped. Metric writes now serialize in-process (the busy
  timeout still covers cross-process writers), and every connection-per-call store
  arms its busy timeout before the WAL pragma.

- **Windows install/uninstall now sweeps the sidecar's stranded `_MEI` runtime dirs
  (#2430).** Force-stopping the onefile sidecar skips PyInstaller's own temp cleanup,
  stranding ~140 MB per stop — and a graceful close isn't reachable from the installer
  (the desktop app absorbs it into close-to-tray). Both installer hooks now delete only
  the `%TEMP%\_MEI*` dirs that contain protoAgent's own bundled marker package, so
  upgrades also reclaim residue left by older versions and no other application's
  extraction can ever match.

- **Installing a plugin from a local path works on Windows (#2433).** The installer's
  source validator only recognized POSIX absolute paths, so every drive-letter path
  (`C:\src\my-plugin`) was rejected as an unsupported source — one line that accounted
  for 38 of the remaining Windows-native test failures. Drive-absolute paths now pass,
  and the plugin-installer suite runs natively on the Windows CI gate.

- **Watches and goals work on Windows — state writes no longer fail, verifiers no
  longer hit the WSL stub (#2434).** Both stores swapped atomic renames for
  overwrite-atomic `os.replace` (Windows `rename` refuses to overwrite, so every state
  update after the first silently failed), and goal command-verifiers run through the
  native shell on Windows instead of `bash` — which resolves to the WSL stub on a stock
  install. Twelve more test files run natively on the Windows CI gate.

### Security
- **Windows credential files now have a real privacy contract (#2431).** POSIX `chmod
  0600` is decorative on Windows (`stat` reports 0666), so secrets, fleet tokens, and
  imported snapshot secrets are now ACL-hardened — inheritance stripped, owner-only
  grant, the OpenSSH-for-Windows private-key posture — and the suite asserts the
  contract natively on both platforms (no broad principal may hold access).

### Docs
- **The external-PR path is documented (#2421).** PROTO.md's gates table now lists the
  changelog-fragment gate (and its `skip-changelog` escape hatch), and CONTRIBUTING.md
  gained a "Sending a pull request" section — fragment shape, allow-maintainer-edits,
  base-on-current-main — so outside contributions stop hitting the gate blind (#2413).

## [0.127.0] - 2026-08-08

### Added
- **The devkit's rail view is now a live build-status page (#2401).** ADR 0096 D8: every
  plugin the agent built for itself, with live/failed badges, tool chips, and traceback
  blocks for failures — auto-refreshing in place as the agent builds. The authoring guide
  card stays at `/guide`.

### Fixed
- **Plugin routers hot-remount on reload and unmount on disable (#2404).** Iterating on a
  plugin's console view goes live without a restart — previously the first-mounted route
  served stale forever (the #942 class, hit within an hour of the self-building loop's
  first live demo) — and disabling a view/router plugin no longer recommends a restart.

- **Enabling a plugin on a fleet member no longer 401s its just-added view (#2405).** The
  hub's member-public path cache now revalidates against the member on a miss (bounded
  per slug), closing the enable→click race the self-building loop's instant rail refresh
  made easy to hit.

## [0.126.0] - 2026-08-07

### Added
- **The shared note is no longer destructively overwritten — it keeps recoverable history (#2022).** `write_note`
  is a documented full overwrite and the operator's editor autosaves over the same file, so anything either side
  replaced was gone for good: no history, no undo, no diff. Every change now archives the OUTGOING text first
  (`<notes-dir>/history/`, one file per version, attributed to the agent or the operator), and the live note file
  is untouched — still exactly the markdown you typed. New tools `list_note_versions`, `restore_note_version` and
  `diff_note_version`, plus `read_note(version=…)`; the editor gains a **History** drawer that lists versions,
  shows a unified diff against the current note, and restores one in a click. A restore archives the text it
  replaces, so recovering is itself undoable.
- **Notes settings: `Versions kept` (default 50) and `Coalesce window` (default 300s).** The window is why
  versioning the note is useful rather than noisy: the editor autosaves on a 700ms debounce, so archiving every
  write would mint a version per keystroke and evict the note's real history within a minute of typing. Successive
  edits by the same author inside the window share one version — one editing session, one version — while a change
  of author always cuts a version, because agent-clobbers-operator is the case most worth undoing.

- **Merge-on-boot declarative seeding — a baked config seed can now stay live across image rolls (#2071).**
  `ensure_live_config` is seed-once, so the *image-owned* half of `PROTOAGENT_SEED_CONFIG` (agent identity, the
  A2A card `description`/`skills`, `plugins.enabled`) never reached a config volume that had been seeded before
  those keys existed — a fleet agent kept serving the stock card and nothing said so. The only fixes were wiping
  the volume (losing operator state) or hand-POSTing `/api/config`; both defeat config-as-code. Setting
  `PROTOAGENT_SEED_MERGE=1` now re-applies the seed on every boot as a **three-way** merge against
  `<config-dir>/.seed-applied.yaml` (the seed as last applied): a key the operator never touched tracks the image,
  a key they edited always wins, a newly-baked block appears, and a key the image drops falls back to its default.
  The snapshot is what makes it durable — comparing the seed against the live file alone would pin every
  already-present key forever, so only the *first* roll would land. Opt-in and env-only (unset ⇒ seed-once, byte
  for byte); secret paths are excluded so a baked credential can never be written into the exportable YAML;
  comments and fork-added sections survive; a malformed seed logs and leaves the live config untouched.

- **Tool cards show what a call cost you in context (#2282).** A tool card carried name, status, input/output
  preview and duration — but nothing about context cost, so a `fetch_url` returning 8KB of HTML and a
  `current_time` returning 40 characters read identically, and the first is what eats a context budget.
  Settled cards whose result clears ~250 tokens now carry an estimate in the header (`fetch_url · ~2.0k ctx`),
  using the same `chars ÷ 4` arithmetic as the prompt viewer's budget rows so the two numbers are comparable.
  It stays off small results, in-flight calls (output arrives with the end frame, so any mid-flight number would
  be wrong) and failures — its presence is itself the signal that a call was expensive. Labelled `~` with a
  tooltip saying plainly that it is an estimate, not a measurement: it cannot know how much of the output the
  model reads, or whether compaction trims it first, and the cost lands on the *next* model call. Real
  per-call attribution needs a counting seam at the tool-execution boundary that the engine does not have
  (usage is tracked per model call, and one model call can emit several tool calls) — that remains open.

- **The plugin-devkit now closes the whole build loop — scaffold → edit → test → hot-swap, no restart
  (#2394).** ADR 0096 slice 1: `plugin_list_files` / `plugin_read_file` / `plugin_write_file` edit a
  plugin in place (fenced to the plugins dir), `test_plugin` runs its pytest suite in a subprocess
  (managed-runtime-aware on desktop), load failures report with a bounded traceback, `reload_plugins`
  no longer drowns real failures in disabled-plugin noise, and the reload-triggering tools offload the
  graph recompile off the event loop.

- **`develop_plugin` — the self-building loop's delegated build lane (#2395).** ADR 0096 slice 2: hand
  a substantial plugin build to a configured `acp` coding delegate working scoped inside the plugin
  dir (fresh session, git lifecycle off, guaranteed teardown), then the host auto-runs the plugin's
  tests and hot-reloads it and reports all three results. `scaffold_plugin` gains `git_init`
  (CLI `--git`) for a repo from birth.

- **Agent-initiated plugin changes now push a live console refresh (#2396).** ADR 0096 D8: the reload
  seam publishes `plugin.changed`, and a `PluginChangeWatch` subscriber refetches the plugin queries —
  so a rail view the agent just built and enabled appears immediately in every open console. Also
  gives the autoupdate loop's `plugin.updated` its first console consumer and unifies the
  Plugins-panel toggle on the shared refresh.

- **`register_plugin_project` — graduate a self-built plugin to a managed project (#2397).** ADR 0096
  D6: the ADR 0095 registry's first runtime write path, scoped to the plugins dir, so fs tools address
  the plugin by name, the GitHub repo picker sees it, and a projectBoard can target it via
  `project_board.project`.

- **The devkit ships a `self-building-demo` skill (#2400).** The rehearsed script for demoing
  protoAgent building, testing, and hot-swapping its own plugins live, with the live-QA lessons
  (next-turn tool binding, the `register()` contract, state placement) baked into the beats.

### Fixed
- **Review findings must quote evidence VERBATIM — the findings contract now says so (#2373).**
  A finding whose `evidence` paraphrases or reconstructs code, rather than copying the file's exact bytes,
  cannot be grounded against that file, so the pr-reviewer grounding guard downgrades it to `uncertain` and it
  loses the power to block. Larger models paraphrase far more than smaller ones: after the QA reviewer moved to
  a deepseek-class 1M model the panel's grounding-downgrade rate went from ~0% to ~30% over a few days, and two
  of those were major-severity — the underlying defect was real, only the quote was a reconstruction
  (`not origin_incognito` where the file has `not getattr(j, "origin_incognito", False)`; a composite Rust line
  stitched from separate lines). A PR whose sole blocker was such a finding would leak past the gate.
  `FINDINGS_CONTRACT` — the canonical schema snippet every finder interpolates — now requires byte-for-byte
  quotes and says to cite `file:line` rather than invent a loose one. Grounding itself is unchanged; it *should*
  reject non-verbatim quotes, so the fix is source-side, making the finders quote correctly.

- **The devkit's build loop no longer calls a plugin that loaded with zero contributions "live"
  (#2398).** `enable_plugin`/`reload_plugins`/`develop_plugin` report what a plugin actually registered
  and flag a silent no-op `register()` with the contract fix (found by the first live run of the
  ADR 0096 self-building loop — a returned tool list is ignored by the loader). The boot log and
  runtime status also now count convention-shipped `skills/` dirs.

- **`test_plugin` now runs a plugin's suite against a disposable copy of the plugin dir (#2399).** A
  destructive test (e.g. an empty-state test that deletes a data file, as a coding delegate wrote
  during ADR 0096 live QA) can no longer touch live files. The building-plugins skill now steers
  runtime state out of the plugin dir entirely.

### Docs
- **ADR 0096 — the self-building loop (#2393).** The architectural record for plugin-devkit closing
  design → scaffold → edit → test → hot-swap (one spine, three build lanes: in-place, delegated ACP
  coder, board-driven), with the 2026-08 audit of the eight open seams and the deferred-guardrails
  decision recorded.

## [0.125.1] - 2026-08-06

### Fixed
- **Renaming an agent in Settings ▸ Agent ▸ Identity now changes the header and the agent
  switcher too (#2377).** On a fleet member's console the rename moved the tab title, the A2A
  card and the chat placeholder, but the switcher label kept the create-time name forever.
  Two sources, one writer: the Identity panel is agent-scoped, so it wrote the *member's*
  `identity.name`, while the header reads the *hub's* `/api/fleet`, whose label comes from that
  workspace's `workspace.yaml` record — and nothing wrote the new identity back to it. (The
  hub-side rename in Settings ▸ Fleet always stamped both halves, which is why only this path
  drifted.) The reload commit — the one choke point every identity change passes through — now
  restamps the record, so the settings save, `/api/config` and an out-of-band YAML edit all
  converge. The agent's immutable `id` (its URL slug, workspace dir and data scope) is
  untouched, so open windows, bookmarks and checkpoints survive.
- **The Identity panel no longer reports a save that was refused (#2377).** `/api/settings` and
  `/api/config` answer a rejected write with `200 {ok: false, messages}` — the server rolls the
  YAML back, leaving disk and the running agent on the old identity. The panel toasted
  "Identity saved · Agent reloaded" either way, so a rolled-back rename read as a console bug
  with nothing saying why. It now checks `ok` like every other settings panel and surfaces the
  server's reason.
- **Fleet start / stop / remove / activate address agents by their immutable `id`, not their
  display name (#2377).** A member can now rename *itself*, from a console that can't see its
  siblings' names, so display names are no longer guaranteed unique — an id can never act on
  the wrong agent. This also drops a whole `/api/fleet` round-trip from every member window's
  boot, which only existed to map the slug back to a display name.

- **A reload before setup is complete no longer 500s (#2379).** `_reload_langgraph_agent`
  skips the plugin build entirely while the wizard is still up, so `new_plugins` was never
  bound — but the commit block published `STATE.plugin_surfaces = new_plugins.surfaces`
  outside the `is_setup_complete()` guard every other read sits inside, raising
  `UnboundLocalError`. That took out every pre-setup reload path: installing or enabling a
  plugin during the wizard (its auto-enable reloads through here), `/api/config/reload`, and
  any `/api/settings` write — where the YAML write lands first, so the config saved while the
  caller saw a 500 and the running agent never picked it up. The wanted-surface set is now
  pre-seeded empty like its siblings, which is the same guard the neighbouring
  `new_middleware = []` was added for.

- **An agent name with spaces or punctuation now gets a normalized fleet label instead of a
  stale one (#2381).** Workspace display names are constrained to `[A-Za-z0-9_-]` because the
  fleet control plane accepts an agent by name as well as by id, but `identity.name` is
  free-form — "Merchant Bot" is a perfectly reasonable thing to call an agent. The member's
  self-sync refused those outright, so the agent renamed itself while the switcher stayed on
  the old label with only a log line to explain it. It now coerces (`Merchant Bot` →
  `Merchant_Bot`) and reports the label it saved. The agent keeps the name the operator typed;
  only the derived record is normalized. Genuinely unusable names — the reserved `host` slug,
  or one with no usable characters at all — are still reported and skipped rather than raised,
  since the identity is already committed and a stale label must never fail the reload.

- **Renaming an agent no longer orphans its inbox, background results and activity feed
  (#2382).** All three were keyed by `agent_name()` — the *editable* display name — so a
  rename silently pointed the agent at brand-new empty databases and its history appeared to
  vanish. Nothing was ever deleted (the old file sits right next to the new one, e.g.
  `inbox/traderAgent.db` beside `inbox/merchantBot.db`); the agent just stopped looking at it.
  The name was never the scope: `instance_root/<store>/` is already private to this instance,
  so the filename is a constant now and a rename can't move it. Existing installs keep their
  data — a lone pre-existing name-keyed store is adopted in place, with no move or copy. A
  workspace already carrying two of them from an earlier rename is genuinely ambiguous, so it
  starts clean and names both files in the log rather than guessing which history is current.
  A configured `inbox_db_path` directory stays namespaced by agent name, since several agents
  may be pointed at one on purpose. (The scheduler is keyed the same way *and* filters rows on
  `agent_name`; that half is tracked separately in #2382.)

- **Removing an agent from the fleet no longer deletes its data (#2384).** The console offered
  an opt-in "Also purge its workspace data (irreversible)" switch, but `remove` deleted the
  workspace directory either way — and for a fleet member that directory *is* its whole
  instance root (config, SOUL, chat checkpoints, knowledge, inbox, tasks, memory), because the
  supervisor spawns members with `PROTOAGENT_HOME=<ws>`. Leaving the switch off destroyed
  exactly the data it implied you were keeping. Remove now retires the agent — its record is
  renamed aside so it drops out of the fleet while every byte stays on disk, and renaming the
  record back restores it. Purge is the irreversible one the switch always described. The
  console and `workspace rm --purge` now say which of the two is about to happen.
- **The purge path honors `PROTOAGENT_BOX_ROOT` (#2384).** It was hard-coded to
  `~/.protoagent/<id>`, so on the desktop app — whose box root is under
  `~/Library/Application Support` — the purge branch could never find the legacy data scope it
  was meant to delete. It resolves through `infra.paths` now.

- **Renaming an agent no longer makes its scheduled jobs disappear (#2382).** The scheduler
  was keyed *twice* by the agent's editable display name: the `scheduler/<name>/jobs.db` path
  segment, and an `agent_name` column that `list_jobs` / `update` / `cancel` all filter on. A
  rename therefore pointed the agent at a brand-new empty database — and even aimed back at
  the right file it would still have shown an empty schedule. Nothing was ever deleted (the
  old directory sits right beside the new one). The default path is instance-private already,
  so its segment is a constant now, and rows written under a previous name are adopted when
  the store opens — every `jobs.db` is single-agent by construction, so every row in it is
  this agent's whatever name it was stored under. Existing installs keep their schedule: a
  lone pre-existing name-keyed store is used in place, and empty leftover directories from an
  earlier rename don't make that ambiguous. A configured `SCHEDULER_DB_DIR` / `db_dir` stays
  namespaced by agent name, since several agents may be pointed at one on purpose.

## [0.125.0] - 2026-08-05

### Added
- **Export an agent as a portable, secret-free snapshot (#2103, ADR 0091 Slice 1).**
  `protoagent agent export` (works on a stopped agent) and `POST /api/agent/export`
  (`{"dry_run": true}` for a review without the bytes) emit a zip carrying the agent's
  *recipe* — SOUL, secret-stripped config, `plugins.lock` SHA pins, MCP servers, and
  `SKILL.md` dirs — not a state dump. The bar is 12-Factor's: the artifact could be pushed
  to a public gist without leaking a credential, enforced by a test that greps the built
  zip's bytes for known secrets. Credentials the target must re-supply travel as a
  `required_secrets` inventory (names and descriptions, never values), and every export
  carries a `REVIEW.md` disclosing what was stripped — inside the artifact, so it can't be
  separated from what it describes. Import, knowledge seed, and the console UX are #2104,
  #2105 and #2106.

- **Import a snapshot to stand up a fresh agent (#2104, ADR 0091 Slice 2).**
  `protoagent agent import <zip>` and `POST /api/agent/import` rehydrate an agent from a
  Slice-1 snapshot — config, persona, skills and pinned plugins — through a new secret-free
  entry into the workspace scaffold that writes an **empty** credential overlay (never the
  `from_config` clone, which copies `secrets.yaml` verbatim).
  **Importing runs code**, so it is two-phase: without acknowledgement it returns a *plan*
  naming every plugin URL it would install (flagging unfamiliar sources), every capability
  the config grants (`filesystem.allow_run`, `operator.allowed_dirs`, `mcp.servers`), and
  every credential needed — and changes nothing. The CLI prints that plan and refuses to
  apply without `--yes`. Capability config is applied verbatim and surfaced rather than
  silently stripped (ADR 0071 D1 — trust, not sandbox). Hostile archives are refused before
  anything reaches disk: zip-slip, absolute-path and Windows-style traversal, zip bombs,
  oversized member counts, and unsupported snapshot versions. The new agent reports itself
  **incomplete** until the credentials the source agent actually had are supplied.

- **Opt-in knowledge seed in agent snapshots (#2105, ADR 0091 Slice 3).**
  `protoagent agent export --include-knowledge` (or `include_knowledge` on
  `POST /api/agent/export`) additionally carries the agent's knowledge as domain-tagged
  markdown — text, not the raw sqlite, since the source's embeddings were computed against
  *its* gateway. Import re-ingests it into the new agent's store, searchable immediately;
  the source docs are kept at `knowledge-seed/` so semantic recall can be added once a
  gateway is configured.
  **Off by default, because it changes what the artifact is.** A definition-only snapshot is
  publishable; one carrying knowledge is not — it holds no credentials and may still be the
  last thing you want public. `REVIEW.md` retracts the publishable claim at the top and
  lists every domain with its chunk count for review.
  **Memory is never included**, flag or no flag: what an agent recalls about a person's
  sessions is a different kind of data with a different consent question, and knowledge can
  be reviewed a domain at a time where accreted personal memory realistically cannot.

- **Duplicate an agent from a snapshot in the console (#2106, ADR 0091 Slice 4).**
  Settings ▸ Fleet ▸ **New agent** now has two sources: an archetype, or a **snapshot** —
  because "where do new agents come from" should be one question with two answers rather
  than two places. Pick a snapshot file and you get a *plan* before anything happens: every
  plugin it would install (with unfamiliar sources flagged), every capability its config
  grants, and the credentials the new agent needs. Nothing is written until you press a
  button that names what it will run — *"Install 2 plugins and create agent"* — because
  applying a snapshot clones those repos and runs their code in-process. Only credentials
  the source agent actually had are asked for, and an agent imported without them is
  reported as needing setup rather than "ready". New guide:
  [Agent snapshots](https://protolabs.studio/guides/agent-snapshots).

- **Settings ▸ Agent ▸ Snapshot — export an agent from the console (#2103).**
  The console surface for ADR 0091's secret-free export. It opens on the **review**, not the
  download: which credentials the target must re-supply (names only, marked *set here* vs
  *declared, unset*), what the pattern sweep scrubbed, and what was skipped — then downloads
  the zip on a second, deliberate click. Findings are split by what to do about them: a
  scrubbed credential is **still live in this agent** and wants rotating, while a scrubbed
  home path just needs re-pointing on the target. It sits last in the Agent group because it
  exports what every section above it configures.

## [0.124.0] - 2026-08-03

### Added
- **Copy a background job's full result from the Background-agents panel (#2352).**
  A finished job's expanded row gains a "Copy" button that writes the **whole** report to
  the clipboard — the panel hydrates from `GET /api/background/{id}`, so it copies the
  full text, not the 2,000-char `background.completed` preview. Reported by an operator
  hand-selecting several thousand words of a delegate's reply out of a scrolling markdown
  pane; the button sits above the result rather than inside it, so it stays reachable on a
  long report instead of scrolling away with the text.

### Fixed
- **A coding-agent delegate whose reply was cut short now says so, instead of handing back a partial answer that looks finished (#2352).**
  `AcpClient` records a `stopReason` on every turn — including `max_tokens`, the model
  stopping mid-generation at its output limit — and nothing in production read it.
  `delegate_to` returned the truncated text with no marker, so the orchestrating agent
  could not tell a cut-off reply from a complete one and acted on half an answer. ACP
  delegate replies that end in `max_tokens` or `refusal` now carry an explicit
  `[incomplete reply — …]` note telling the caller what happened and what to do about it
  (re-dispatch the remainder / restate the task). `end_turn`, `cancelled`, and a missing
  stop reason are untouched — a normal completion doesn't grow a scary marker, and an
  operator who hit stop already knows.

- **A server-fired turn that fails now says so in chat, instead of trailing off or vanishing (#2360).**
  Scheduled fires, watch reactions and background push-resumes never stream to the
  browser, so the `chat.resumed` push is the operator's only live view of them — and it
  carried no terminal state. A crashed turn arrived as an ordinary answer that stopped
  mid-sentence, indistinguishable from the agent finishing; one that crashed *before*
  saying anything hit an empty-text guard and was dropped entirely, leaving no trace that
  a turn had ever run while the reason sat unread on the task's terminal status. The push
  now carries `state` + `error`, the guard applies only to a completed turn, and a failed
  resume renders as a failure with the reason the server already recorded.

- **A server-fired turn is now watchable while it runs, instead of a typing indicator and then the whole answer at once (#2361).**
  Scheduled fires, watch reactions and background push-resumes self-POST into a chat
  session; the server holds that A2A stream, so the browser — which only renders turns it
  streamed itself — showed *"responding to a background trigger…"* and nothing else. One
  reported turn ran three minutes across 25 model calls and dozens of tool calls with no
  visible output, indistinguishable from a hang. Those turns now republish their tool
  frames **and their narration** as `chat.progress`, which the console folds into a growing
  assistant bubble; the terminal `chat.resumed` then replaces that preview in place rather
  than appending a duplicate. Gated on the turn's origin, so a turn the browser is
  streaming itself is never double-rendered, and published unretained so a long turn can't
  flush the event bus's replay ring.

- **A background delegate's reply now reaches the orchestrator whole, instead of being cut at 3,000 characters (#2363).**
  `delegate_to(background=True)` results were drained through ADR 0070 D2's report
  treatment — excerpted to `_BG_RESULT_CAP` with a "searchable via `memory_recall`"
  pointer. That cap is right for an *unsolicited subagent report*, but a delegate's
  reply is the **deliverable** the caller dispatched and is waiting on, so truncating
  it destroyed the work product and left operators hand-copying the rest out of the
  console (#2352). Foreground `delegate_to` was never capped, so the same reply arrived
  differently depending on whether the orchestrator held its turn open — background vs
  foreground is a transport choice, not a content policy. `spawn_work` jobs
  (`delegate_to`, `knowledge_ingest`) are now stamped `deterministic` at creation and
  delivered in full; `spawn` subagent-turn reports keep the D2 excerpt-plus-pointer
  shape unchanged. This also retires a false pointer (#2362): the truncation branch is
  now reachable only by the jobs that are actually indexed, so the notification can no
  longer send the model to an empty `memory_recall`.
