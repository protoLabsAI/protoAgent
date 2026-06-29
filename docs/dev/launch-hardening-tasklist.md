# Launch-hardening tasklist

Pre-launch security/correctness hardening, derived from the antagonistic multi-agent
review of 2026-06-28 (13 finder dimensions → every finding double-verified by an
independent reachability skeptic + impact skeptic; 30 findings survived, 0 refuted,
several severity-recalibrated downward by the verifiers).

Organized by **churn risk** (regression blast-radius of the *fix*) × **LOE** (effort to
land it well), not by raw severity — so it doubles as an execution order. Severity is
kept as a tag.

**Scales** — LOE: `XS` 1–3 lines · `S` one function/<1h · `M` multi-file+tests/hours ·
`L` design + broad regression/day+. Churn: `Low` isolated/additive · `Med` shared
helper or trafficked path · `High` auth semantics, behavior-changing *default*, or
hot streaming/concurrency path.

**Default-posture context** (why most criticals land at high, not critical): binds
`127.0.0.1` by default; boot-gate refuses a non-loopback bind without a token
(`auth.evaluate_open_bind`); `/a2a` and `/v1` are default-deny. Most high-severity
items require *installing a malicious plugin*, *a shared A2A/API token*, or *deliberate
network exposure*.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[>]` deferred (post-launch).

---

## Batch 1 — Low churn × Low LOE → ship now (one PR off `main`)

Additive guards / one-liners; near-zero regression risk, high security ROI.

- [x] **Ingestion SSRF guard** — `High` · churn Low · LOE S — `ingestion/engine.py:334-398`.
  Route `extract_url`/`_http_fetch` through the existing `security.egress.check_url`
  (as `fetch_url` already does), set `follow_redirects=False` and re-check every hop.
  Closes internal/metadata-fetch-into-KB. *(Two finders flagged this; asymmetric with
  the already-guarded `fetch_url` tool.)*
- [x] **`KnowledgeStore` missing `busy_timeout`** — Med · Low · XS — `knowledge/store.py:273`.
  `PRAGMA busy_timeout=5000` in `_connect` (concurrent writes silently lost today;
  error swallowed at `400-402`).
- [x] **Scheduler raw `database is locked`** — Low · Low · XS — `scheduler/local.py:245`.
  Same PRAGMA in `LocalScheduler._connect`.
- [x] **Non-constant-time credential compare** — Low · Low · XS — `a2a_impl/auth.py:218`,
  `operator_api/console_handlers.py:389`. Use `hmac.compare_digest` for X-API-Key and
  the inbox token (bearer already does).
- [x] **`requestForm` double-body-read masks 401** — Med · Low · XS–S —
  `apps/web/src/lib/api.ts:345`. Read body once (`text()` then best-effort
  `JSON.parse`); restores the real error + the 401 AuthGate on uploads.
- [x] **Boot TTL sweep deletes HITL tasks** — Low · Low · XS–S —
  `a2a_impl/stores.py:291`. Exclude `INPUT_REQUIRED`/`AUTH_REQUIRED` from
  `sweep_expired_tasks` (mirror reconcile's preserved states).
- [x] **`requires_pip` arg injection** — Med · Low · S — `graph/plugins/installer.py:630`.
  Validate each entry as a plain PEP 508 requirement: reject leading `-`
  (`--index-url`/`-e`), reject VCS/URL/`@`/`file:` refs, pass `--` before specs.
- [x] **Subagent return-value mis-parse** — Low · Med · S — `graph/agent.py:278`.
  Select the last `AIMessage` (not "any message with content"); drop the
  `startswith("Error")` heuristic — use the `SubagentError` path for failures.
- [>] **Palette deep-link dead-ends on workspace console** — Low · Low · S —
  `apps/web/src/app/usePaletteRegistry.ts`. *(Deferred: `_link` registers at module-import,
  so an `isHostConsole()` gate there is timing-fragile; the clean fix is a run-time guard in
  `SettingsSurface`. Cosmetic — low priority.)*
- [ ] **SSE token in URL** — Low · Low · XS — `apps/web/src/lib/events.ts:70`. Scrub
  `token` from server access logs (cookie-bound SSE token is a bigger change; defer).
  Already mitigated by 30s HMAC TTL + `/api/events`-only scope.

## Batch 2 — Med churn × Low–Med LOE → contained, needs regression tests (one PR each)

- [x] **Plugin `public_paths` prefix-match auth bypass** — `High` · churn Med · LOE S–M —
  `graph/plugins/manifest.py:141-146` + `a2a_impl/auth.py:88-100`. Boundary-less
  `startswith` lets a plugin with `id: install` + `public_paths:["/api/plugins/install"]`
  strip the bearer gate off the core install (RCE) route. Fix: require a trailing-slash
  boundary (`/api/plugins/{id}/`), validate `plugin_id` against `^[a-z0-9][a-z0-9_-]*$`,
  reserved-name denylist (`install`,`installed`,`sync`,`updates`,`catalog`,`enabled`).
  Test that legit plugin public pages still pass.
- [x] **Secret-redaction fail-open (a)+(b)** — Med×2 · Med · S–M — `graph/config_io.py` +
  `graph/plugins/pconfig.py`. Root cause: discovery-empty was indistinguishable from
  discovery-failure. Added `strict=True` discovery that PROPAGATES errors: (a) `GET
  /api/config` now blanks the whole plugin section on discovery failure (fail-safe);
  (b) `secret_paths()` `#877` cache fallback actually triggers now (was dead).
  - [>] **(c) MCP inline env/header secrets** unredacted in `config_to_dict` (`386-391`) —
    deferred (Low; live YAML is gitignored, and masking needs save-roundtrip / blank-means-
    unchanged handling so a re-save doesn't clobber the stored value).
- [x] **Plugin install/update/sync block the event loop** — `High` · Med · S–M —
  `operator_api/plugin_routes.py:192,324,337,382` → `graph/plugins/installer.py`.
  `await asyncio.to_thread(installer.…)` in the handlers + bounded subprocess timeouts
  on clone/ls-remote. Self-DoS: one install freezes all chat/A2A/scheduler.
- [x] **`data` goal-verifier `eval()` escapable sandbox** — Low (adj) · Low–Med · S —
  `graph/goals/verifiers.py:178-184`. Replace `eval()` with the AST-whitelist approach
  from `tools/lg_tools.py:_safe_eval` (reject `Attribute`/`Call`/comprehensions); fix the
  misleading "blocks exec/eval" comment. *(Low on its own — the sibling `command`
  verifier already gives RCE on the same surface — but see Batch 3 trust-gate.)*

## Batch 3 — High churn × Med LOE → behavior-changing defaults / cross-surface signature

- [x] **Gate `/metrics` behind auth** — Low · Med (*ops*) · XS — `a2a_impl/auth.py:51`.
  Public only in open mode (no bearer & no api-key) or via `PROTOAGENT_PUBLIC_METRICS=1`
  opt-out. **Breaks anonymous Prometheus scrapers on token-gated deploys** — they must
  send `Authorization: Bearer` or set the opt-out. *(Authorized for launch; shipped on
  this branch.)*
- [x] **Strip secrets from stdio MCP subprocess env** — `High` · High · M —
  `tools/mcp_tools.py:108-118`. Default (`inherit_env` unset) → secret-filtered
  passthrough (strip `*_TOKEN`/`*_SECRET`/`*API_KEY`/`*PASSWORD`/`*_KEY` + DSN/DB
  connection-strings + SSH/Kerberos/GPG agent sockets; base-URLs kept); `inherit_env:
  true` = explicit full passthrough escape hatch; `inherit_env: false` = minimal.
  **Breaks servers that relied on an implicitly-inherited secret env var** — they set
  `inherit_env: true` or a per-server `env:`. *(Authorized for launch; shipped on this
  branch.)*
- [>] **"Operator-only" goal trust gate** — `High` · High · M — `server/chat.py:692,1036`
  → `graph/goals/controller.py:98`. Thread `trusted: bool` from the calling surface into
  `parse_control`; refuse `command`/`test`/`ci`/`data` verifiers on the A2A and `/v1`
  paths (route through `set_goal_safe`'s allowlist). Today a shared-token A2A peer gets
  `bash -c` on the host. Pairs with the Batch 2 `eval` fix.
  **DEFERRED — needs a decision (surfaced to the user):** the console's *streaming* chat
  goes through `/a2a` with the operator bearer (ADR 0045), i.e. the SAME path + SAME token
  a federated peer uses — so console-vs-peer is indistinguishable by code-path or token. A
  clean gate would break the operator's own console `/goal command`, and the `/a2a`
  federation vector (the main one) can't be gated without a **separate operator-vs-federation
  token** model. (`data`'s eval escape is already closed in Batch 2, so `data` is no longer
  an RCE sink — only `command`/`test`/`ci` remain.)
- [x] **ACP runtime eviction race** — Med · Med–High · M — `server/chat.py:102-141`.
  Fixed: `asyncio.Lock` around all registry mutation, an `_ACP_BUSY` refcount so eviction
  never closes an in-flight runtime (idle TTL + LRU cap both skip busy), `pop(tid, None)`
  safety, and `_acp_acquire`/`_acp_release` helpers. The ACP turn body was extracted to
  `_acp_drive_turn` so the A2A handler wraps it in acquire/try-finally/release without a
  deep reindent.

## Batch 4 — High churn × Med–High LOE → design-first, isolate (own initiative)

The chat-finalization + concurrency cluster sits on the hottest paths; the codebase has
been burned here before (the `#1328` native-reasoning rewrite, the CSS-minifier trap).
**Coordinate with inflight DRAFT PR #1394 (`fix/subagent-stream-isolation`)** — it
touches the same chat/subagent streaming surface.

- [>] **`extract_output` truncates on literal `<think>`/`<scratch_pad>`** — Med · High · M
  — `graph/output_format.py:104-137,421`. Strategy-3 balanced-only stripping + a
  `(?<!\`)` backtick guard; must preserve leaked-reasoning stripping (LiteLLM #22392).
- [>] **Dropped empty-turn → silent empty answer (streaming)** — Med · High · M —
  `server/chat.py:919-997`. Detect empty-text-and-no-tool-call; give the streaming path
  the non-streaming path's empty-answer fallback.
- [>] **A2A artifact append-vs-replace divergence** — Med · High · M —
  `a2a_impl/executor.py:308-345`. On the terminal frame, replace the answer artifact
  (`append=False`) when `final_text` differs from what streamed — the docstring already
  claims this; the impl appends.
- [>] **Concurrent A2A turns corrupt history** — Med · High · M —
  `a2a_impl/executor.py:366-373`, `server/chat.py:862`. Serialize per `context_id`
  (`asyncio.Lock`/queue) before `astream_events`, mirroring the console steering queue.
- [>] **chat-store cross-tab clobber** — Med · Med–High · M —
  `apps/web/src/chat/chat-store.ts:141`. `storage`-event merge (union by id, newest
  `updatedAt` wins) or a `BroadcastChannel` single-writer.
- [>] **`localStorage` bearer (XSS-exfiltratable)** — Low · High · L —
  `apps/web/src/lib/auth.ts:42`. Move to an httpOnly SameSite cookie, or accept-risk +
  guarantee no raw-HTML/JS render sink (none found today — DS markdown only). Interim:
  document as a known sink.

## Doc-only / optional

- [ ] **Plugin iframe `allow-scripts`+`allow-same-origin`** — Low · Low · XS —
  `apps/web/src/app/PluginView.tsx:266` (+ `App.tsx:529`, `Launcher.tsx:67`). No
  functional change (plugins are trusted code, given the bearer by design); document the
  trusted-code contract + standardize the sandbox string in one helper so the three call
  sites can't drift.
- [ ] **`MiddlewarePanel` removal lost a runtime diagnostic** — Info — settings-IA branch.
  Fold a read-only wired-middleware status strip into the Behavior/Overview panel from
  `runtime.middleware`, or note the intentional removal in ADR 0048.

---

## Sequencing notes

- **Effort ≠ risk.** `/metrics` and the MCP-env fix are tiny diffs but high churn — they
  break *working external setups*, so they need coordination + a migration note, not just
  a commit. The ingestion SSRF (High severity) is Batch-1-easy.
- **Three clusters share a root** — keep each as one PR: the secret-redaction trio
  (discovery-empty ≠ failure), the plugin-blocking-loop pair (`install` + `updates`), and
  the chat-finalization quintet.
- **Batch 1 + ingestion SSRF** is the "merge today" set: ~10 additive fixes closing one
  High + several Meds at near-zero regression risk.
- All work lands via the `feat/launch-hardening*` worktree off `main` — never the inflight
  `feat/settings-ia-domain-first` (PR #1393) tree.
