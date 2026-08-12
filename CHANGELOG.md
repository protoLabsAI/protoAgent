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
