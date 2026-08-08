# Plan: system prompt viewer — the exact prompt for any turn, per-chat

**Status:** P1 + P2 shipped (2026-07-24) — capture middleware + store + routes +
View-prompt dialog + `/prompt` + delete-purge, plus composer-annotated section
budgets (`*_parts()` composers, per-section storage, breakdown UI). P3 shipped 2026-08-08 (#2388):
section-level diffs vs previous call/turn, subagent capture nested under the delegating
tool-call id, and the explicit `/api/prompts/preview` speculative route
(`compose_context(record=False)` — no injection-log write). Tracking issue:
[#2243](https://github.com/protoLabsAI/protoAgent/issues/2243).
**Owner:** console + `graph/middleware` + `observability`.

Most agent systems hide the system prompt; seeing what the model actually received
means wiring up tracing. protoAgent's ethos is that the operator owns everything —
and the prompt is the most consequential hidden state there is. This plan gives every
chat a way to see the **exact** prompt for any turn, plus where the context budget went.

## The decision that shapes everything: capture, don't reconstruct

The system prompt is NOT one static thing per agent. The static half
(`graph/prompts.py::build_system_prompt` — SOUL → subagent rules → managed projects →
operating model → operator guidelines) is fixed at graph build; the **dynamic half**
(`state["context"]`: the `<injected_memory>` envelope, the MRU-ordered
`<available_skills>` index, `<working_state>`) is authored per call by
`KnowledgeMiddleware.before_model` and folded into the system message per call by
`PromptCacheMiddleware.wrap_model_call` (`prompt_cache.py:67-87`). Reconstructing
after the fact would quietly lie whenever state has moved. So: **snapshot at call
time**, at the one seam where the final request exists.

## Architecture

### Capture middleware (new: `graph/middleware/prompt_capture.py`)

A `PromptCaptureMiddleware` with `wrap_model_call`/`awrap_model_call`, appended in
`_build_middleware` (`graph/agent.py`) **immediately after `PromptCacheMiddleware`**
— the only point where `request.system_message` is final (TraceContext only touches
`extra_body`; ToolDeferral only trims `tools`; nothing downstream mutates the prompt).

Capture is cheap because the cache boundary already marks the split
(`prompt_cache.py:78-81`):

- `blocks[0]` = the stable `build_system_prompt()` blob (carries `cache_control`) —
  **stored once, keyed by content hash** (same idea as `soul_rev`,
  `telemetry_store.py:44`)
- `blocks[1]` = the dynamic tail (`state["context"]`) — small, stored per call

Because `wrap_model_call` wraps the handler, the **response** is in-hook too: store
real per-call `usage_metadata` (input/output/cache tokens — the `stream_usage=True`
plumbing from `graph/llm.py:132` already guarantees it).

Best-effort like the injection log (`knowledge.py:462-463`): a capture failure
debug-logs and never touches the turn.

### Correlation (the one real gap found)

In-hook ids: `session_id` (state / `tracing.current_session_id()`) and
`trace_id`/`span_id` (`tracing.current_trace_context()`). **`task_id` is NOT in-hook**
— it lives at the A2A turn boundary. P1 threads it via the request-context metadata
(`runtime/request_context.py`) from the executor, so snapshot rows key as
`(task_id, call_index)` — matching the telemetry PK **and** the `taskId` the frontend
already stamps on every assistant bubble (`ChatSurface.tsx:1413-1424`). Fallback when
absent (non-A2A callers): key by `(session_id, trace_id)` and join to `task_id`
through the telemetry row at read time (it stores both).

### Store (new: `observability/prompt_snapshots.py`)

Telemetry-style scaffolding (WAL, busy_timeout, connection-per-call, idempotent
`ALTER TABLE` migrations), instance-scoped at `instance_paths().store("prompt-snapshots.db")`:

```sql
stable_blobs(hash TEXT PRIMARY KEY, text TEXT, created_at TEXT)
calls(id INTEGER PK, task_id TEXT, session_id TEXT, trace_id TEXT,
      call_index INTEGER, ts TEXT, stable_hash TEXT, context_text TEXT,
      model TEXT, input_tokens INT, output_tokens INT,
      cache_read_tokens INT, cache_creation_tokens INT)
-- indexes: (task_id), (session_id), (ts)
```

**Retention: the `metrics_store` in-write cap** (`metrics_store.py:82-110` — age AND
count trimmed inside the write transaction, no maintenance loop): default
`retention_days=30`, `max_calls=5000`, `<=0` disables. Config knobs `prompts.capture`
(default **on** — "no hidden prompt" is the point; capture cost is a hashed blob +
a small tail per call) and `prompts.retention_days`, wired like
`telemetry.retention_days` (`settings_schema.py:668-678`). Orphaned stable blobs swept
opportunistically on trim. **Resolves open question 1: in-write age+count cap, not a TTL sweep.**

### API (new: `operator_api/prompt_routes.py`)

- `GET /api/prompts/{task_id}` → `{enabled, calls: [{call_index, ts, model, system:
  {stable, context}, sections?, usage}]}` — injection-detail ergonomics
  (resolve-hash-on-read, 404 on miss, `{enabled: false}` when off).
- `GET /api/prompts/last?session_id=` → the session's most recent captured call
  (backs `/prompt`).
- Registered in `server/__init__.py` beside the injection/telemetry registrars.
  Operator `/api` only — never `/v1` or A2A.

## UX

**Per-message (primary):** a "View prompt" `MessageAction` in the existing action row
(`ChatMessageView.tsx:176-203` — Copy/Fork/Rewind live there; `FileText` icon already
imported), on every assistant message, keyed on `message.taskId`. Opens a
`DocumentViewer`-style dialog (`docviewer/`, `width=min(1100px, 96vw)`, scrollable
body) with a segmented `Tabs` strip per model call (the `McpCatalogDialog` pattern)
— monospace prompt text, a per-tab Copy button (`copyMessage`/Telemetry clipboard
pattern), and the call's real token usage via the `UsageFooter` formatters.

**`/prompt` (secondary):** a **client-side** slash command via `registerSlashCommand`
(`coreSlashCommands.ts` — the `/btw` template): shows the prompt **as of this
session's last model call** as an ephemeral system note (never saved to the thread;
system notes are client-store-only), with an "open full viewer" affordance. Client
commands are operator-only by construction — they short-circuit before the agent
ever sees them. *Honest framing:* a true "as it would be next call" preview requires
speculatively running knowledge retrieval; that's P3, not P1 — "last call" is exact
and cheap.

## Segmentation (P2) — resolves open question 2

**Annotate at the composer, not marker-parse.** Both composers already build lists:
`build_system_prompt` joins a `parts` list (`prompts.py:175`), and
`KnowledgeMiddleware` joins its own parts (`knowledge.py:396`). Add sibling
`*_parts()` variants returning `[(label, text)]` (join for the existing callers —
zero behavior change), and capture stores `sections: [{label, chars, approx_tokens}]`
using the chars/4 estimator precedent (`knowledge.py:460`). The viewer renders the
budget breakdown: "SOUL 2.1k · skills index 34 entries · 3 memories (named via the
injection-log row) · guidelines" — per-section token counts turn the viewer into a
"where is my context going" tool, which no other system offers.

