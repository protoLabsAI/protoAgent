# Conversations in Work — residual-risk review of unsupported tools, deployments and residual risks

Status: findings only. This document satisfies the proof gate's requirement that
CONTINUE rest on "an explicit review of unsupported tools/deployments and residual
risks" (see [conversations-in-work-proposal.md](./conversations-in-work-proposal.md),
"## Proof that turns REVISE into CONTINUE"). Josh asked for this review on
2026-11-17 ("yes"). It is the written review his CONTINUE decision rests on; it does
not itself decide anything.

## What this reviews, and what "private" means here

The proposal's privacy contract (decisions 1 and 2) is an **audience partition**:
the human operator's console steering and the agent's private replies to it are kept
off every peer surface, while the agent may still deliver *intended results* to the
original requester. "Private" therefore means *human-operator audience*, not the
pre-existing `incognito` ("no memory trail") flag. The two are different mechanisms
and this review keeps them apart:

- **`incognito`** (ADR 0069) suppresses *memory/knowledge writes and derived
  capture* for a chat. It is live on `main`. It says nothing about peer audiences —
  an incognito turn still streams to peers.
- **Audience partition** (ADR 0131 P1c / §5, amended by bd-jo3v6) is the privacy
  contract this proposal depends on. Its scaffolding exists on `main` but is **dormant
  / not wired to a route** (below); the adversarial proof matrix that would validate
  it has **not been executed** — the proposal is explicit that "this revision does not
  execute these tests".

Grounding: all citations are to `main` at `a7f16c40c` (the branch base), read in this
worktree. Where a mechanism exists but is dormant, this is stated.

### Partition machinery that exists on `main` (so the review is concrete about the boundary)

