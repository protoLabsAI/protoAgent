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