## Security & privacy

- Operator surface only; excluded from `/export` structurally (system messages are
  already skipped — `export_op.py:152-176`) and from the future hosted viewer
  (#2179) when it lands.
- **Chat-delete purges snapshots**: `DELETE /api/chat/sessions/{id}`
  (`chat_routes.py:112`) does NOT purge sibling observability stores today; this
  store adds an explicit purge hook there so prompts never outlive their conversation.
- Restricted consoles: `prompts.capture` is an ordinary settings key, so ADR 0071
  posture applies — `settings.hidden` can lock it, and turning capture off both
  stops writes and flips the routes to `{enabled: false}`.

## Subagents — resolves open question 3

**P1 is main-loop only, and that's the clean cut**: subagents run a thinner
middleware stack (`agent.py:347-351` — no PromptCache/Knowledge), their prompts are
static per type, and they carry no `session_id` (identity = `parent_task_id` +
the `subagent:<type>` span). P3 can add a capture hook to `sub_middleware` keyed on
`parent_task_id` and render them as nested tabs.

## Slices

| Slice | Contents | LOE |
|---|---|---|
| **P1** | capture middleware + task_id threading + store (retention cap) + 2 routes + View-prompt dialog (raw text, per-call tabs, copy) + `/prompt` (last-call note) + session-delete purge | 3 |
| **P2** | `*_parts()` composers + section storage + budget breakdown UI w/ per-section approx tokens + real usage per call in the dialog | 2 |
| **P3** | diff vs previous turn ("2 memories added, skills reordered"); subagent capture; true next-call preview | 3 |

## Test plan

- Middleware: capture fires after PromptCache (blocks split preserved), best-effort
  on store failure, no-op when `prompts.capture` off; async twin.
- Store: hash-dedupe (same stable blob stored once), retention trim on write
  (age + count), orphan blob sweep, migration idempotence.
- Routes: `{enabled:false}` contract, 404 on unknown task, purge-on-session-delete.
- Frontend: vitest for the dialog's call-tab mapping + `/prompt` note path; e2e —
  send a turn in the mock console, open View prompt, assert the dialog shows the
  captured system text (mock backend serves a fixture snapshot).
- Contract pin: a test asserting `PromptCaptureMiddleware` sits directly after
  `PromptCacheMiddleware` in `_build_middleware` — the ordering IS the correctness.

## Refs

`prompt_cache.py:67-87` (final-assembly seam + cache boundary) ·
`knowledge.py:283,396,434-463` (dynamic tail + injection-log precedent) ·
`agent.py:26-197` (middleware order), `:347-351` (subagent stack), `:1038` (static build) ·
`telemetry_store.py` (store scaffolding + task_id PK) · `metrics_store.py:82-110`
(in-write retention) · `injection_routes.py` (read ergonomics) ·
`ChatMessageView.tsx:176-203` + `types.ts:614` (action row + taskId) ·
`coreSlashCommands.ts:58-111` (`/btw` template) · `docviewer/` + `McpCatalogDialog.tsx`
(dialog patterns) · `export_op.py:152-176` (exclusion precedent) · ADR 0069 D6 ·
ADR 0071 · #2179 (future hosted-viewer exclusion)