| Layer | Where | State |
| --- | --- | --- |
| Input queue audience tag (PEER vs PRIVATE) and audience-scoped drain | `src/graph/steering.py:55` (`AUDIENCE_PEER`/`AUDIENCE_PRIVATE`), `src/graph/steering.py:90` (`enqueue(..., audience=…)`), `src/graph/steering.py:147` (`drain(session_id, audience)`); consumed at `src/graph/middleware/steering.py:94` | Dormant — default is PEER and a peer item carries no tag, so ordinary turns are unchanged (docstring, `src/graph/steering.py:121`) |
| Checkpoint-message audience stamp and public projection for derived readers | `src/graph/conversation_audience.py:32` (`PRIVATE_AUDIENCE = "human-operator"`), `:44` (`AUDIENCE_KWARG`), `:47` (`message_audience`), `:66` (`public_messages`) — fail-closed on unknown/mixed provenance (`:16`) | Boundary module present; the derived-reader proof (#5403) is "currently missing" per the proof matrix |
| Task-store private partition (private input/reply persisted apart from public history) | `src/a2a_impl/stores.py:480` (`private_version`/`private_audience`/`private_payload` columns), `:486` (`PRIVATE_AUDIENCE`), `:497` (`task_public_filter`, refuses unknown audience/version at `:516`), `:623` (`save_private`) | Dormant — `save_private`'s own docstring: "No route calls this" (`src/a2a_impl/stores.py:626`) |
| Sealed private-file delivery (download authorized against sealed bytes, device-kind only) | `src/infra/media.py:140` (`bind_private_files`), `:149` (`private_media_content`), `:169` (`private_media_allowed`) | Delivery-reader seal present; scoped-download proof not executed |
| Observability suppression on private/incognito turns | `src/graph/middleware/trace_context.py:196`, `src/graph/middleware/prompt_capture.py:125` (and private-partition drop at `:130`) | Present; later-turn / fleet-export proof (P1g) not executed |

The gap this review exists to name: **the audience partition governs conversation
inputs, replies, derived readers, history projection and file delivery — it does not
sit on the egress surface of the agent's effectful tools.** A tool invoked while a
private segment is active runs with the agent's full credentials, and its arguments
and side effects are not audience-scoped.

## 1. Tools usable in a private segment, and whether private values can leave

Every tool the agent holds can be called during any turn, including a private
segment; there is no tool allow/deny list keyed on audience on `main`. The tool set
is assembled in `src/tools/lg_tools.py:1073` (`get_all_tools`). The only per-tool
privacy gate that exists is the `incognito` memory check, and it is present on only
some tools (see below). "Can private values leave outside a delivered result?" below
means: can a value the operator intended to stay human-operator-private reach an
external system, a peer surface, or a persisted store that a peer/derived reader can
later read — through this tool, when the tool's output is *not* the sealed delivered
result.

### Effectful / unsandboxable tools

| Tool | Defined at | Effect / egress | Audience-gated on `main`? | Can a private value leave outside a delivered result? |
| --- | --- | --- | --- | --- |
| `run_command` (shell) | `src/tools/fs_tools.py:1780`; impl `src/tools/shell.py`; gated by `filesystem.allow_run` (`src/tools/fs_tools.py:1179`), "unsandboxed … arbitrary argv as the server user" (`:1181`) | Arbitrary subprocess: network, filesystem, any binary | **No** — only `incognito` check in `fs_tools.py` is the editor hand-off at `:925`; `run_command` has none | **Yes.** A private value passed in a command (or written to a file, or POSTed by a spawned process) leaves with no audience check. Its stdout/stderr return into the transcript. |
| File writes — `write_file` / `edit_file` / `delete_file` | `src/tools/fs_tools.py:1562` / `:1586` / `:1621` (set `_WRITE_TOOLS` `:1077`; `delete_file` HITL-gated) | Durable filesystem mutation outside the conversation store | **No** — no `incognito`/audience gate on the write tools (grep confirms line 925 is the only `incognito` read in the file, and it guards the editor hand-off, not writes) | **Yes.** A private value written to disk persists outside the partition; any later reader (including a non-private turn, or `read_file`) can surface it. |
| `web_search` / `fetch_url` | `src/tools/lg_tools.py:433` / `:536`; egress allowlist `src/security/egress.py:121` (`check_url`) | Outbound HTTP(S) to third parties | **No** audience gate; egress allowlist checks *destination*, not *audience* | **Yes.** A private value placed in a query string or request body is sent to an external host. The allowlist can block the host but cannot know the value is private. |
| `task` / `task_batch` (delegate to subagent) | `src/graph/agent.py:1982`; `incognito` rides into the subgraph (`:1624`) and onto background jobs (`_incognito_from`, `:796`; `origin_incognito`, `:2023`) | Spawns a subagent (optionally detached/background) with the same tool access | `incognito` propagates; **audience does not** | **Partly.** The no-memory-trail flag is carried, but the subagent is not told the parent turn is a private segment, and its own effectful tools are ungated. A delegated run can leak exactly as the rows above. |
| MCP-bridged tools (managed / client MCP) | wiring `src/tools/mcp_tools.py` (HTTP transport `:142`, stdio subprocess `:160`, env filter `:165`); outbound exposure profile `src/runtime/operator_mcp_tools.py` (`_MCP_INCOMPATIBLE` `:37`, read-only profile `:41`/`:85`, `execute_code` star-excluded `:30`) | Calls an external MCP server (local subprocess or remote HTTP); tool args cross that boundary | **No** audience gate | **Yes.** Any argument sent to a bridged MCP tool crosses to that server. The `read-only` profile limits what *this agent exposes over the bus*; it does not limit what the agent *sends out* to the MCP servers it consumes. |
| `execute_code` kernels (plugin) | `plugins/execute_code/engine.py:673` (`build_execute_code_tool`), `:754` (`execute_code`) | Runs code in a persistent kernel: network + filesystem, like `run_command` | **No** audience gate (off by default; a plugin, not core) | **Yes**, where enabled — same egress class as `run_command`. |
| `schedule_task` / `cancel_schedule` | `src/tools/scheduler_tools.py:106` / `:238` | Persists a scheduled turn that later runs (ordinary, non-private) | **No** gate at all (`grep -c incognito` = 0) | **Yes.** A private value captured into a scheduled task's prompt re-enters later as an ordinary turn with peer audience. |
| `memory_save` / `knowledge_ingest` | `src/tools/memory_tools.py:597` / `:647` | Writes to the memory/knowledge store (a derived reader surface) | **`incognito`-gated** — refuses with `_INCOGNITO_REFUSAL` (`src/tools/memory_tools.py:52`, checks `:505`/`:672`) | Suppressed under `incognito`. But `incognito` ≠ private audience: a private segment that has *not* set `incognito` would still write. |
| `memory_forget` | `src/tools/memory_tools.py:960` | Deletes memory rows | **No** `incognito`/audience gate | Deletion, not egress; noted for completeness — a private segment can mutate shared memory. |

### Non-effectful / interrupt tools (for completeness)

- `ask_human`, `request_user_input`, `request_decision` — HITL interrupts
  (`src/tools/lg_tools.py:98`/`:120`/`:166`); grouped as `HITL_TOOL_NAMES`
  (`:1061`) and denied to subagents via `SUBAGENT_DENIED_TOOLS` (`:1070`). They pause
  the turn; no external egress. Their resumed answer is operator-supplied and belongs
  to the private audience by construction, which is why the proof matrix's
  "preexisting handles" and "idle and parked/resume" rows assert on them.
- `current_time`, `calculator`, `load_skill` (`:1001`), `show_component` (`:254`),
  `read_file`/`list_files`/`search_files` — read-only/local; no egress.

### The one material tools finding

The audience partition is enforced at the conversation, derived-reader, history and
delivery layers, but **not at the effectful-tool egress layer**. Every effectful tool
above can carry a private value out of the partition — to an external host, to disk,
to a child subagent, to an MCP server, or into a later ordinary turn — and only
`memory_save`/`knowledge_ingest` are gated at all (and only by `incognito`, which is
not the same boundary). The proposal's own matrix anticipates this as the
**P1e "Other exits and termination"** row ("private tool/effect restrictions … Deny
exits that are not part of a delivered result"), and that row is **not covered** — the
revision does not execute it. Until P1e ships and is proven, a private segment that
calls an effectful tool is outside the privacy promise.

## 2. Deployments, and where credential and tool isolation cannot be established

Classification of a caller into operator / peer / fleet / federation is the hinge the
whole privacy contract turns on (P1a). It lives in `_classify_token`
(`src/a2a_impl/auth.py:502`), which distinguishes the principal kinds enumerated at
`src/a2a_impl/auth.py:388`: `shared-operator-bearer` (`:518`), `device` (`:535`),
`fleet-service` (`:526`), `agent-device`, `federation` (`:521`), `legacy-api-key`
(`:1162`), `open/unknown`. "Human operator" for the private audience is
`shared-operator-bearer` or `device`-kind (`src/a2a_impl/auth.py:401`).

| Deployment | How it runs (file:line) | Where isolation cannot be established |
| --- | --- | --- |
| **Single agent (default/desktop)** | Loopback bind, open mode when no bearer/API-key is configured: `open_mode` `src/a2a_impl/auth.py:446`, open credential rated operator (`_OPEN_CREDENTIAL`, `:419`; `bearer_tier` `:432`); non-loopback bind requires a token (`evaluate_open_bind`, `:547`) | **Open loopback = every caller is operator with no credential.** Any local process reaching the loopback port reads as the human operator and could read private history. This is the accepted desktop trust model, but it means credential isolation *cannot* be established on an open instance. |
| **Fleet hub → local member** | Hub injects the fleet service token into the member's env (`src/graph/fleet/supervisor.py:335`; `A2A_AUTH_TOKEN` fallback `:357`); on proxy the hub swaps the caller's credential for the fleet token (`src/graph/fleet/proxy.py:863`), then re-narrows scope with a header (`:872`) | **The fleet token is one shared operator-tier credential (`fleet-service`, `src/a2a_impl/auth.py:526`).** The member cannot tell a human operator from a device from an automated self-call behind the hub; per-device revocation does not reach the member. Scope narrowing is orthogonal to identity. |
| **Remote member** | Resolved as remote, carries its own stored bearer, never the fleet token (`src/graph/fleet/proxy.py:243`/`:269`/`:280`; "a remote never gets the hub-signed token" `:352`) | **The original transcript lives on the owner's host**, not reachable here (proposal participant table, row "Remote A2A thread"). The stored bearer is shared across all hub→remote calls; no per-request human-operator identity crosses the hop. Private steering of a remote thread needs owner-side guarantees that do not exist. |
| **Open / loopback mode** | As single-agent above; classification is bypassed entirely in open mode (`open_mode` short-circuits `bearer_tier`, `src/a2a_impl/auth.py:446`) | Same as single-agent: no principal distinction is made, so the operator-vs-peer boundary the partition needs is absent. |
| **Federation** | Federation token classified `federation` (`src/a2a_impl/auth.py:521`); denied all `/api` operator paths (`_requires_operator` `:352`, 403 at `:1038`); collapses to operator if misconfigured equal to the bearer | **A federation (or any peer-tier) caller is never the human operator and must never receive private material.** This is the correct fail-closed direction, but it also means a federated peer's thread is categorically unsteerable-in-private. If `federation_token == bearer`, the tiers collapse and the distinction is lost. |

**Josh's Q2 decision is recorded, not reopened.** On 2026-11-16 Josh accepted the
risk that private steering stays enabled everywhere, including where credential and
tool isolation cannot be established, with no per-deployment disable switch (proposal,
decision block for Q2; "accept the risk … yes keep it on everywhere"). This review
records that the open-loopback and shared-fleet-token cases above are exactly the
"isolation cannot be established" surfaces that decision covers. It does not propose
changing that decision.

## 3. Residual risks the proof slices and tests do not cover

1. **Effectful-tool egress (P1e).** As in §1 — the partition is not on the tool
   egress surface; no executed test asserts a private value cannot leave through
   `run_command`, a file write, `fetch_url`/`web_search`, an MCP call, `execute_code`,
   `schedule_task`, or a delegated `task`. The P1e scenario is defined but unexecuted.
2. **`incognito` is not the audience boundary.** The only per-tool privacy gate on
   `main` is `incognito` (memory/knowledge writes), and a private segment need not be
   incognito. Conflating the two would over-claim coverage. The derived-reader module
   (`src/graph/conversation_audience.py`) correctly keys on the message audience, not
   the thread `incognito` flag (`:1`–`:20`), but its end-to-end proof (#5403) is
   "currently missing" per the matrix.
3. **Dormant partition, no route.** `save_private` persists the private partition but
   "No route calls this" (`src/a2a_impl/stores.py:626`), and the steering audience tag
   defaults to PEER with no live producer of PRIVATE items. The contract is scaffolded,
   not wired; nothing on `main` actually produces or enforces a private audience end to
   end yet.
4. **Deployment isolation gaps** (open loopback, shared fleet token, remote owner
   host) — per §2, accepted by Josh (Q2) but still the surfaces where the promise
   cannot be mechanically guaranteed.
5. **Retention and `×` delete — deferred to the retention phase.** The proposal defers
   retention and chat deletion to a later phase. Decision 3 says `×` "delete still
   happens as today" and Work then shows the original as unavailable (proposal,
   "Loading, failures and missing history" table, row "Original no longer stored"); the
   matrix's "Missing/stale state and deletion" row requires `×` to delete **both**
   partitions and leave Work unavailable, and that stale derived readers cannot
   resurrect them. **None of that retention/two-partition-delete behavior is built or
   tested on `main`** — it is explicitly a later phase. This is the single largest
   not-covered area and is called out so CONTINUE is not read as covering it.
6. **Later ordinary turns and restart.** The matrix requires private canaries to stay
   absent across compaction, restart, and a subsequent `incognito=False` turn (derived
   readers, observability export). The suppression hooks exist
   (`src/graph/middleware/prompt_capture.py:130`, `trace_context.py:196`,
   `src/graph/projection.py:481`) but the cross-turn/cross-restart assertions are
   unexecuted.

## 4. Findings table

Severity reflects residual exposure of a human-operator-private value **if the
partition shipped as scoped today**, not the likelihood Josh's accepted deployment
risks occur. "Not covered" means no executed test asserts the privacy property; where
a proof-matrix scenario names the intended future test, it is noted.

| # | Risk | Where (file:line) | Covered by | Severity |
| --- | --- | --- | --- | --- |
| F1 | Private value leaves via `run_command` subprocess (no audience/incognito gate) | `src/tools/fs_tools.py:1780`, `:1181`; only incognito read is the hand-off at `:925` | Not covered (P1e scenario, unexecuted) | High |
| F2 | Private value persisted to disk via `write_file`/`edit_file`/`delete_file` (no gate) | `src/tools/fs_tools.py:1562`/`:1586`/`:1621` (`_WRITE_TOOLS` `:1077`) | Not covered (P1e) | High |
| F3 | Private value sent to a third-party host via `fetch_url`/`web_search` (egress allowlist checks destination, not audience) | `src/tools/lg_tools.py:433`/`:536`; `src/security/egress.py:121` | Partially — `tests/test_egress.py` covers the destination allowlist only; audience not covered | High |
| F4 | Private value crosses to an MCP server via tool args | `src/tools/mcp_tools.py:142`/`:160`; exposure profile `src/runtime/operator_mcp_tools.py:37`/`:85` | Not covered (P1e) | Medium |
| F5 | `execute_code` kernel egress (where the plugin is enabled) | `plugins/execute_code/engine.py:673`/`:754` | Not covered (plugin off by default) | Medium |
| F6 | Private value re-enters as a peer-audience turn via `schedule_task` | `src/tools/scheduler_tools.py:106` | Not covered (no incognito/audience gate) | Medium |
| F7 | Delegated subagent (`task`/`task_batch`) not told the segment is private; its tools are ungated | `src/graph/agent.py:1982`, `:1624`, `:2023` | Partially — `incognito` propagation has tests (`tests/test_background_results.py`, `tests/test_projection.py`); audience propagation not covered | Medium |
| F8 | Open-loopback instance rates every local caller operator — no credential isolation | `src/a2a_impl/auth.py:446`, `:419`, `:432` | Covered as behavior by `tests/test_a2a_auth.py`; risk **accepted** by Josh (Q2) | Accepted (High if unaccepted) |
| F9 | Shared fleet service token indistinguishable per-device at a local member | `src/graph/fleet/proxy.py:863`; `src/a2a_impl/auth.py:526`; `src/graph/fleet/supervisor.py:335` | Behavior covered by `tests/test_fleet_proxy.py`; privacy implication accepted (Q2) | Accepted (Medium) |
| F10 | Remote member thread not reachable/steerable-in-private; owner-host boundary | `src/graph/fleet/proxy.py:243`/`:280`/`:352` | Covered by proposal participant table (row marked Unavailable); no copied fallback | Low (scoped out of v1) |
| F11 | Federation/peer tier must never receive private material; collapses if token == bearer | `src/a2a_impl/auth.py:521`, `:352`, `:1038` | Covered by `tests/test_a2a_auth.py` (tier ceiling) | Low |
| F12 | Audience partition is dormant — `save_private` has no caller; steering PRIVATE tag has no live producer | `src/a2a_impl/stores.py:626`; `src/graph/steering.py:121` | Not covered end-to-end (proof matrix explicitly unexecuted) | High (until wired + proven) |
| F13 | Derived readers (harvest, compaction archive, digest, facts) could learn private material on a later turn | `src/graph/conversation_audience.py:47`/`:66`; `src/graph/projection.py:481` | Not covered — matrix calls this the "currently missing #5403 execution evidence" | High |
| F14 | Observability (trace, prompt capture, fleet export) across later turns / restart | `src/graph/middleware/trace_context.py:196`; `src/graph/middleware/prompt_capture.py:130` | Partially — suppression hooks present; cross-turn/restart assertions (P1g) not covered | Medium |
| F15 | Scoped private-file download seal | `src/infra/media.py:149`/`:169` | Not covered (P1e scoped-download proof unexecuted) | Medium |
| F16 | **Retention + `×` delete of both partitions — deferred to the retention phase** | Proposal "Loading, failures and missing history" (× row); matrix "Missing/stale state and deletion" | Not covered — deferred; nothing built or tested on `main` | Deferred (High if treated as covered) |

## Closing

This review records findings. It does not decide CONTINUE; that is Josh's decision.
