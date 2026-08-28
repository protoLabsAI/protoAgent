# Starter tools

The tools `tools/lg_tools.py::get_all_tools()` binds — what an agent can do **before you
install a single plugin**. Most of them are conditional: they appear only when their backing
store or config flag is present, which is why two instances of the same build legitimately
show different tool lists.

- **What ships, and what turns each one on** → [At a glance](#at-a-glance)
- **A tool you expected isn't there** → [Why a tool isn't bound](#missing)
- **The exact shape of one tool** → the [reference sections](#general) below

## Where the agent's tools come from

`get_all_tools()` is one of five sources. The final toolset is assembled in
`graph/agent.py`, in this order:

| Source | Example tools | Bound when |
|---|---|---|
| **Core** — this page | `web_search`, `memory_recall`, `schedule_task` | per-tool gates [below](#at-a-glance) |
| **Plugins** (`register_tools`) | `read_note`, `docs_search`, `delegate_to`, `github_get_pr` | the plugin is enabled — see [Plugin tools](#plugin-tools) |
| **Subagent delegation** | `task`, `task_batch` | subagents are included in the build ([Subagents](/guides/subagents)) |
| **Filesystem fence** | `read_file`, `edit_file`, `run_command` | [`filesystem`](/reference/configuration#filesystem) — **on by default**, fenced to `workspace` |
| **MCP servers** | `<server>__<tool>` | [`mcp.enabled`](/guides/mcp), or a plugin's managed server |

Two things then happen to the **whole** assembled set, not just the core part:

- `tools.disabled` / `tools.hidden` drop named tools — including plugin, MCP, delegation and
  filesystem ones (`tools.disabled: [run_command]` really does remove shell access).
- If [deferred disclosure](#search_tools) is on, everything outside a small base set is hidden
  from the model's per-call schema list until it searches for it. The tools are still bound
  and callable; only the model's *view* is trimmed.

## At a glance {#at-a-glance}

41 tools in twelve groups. Each group's heading says what makes it appear.

### General — always bound

| Tool | What it does |
|---|---|
| [`current_time(timezone="UTC")`](#current_time) | Wall-clock time in an IANA timezone. |
| [`calculator(expression)`](#calculator) | Arithmetic over an AST — never `eval()`. |
| [`web_search(query, max_results=5)`](#web_search) | DuckDuckGo text search. No API key. |
| [`fetch_url(url, max_chars=8000)`](#fetch_url) | Fetch a URL, return cleaned plain text. |

### Asking the operator — always bound, **lead agent only**

Both pause the turn via a LangGraph `interrupt()` (A2A `input-required`) and resume with the
answer. Hard-denied to subagents; auto-answered on autonomous turns so nothing deadlocks.

| Tool | What it does |
|---|---|
| [`ask_human(question)`](#ask_human) | One free-text question. |
| [`request_user_input(title, steps, description="")`](#request_user_input) | A Back/Next form wizard — text, number, boolean, choice cards. |

### Rendering — always bound

| Tool | What it does |
|---|---|
| [`show_component(component, props, title="")`](#show_component) | Render a `table` / `keyvalue` / `timeline` widget inline in chat instead of a markdown blob. |

### Skills & curation — always bound

| Tool | What it does |
|---|---|
| [`load_skill(name)`](#load_skill) | Expand one `<available_skills>` entry into its full procedure. |
| [`list_skills()`](#list_skills) | Every indexed skill — name · source · confidence · description. |
| [`save_skill(name, description, body, tools=None, provenance_reason="", source_session_id="")`](#save_skill) | Create a new skill. Additive-only: refuses to overwrite. |
| [`recent_activity(limit=30, window_hours=168)`](#recent_activity) | Read-only digest of recent turns + a telemetry rollup. |

### Memory & knowledge — bound when a `KnowledgeStore` exists

Built by default; drop the whole group with `middleware.knowledge: false`. See
[`knowledge`](/reference/configuration#knowledge).

| Tool | What it does |
|---|---|
| [`memory_ingest(content, domain="general", heading=None, memory_kind=None, subject=None, delivery_policy=None)`](#memory_ingest) | Store text you already have — optionally typed (what it is, when it enters the prompt). |
| [`knowledge_ingest(source, domain="general", title=None)`](#knowledge_ingest) | Fetch + extract + chunk a URL or file — the only path to a YouTube transcript or a PDF. |
| [`memory_recall(query, k=5, domain=None, memory_kind=None, delivery_policy=None)`](#memory_recall) | Search memory; returns cited matches. |
| [`session_search(query, limit=5, surface="")`](#session_search) | Search prior session transcripts by content and return expandable session ids. |
| [`recall_session(session_id)`](#recall_session) | Expand one `<prior_sessions>` line into that session's full summary. |
| [`memory_list(domain=None, limit=10, memory_kind=None, delivery_policy=None)`](#memory_list) | Most-recent-first listing, with each chunk's `#id` and typed-memory tags. |
| [`memory_stats()`](#memory_stats) | Per-domain chunk counts. |
| [`forget_memory(chunk_id, reason="")`](#forget_memory) | Hard-delete exactly one chunk by id. |

### Scheduling — bound when a scheduler backend exists

Built by default; drop with `middleware.scheduler: false` (or `SCHEDULER_DISABLED=1`). See
[Schedule future work](/guides/scheduler).

| Tool | What it does |
|---|---|
| [`schedule_task(prompt, when, job_id=None, timezone=None)`](#schedule_task) | Persist a future turn — cron expression or ISO datetime. |
| [`list_schedules()`](#list_schedules) | This agent's jobs (never another agent's). |
| [`cancel_schedule(job_id)`](#cancel_schedule) | Cancel one job by id. |
| [`wait(seconds, then)`](#wait) | End the turn now, resume in `seconds` with `then` as the instruction. **Lead agent only.** |

### Tasks — bound when a `TaskStore` exists

Built by default. The agent's in-process planning board, mirrored to the console Tasks panel.

| Tool | What it does |
|---|---|
| [`task_create(title, description="", priority=2, issue_type="task")`](#task_create) | Open an issue. `priority` 0 (highest)–3; type `task\|bug\|feature\|chore\|epic`. |
| [`task_list(include_closed=False)`](#task_list) | List the board. |
| [`task_update(issue_id, status="", title="", description="", priority=-1, issue_type="")`](#task_update) | Change fields; `status` is `open\|in_progress\|blocked\|deferred\|closed`. |
| [`task_close(issue_id, reason="")`](#task_close) | Close as done or won't-do. |

### Inbox — bound when an `InboxStore` exists

Built by default (absent only if the store fails to open).

| Tool | What it does |
|---|---|
| [`check_inbox(priority_floor="next", limit=10)`](#check_inbox) | Pull pending inbound messages posted to `POST /api/inbox` and mark them delivered. |

### Goals — `goal.enabled` **and** at least one plugin verifier

`goal.enabled` defaults to **true**, but the three goal tools also need a registered plugin
verifier — with none, only `list_verifiers` binds. See [Goal mode](/guides/goal-mode) and
[ADR 0028](/adr/0028-plugin-goal-verifiers).

| Tool | What it does |
|---|---|
| [`list_verifiers()`](#list_verifiers) | Every verifier registered here. Bound if goals **or** watches are on. |
| [`set_goal(condition, check, check_args=None, max_iterations=None)`](#set_goal) | Set this session's standing goal, ground-truthed by a plugin verifier. |
| [`update_goal_plan(plan)`](#update_goal_plan) | Carry a running plan across goal iterations. |
| [`abandon_goal(reason)`](#abandon_goal) | Declare the goal unachievable and stop the loop. |

### Watches — `watches.enabled` **and** at least one plugin verifier

An independent axis from goals — a watch is verifier-only and moved by an external process.
Defaults to **true**, same verifier requirement. See [Watches](/guides/watches) and
[ADR 0067](/adr/0067-standalone-watch-primitive).

| Tool | What it does |
|---|---|
| [`create_watch(condition, check, …)`](#create_watch) | Poll a condition on a cadence; optionally run a follow-up prompt when it trips. |
| [`list_watches()`](#list_watches) | Every watch — id · status · condition · verifier. |
| [`update_watch(watch_id, …)`](#update_watch) | Adjust a live watch without losing its observation history. |
| [`clear_watch(watch_id)`](#clear_watch) | Remove one watch. |

### Introspection & onboarding — config-gated

| Tool | What it does | Bound when |
|---|---|---|
| [`show_config(section="")`](#show_config) | Read the agent's own effective, merged config, secrets masked. | a config is available (always, in the server) |
| [`onboard_project(github_repo, name=None, write=None)`](#onboard_project) | Clone a repo into the onboarding root and register it as a managed project. | `onboarding.enabled: true` (default **off**) |

### Opt-in singles

| Tool | What it does | Bound when |
|---|---|---|
| [`edit_soul(section, content, mode="replace", reason="", source_session_id="")`](#edit_soul) | Rewrite one section of the agent's own `SOUL.md`. | legacy lead opt-in, or bounded auto self-improvement |
| [`update_skill(name, description, body, reason, tools=None, source_session_id="")`](#update_skill) | Replace an editable skill and archive its outgoing version. | auto self-improvement on a private/layered store |
| [`delete_skill(name, reason, source_session_id="")`](#delete_skill) | Delete an editable skill and archive its outgoing version. | auto self-improvement on a private/layered store |
| [`set_config(updates)`](#set_config) | Change the agent's own **operational** config — models, routing, plugin settings. Lead agent only. | `tools.self_config_enabled: true` (default **off**) |
| [`search_tools(query="", limit=10)`](#search_tools) | Load deferred tools by capability. | `tools.deferred.enabled: true` (default **off**) |

## Why a tool isn't bound {#missing}

In rough order of how often each one bites:

1. **It's a plugin tool and the plugin is off.** `read_note`, `docs_search`, `delegate_to`,
   `github_*`, `run_workflow`, `show_artifact` are **not** core. Check
   `plugins.enabled` and the console's **Plugins ▸ Installed** panel, whose search matches
   *tool names* — the fastest way to answer "which plugin ships this?".
2. **Its backend isn't there.** No `KnowledgeStore` → no `memory_*`. No scheduler → no
   `schedule_task` *and* no [`wait`](#wait). See the group headings above for the exact flag.
3. **Goals/watches are on but no verifier is registered.** `goal.enabled: true` alone binds
   only `list_verifiers`; `set_goal` and `create_watch` need a plugin-contributed verifier and
   are omitted until one exists. This is the surprising one — the flag is on and the tool
   still isn't there.
4. **It's on the denylist.** `tools.disabled` (off, still toggleable in the console) or
   `tools.hidden` (off *and* not offered in the UI at all). Both sweep the fully assembled
   set. See [`tools`](/reference/configuration#tools).
5. **The subagent's allowlist doesn't name it.** Subagents get an explicit list, not the full
   set — and `ask_human` / `request_user_input` are hard-denied even when a fork lists them
   ([Subagents](/guides/subagents)).
6. **Deferred disclosure is hiding it.** With `tools.deferred.enabled`, the tool is bound but
   its schema is withheld until [`search_tools`](#search_tools) surfaces it.

The console's **Settings ▸ Capabilities ▸ Tools** panel lists what this instance actually
ended up with, and `GET /api/tools` is the same view over HTTP.

## General

### `current_time`

```python
async def current_time(timezone: str = "UTC") -> str
```

Current wall-clock time in the given IANA timezone (e.g. `"UTC"`, `"America/New_York"`,
`"Asia/Tokyo"`).

```
2026-04-17T13:23:42.644606-04:00 (America/New_York)
Human: Friday, April 17 2026, 13:23:42 EDT
```

Unknown timezones return `"Error: unknown timezone 'Not/A_Zone'. …"` — never raises.

### `calculator`

```python
async def calculator(expression: str) -> str
```

Evaluates a numeric expression by walking an AST. **Does not call `eval()`.**

| Supported | Example |
|---|---|
| `+ - * /` | `1 + 2 * 3` |
| `//` floor div | `10 // 3` |
| `%` mod | `10 % 3` |
| `**` power | `2 ** 10` |
| Unary `-` | `-5 + 3` |
| Parens | `(1 + 2) * 3` |

Rejected with an error string: names (`__import__`, any identifier), calls (`abs(-5)`),
attribute access (`(1).__class__`) — anything that isn't pure arithmetic.

Success: `"2 ** 10 = 1024"`. Division by zero: `"Error: division by zero"`.

### `web_search`

```python
async def web_search(query: str, max_results: int = 5) -> str
```

DuckDuckGo text search via the `ddgs` package. No API key. `max_results` is clamped to 1–10.

```
3 result(s) for 'LangGraph tutorial':
1. LangGraph Introduction — https://langchain.com/langgraph
   LangGraph is a framework for building...
```

Network failures, rate limits and import errors come back as `"Error: …"` strings, so the
model can read them and degrade rather than losing the turn.

### `fetch_url`

```python
async def fetch_url(url: str, max_chars: int = 8000) -> str
```

Fetches a URL and returns cleaned plain text.

- Scheme must be `http://` or `https://` — `file://`, `javascript:`, `ftp://` are rejected.
- **SSRF guard is on by default**: with no allowlist configured, a host that resolves to a
  private / loopback / link-local / cloud-metadata / reserved address is refused. Setting
  `egress.allowed_hosts` flips it to deny-by-default — only listed hosts (wildcards allowed)
  pass, and the configured model gateway is auto-included. See
  [`egress`](/reference/configuration#egress).
- Response body capped at 2 MB before parsing; text truncated at `max_chars` with a
  `…[truncated]` marker.
- HTML: scripts, styles, nav, footer and noscript stripped; prefers `<main>` / `<article>`.
- Non-HTML (JSON, text, CSV) is decoded and returned as-is.

```
[200] https://example.com

Example Domain
This domain is for use in documentation examples...
```

User-Agent is `protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)`.

## Asking the operator

### `ask_human`

```python
def ask_human(question: str) -> str
```

Pause the turn, ask the operator one question, continue with their answer (HITL,
[ADR 0003](/adr/0003-reactive-agent-activity-thread)). Issues a LangGraph `interrupt()`, which
checkpoints the graph at the call site; A2A callers see the task move to `input-required`
carrying the question and resume it by sending a follow-up message with the same `taskId`.

**Lead agent only** — hard-denied to subagents (the interrupt is resumed by the lead turn's
runner, and a subagent's graph has no checkpointer to resume one).

On an **autonomous turn** (scheduler / inbox / webhook / background) nobody is watching, so
rather than parking the task forever the runtime auto-answers with a "no operator — proceed"
sentinel (bounded; it force-completes past the budget). Prefer proceeding with a stated
assumption there. Use this for a decision you genuinely must wait on — never for narration.

### `request_user_input`

```python
def request_user_input(title: str, steps: list[dict], description: str = "") -> str
```

Ask for **structured** input via a form dialog and continue with the submitted fields as a
JSON object. Same mechanics and same lead-only/auto-answer rules as `ask_human`.

`steps` is a list of form steps; more than one renders as a sequential **Back/Next wizard**
(step indicator, required fields gate Next/Submit) and the last step submits every step's
answers together. Each step is `{"schema": <JSON Schema draft-07 of that step's fields>,
"title"?: str, "description"?: str}` — at least one step with fields is required (an empty
`steps` is rejected).

Field types, per property in a step's `schema.properties`:

- **text / number / boolean** — `{"type": "string" | "number" | "integer" | "boolean"}`; add
  `"format": "textarea"` for multi-line.
- **single-choice cards** — `{"type": "string", "oneOf": [{"const": "pg", "title": "Postgres",
  "description": "Durable, multi-writer"}, …]}`. Each option is a selectable card with its
  label and description. A bare `"enum": [...]` renders as a plain dropdown instead.
- **multi-choice cards** — wrap the options in an array:
  `{"type": "array", "items": {"oneOf": [...]}}`; the value comes back as a list.

Mark fields required with the step schema's `"required": [...]`. For one free-text or yes/no
question, use `ask_human`.

## Rendering

### `show_component`

```python
def show_component(component: str, props: dict, title: str = "") -> str
```

Render structured data as a typed widget inline in the chat
([ADR 0051](/adr/0051-a2a-realtime-streaming-and-component-rendering)) instead of a markdown
blob. Data-only and safe — no code execution — and the console renders it through an
**extensible component registry** that plugins can add to.

| `component` | `props` |
|---|---|
| `table` | `{"columns": ["A","B"], "rows": [["a1","b1"], …]}` |
| `keyvalue` | `{"items": [{"label": "Credits", "value": "183k"}, …]}` |
| `timeline` | `{"steps": [{"label": "Buy hauler", "state": "done\|active\|todo", "detail": "…"}, …]}` |

`title` is an optional heading. An unknown `component` returns an error naming the valid ones.

Rule of thumb: a data **shape** (table / metrics / steps) → this tool; a generated **visual**
(chart, diagram, bespoke HTML/React/SVG) → an artifact, which renders generated code in a
separate sandboxed panel.

## Skills & curation

`load_skill` is the runtime half of [progressive disclosure](/guides/skills); the curation tools
back the scheduled `/dream` (memory consolidation) and `/distill` (workflow → skill) passes
([ADR 0054](/adr/0054-dream-distill-curation-subagents)). They read `STATE` at call time and
self-gate, which is why they're unconditional here. The update/delete tools are separately
guarded by the unified self-improvement policy.

### `load_skill`

```python
def load_skill(name: str) -> str
```

The `<available_skills>` block in the agent's context lists each skill as a name plus a
one-line summary ([ADR 0060](/adr/0060-skill-progressive-disclosure)); this returns that
skill's complete body. `name` must match a `<skill name="…">` exactly. An unknown name returns
the available names (capped at 40, then a pointer to `list_skills`) rather than an error.

### `list_skills`

```python
def list_skills() -> str
```

Every skill in the index — `name [source · confidence] — description` — so a distill pass
extends instead of duplicating. Read-only.

### `save_skill`

```python
def save_skill(name: str, description: str, body: str, tools: list[str] | None = None,
               provenance_reason: str = "", source_session_id: str = "") -> str
```

Create a new skill. **Additive-only**: it refuses if the name already exists and never
overwrites. Saved with `source=distilled` and curator-managed confidence, so a mistaken
capture decays and self-cleans rather than accumulating. `description` is required — it's how
the skill gets matched. A self-improvement pass can supply `provenance_reason`; the producing
session is recorded automatically.

### `update_skill`

```python
def update_skill(name: str, description: str, body: str, reason: str,
                 tools: list[str] | None = None, source_session_id: str = "") -> str
```

Available only when the self-improvement review and skills facet are both `auto`. Replaces a
user or learned skill, preserving user-facing metadata and existing tool hints when none are
supplied. Bundled/commons skills are read-only, and flat shared stores receive no automatic
skill writers. The outgoing file is copied unchanged under `skills/.history/<slug>/` before
the live artifact changes; a JSON sidecar records the mutation session and reason.

### `delete_skill`

```python
def delete_skill(name: str, reason: str, source_session_id: str = "") -> str
```

Uses the same gate and read-only rules as `update_skill`. The complete outgoing artifact is
archived before deletion, providing the rollback copy named in the tool result.

### `recent_activity`

```python
def recent_activity(limit: int = 30, window_hours: int = 168) -> str
```

Read-only digest of what the agent has actually **done**: the Activity feed (time · origin ·
trigger · text) plus a telemetry rollup (turns, tool calls, LLM calls, cost, success rate, by
model) over the window. `limit` is clamped to 1–200. Returns a "nothing to consolidate" line
when both sources are empty.

## Memory & knowledge

See [Memory & the knowledge store](/explanation/memory-and-knowledge) for the model behind
these, and [Ingestion](/guides/ingestion) for the pipeline `knowledge_ingest` drives.

### `memory_ingest`

```python
async def memory_ingest(
    content: str,
    domain: str = "general",
    heading: str | None = None,
    memory_kind: str | None = None,
    subject: str | None = None,
    delivery_policy: str | None = None,
) -> str
```

Store a chunk of text **you already have** — preferences, environment facts, decisions worth
recalling later. `domain` is a logical bucket (`"preferences"`, `"context"`, `"general"`, …);
`heading` is an optional short label that doubles as a stable de-dupe key.

The typed-memory arguments ([ADR 0108 D4](/adr/0108-context-architecture-v2)) are all
optional: `memory_kind` says what the chunk *is* (`"profile"`, `"standing"`, `"fact"`,
`"decision"`, `"note"`, `"episode"`, `"reference"`), `subject` what it's about, and
`delivery_policy` *when* it enters the prompt — `"always"` (every turn, the same promotion
as `domain="hot"`), `"retrieved"` (on a relevant query; the default when omitted) or
`"on_demand"` (only through `memory_recall`). When `knowledge.hot_write_confirm` is on the
tool refuses always-on writes — `domain="hot"` *or* `delivery_policy="always"` — and tells
the model to ask the operator.

Returns `"Stored chunk 17 in 'preferences'."`, or an error string when the store is
unavailable.

### `knowledge_ingest`

```python
async def knowledge_ingest(source: str, domain: str = "general", title: str | None = None) -> str
```

Runs the **full ingestion pipeline** over a source the agent doesn't have the text of yet: it
pulls the URL or file, extracts text, then chunks and embeds it. Handles web articles, YouTube
transcripts, PDFs and text documents, and — when a config with a gateway is available — audio,
video and image sources via STT/vision.

This is the only path that gets a transcript or decodes a file; `web_search` + `fetch_url`
won't. When a background manager is present ([ADR 0050](/adr/0050-background-subagents-reactive-notifications))
a slow source is detached as a background job instead of blocking the turn. Filing a source
under `domain="hot"` is an always-on write, so with `knowledge.hot_write_confirm` on the tool
refuses it the same way `memory_ingest` does — before anything is fetched.

### `memory_recall`

```python
async def memory_recall(
    query: str,
    k: int = 5,
    domain: str | None = None,
    memory_kind: str | None = None,
    delivery_policy: str | None = None,
) -> str
```

Top-k search over the store (FTS5, LIKE fallback), one match per line, each citing its
provenance — domain, stored date, namespace:

```
[preferences] coffee: Operator's preferred coffee is a Gibraltar with oat milk.
[context] lab: Primary lab is Snickerdoodle in Spokane.
```

`domain` scopes the search to one bucket — use it to separate the agent's own record from
inherited or imported knowledge (a domain like `claude-import` is another codebase's history,
not this agent's actions). `memory_kind` and `delivery_policy` narrow it to one typed-memory
classification ([ADR 0108 D4](/adr/0108-context-architecture-v2)); `delivery_policy="on_demand"`
is the only way an on-demand memory surfaces. Returns `"No matches."` when nothing clears the
threshold.

### `session_search`

```python
async def session_search(query: str, limit: int = 5, surface: str = "") -> str
```

Full-text search over persisted prior-session summaries when the relevant session id is
unknown. The disposable FTS5 index is synchronized lazily, so existing histories become
searchable without migration and session persistence never depends on index health. Results
are relevance-ranked, credential-redacted, capped at 20, exclude the active session, and
carry an excerpt plus id for expansion with `recall_session`.

`surface` optionally limits results to `chat`, `a2a/other`, `activity`, `palette`, or
`background`. Query text is converted to literal terms; raw FTS operators are never executed.

### `recall_session`

```python
async def recall_session(session_id: str) -> str
```

Expands one entry of the auto-injected `<prior_sessions>` digest into that session's full
persisted summary — messages and final output, reasoning-stripped, capped at ~6 000 chars
([ADR 0069](/adr/0069-memory-delivery-layer)). The digest itself carries only one attributed
line per prior session (id · timestamp · surface · topic · message count), so this is the
on-demand path to the content. Errors cleanly on an unknown or malformed id.

### `memory_list`

```python
async def memory_list(
    domain: str | None = None, limit: int = 10, memory_kind: str | None = None, delivery_policy: str | None = None
) -> str
```

Most-recent-first listing, filtered by domain, `memory_kind` and/or `delivery_policy` when
given. Each row carries the `#<id>` that `forget_memory` takes, plus `kind=` / `policy=` /
`review=` tags when the chunk is typed. Useful for "what did I log today?".

### `memory_stats`

```python
async def memory_stats() -> str
```

Per-domain chunk counts plus a total — the sanity check that an ingest actually landed.

### `forget_memory`

```python
async def forget_memory(chunk_id: int, reason: str = "") -> str
```

**Hard-deletes exactly one chunk** by the id `memory_list` shows. The forgetting half of a
`/dream` pass: use it on a fact that is stale, superseded or duplicated, ideally after
ingesting the corrected version first.

This is a real delete, not a supersede — automatic fact consolidation marks replaced rows
`invalidated_at` and keeps them for audit, but an explicit forget is operator intent and
removes the row. No bulk or wildcard form exists, by design. `reason` is recorded for the
audit trail.

## Scheduling

### `schedule_task`

```python
async def schedule_task(prompt: str, when: str, job_id: str | None = None, timezone: str | None = None) -> str
```

Persist a future invocation; the agent receives `prompt` as a fresh turn when it fires.

`when` is either a 5-field cron expression (`"0 9 * * 1-5"` = weekdays at 9am) or an ISO-8601
datetime (`"2026-05-01T15:00:00"` = once). Backends auto-detect which. `timezone` is an
optional IANA zone for interpreting `when`. `job_id` defaults to `<agent_name>-<uuid>` — you
need it later for `cancel_schedule`.

Returns `"Scheduled job <id> next at <iso>."`, or `"Error: …"` on a malformed `when`.

Prompts must be **self-contained**: the agent has no memory of the scheduling moment when the
task fires, so write a fresh turn ("review last week's pipeline incidents and post a summary"),
not a back-reference ("do that thing we discussed").

### `list_schedules`

```python
async def list_schedules() -> str
```

This agent's scheduled jobs — one per line with id, next fire, schedule and prompt preview.
Multi-agent isolation is real: each agent only sees jobs it created. `"No scheduled jobs."`
when empty.

### `cancel_schedule`

```python
async def cancel_schedule(job_id: str) -> str
```

Cancel by id. Returns `"Canceled <id>."` or `"Error: no such job <id>."` Cross-agent
cancellation is blocked — `gina-personal` cannot cancel `gina-work`'s jobs even when they
share a sqlite path.

### `wait`

```python
async def wait(seconds: int, then: str) -> str
```

Yield the turn and resume **later** instead of busy-polling a status tool
([ADR 0053](/adr/0053-wait-yield-and-resume)). Calling `wait` **ends the current turn** (via
`WaitYieldMiddleware`) and schedules a one-shot resume `seconds` from now; when it fires the
agent is re-invoked with `then` as its instruction, in the **same conversation thread**
(history intact). This is how you run long-horizon "do X, wait, do Y" work without burning the
recursion budget on a poll loop.

`then` is required and **self-contained** — it's the agent's only context on resume, so it
must name the work and the entities ("Dock NOVAHAUL-5 at X1-UC87-K93, sell the ore, accept the
next contract"). `seconds` is clamped to ≥ 1; pass the ETA a status tool gave you and wait the
**full** duration in one call, since under-waiting just wakes early to wait again.

**One pending wait per thread**
([#1702](https://github.com/protoLabsAI/protoAgent/issues/1702)): a new `wait` **supersedes**
any still-pending wait for the same session (a stable `wait:<session>` job id →
cancel-then-add), so repeated waits can't stack into a pile of wake-ups that all fire into the
thread. A `schedule_task` job uses its own id and is untouched. Every scheduling is logged —
`[wait] thread=… in Ns (superseded a pending wait) → resume: …` — so a stacking loop is
visible.

**Lead agent only** (subagents are bounded by `max_turns` and no allowlist names it). The
yield is durable across restart. For an absolute time or a recurring cadence use
`schedule_task` — `wait` is for "yield for a bit, then pick this back up".

## Tasks

The agent's own planning board — an in-process SQLite issue tracker, mirrored to the console
Tasks panel. Tasks are attributed to the session that created them.

### `task_create`

```python
def task_create(title: str, description: str = "", priority: int = 2, issue_type: str = "task") -> str
```

Open an issue and return its id. `priority` is 0 (highest) to 3 (low); `issue_type` is one of
`task` / `bug` / `feature` / `chore` / `epic`. An invalid value returns `"Error: …"` rather
than raising.

### `task_list`

```python
def task_list(include_closed: bool = False) -> str
```

One line per issue — `[status] id (pN, type) title`. Open issues only unless
`include_closed=True`. `"No issues on the board."` when empty.

### `task_update`

```python
def task_update(issue_id: str, status: str = "", title: str = "", description: str = "",
                priority: int = -1, issue_type: str = "") -> str
```

Change any subset of fields. `status` is `open` / `in_progress` / `blocked` / `deferred` /
`closed`. Leave a string field empty — or `priority` at `-1` — to keep it unchanged.

### `task_close`

```python
def task_close(issue_id: str, reason: str = "") -> str
```

Close an issue as done or won't-do, with an optional `reason`.

## Inbox

### `check_inbox`

```python
async def check_inbox(priority_floor: str = "next", limit: int = 10) -> str
```

Pull pending **inbound messages** ([ADR 0003](/adr/0003-reactive-agent-activity-thread)) —
webhooks, external systems and sister agents that posted to `POST /api/inbox` — and mark them
delivered.

`priority_floor` selects the tiers: `now` (now only), `next` (now + next, default) or `later`
(everything pending). `now`-priority items have already fired an Activity turn on arrival;
`next` and `later` wait for this call, so the agent decides when to surface them. Returns the
items one per line, or `"Inbox empty."`.

## Goals & watches

Both surfaces are **plugin-verifier only**. The tools hardcode `type="plugin"`, so an agent
cannot open a shell, test or data goal on itself — those stay operator-only via `/goal` and
the operator API. A verifier name looks like `<plugin-id>:<name>`.

### `list_verifiers`

```python
async def list_verifiers() -> str
```

Every verifier registered on this instance: the core types (`command`, `test`, `ci`, `data`,
`llm`, `plugin`) for awareness, then the plugin-contributed checks with their
`<plugin-id>:<name>` identifier and description. **Only the plugin checks are usable by
`set_goal` / `create_watch`.** When none are registered it says so explicitly — which is the
signal that goal mode is on but has nothing it can verify.

### `set_goal`

```python
def set_goal(condition: str, check: str, check_args: dict | None = None,
             max_iterations: int | None = None) -> str
```

Set a standing goal for **this session**. The agent is re-invoked toward `condition` until the
plugin verifier named by `check` passes; `check_args` is declarative data the verifier reads
(e.g. `{"min": 1000000}`).

An unknown `check` is rejected up front, listing the registered verifiers — without that guard
the goal would be created but could never pass, spinning to the iteration cap and finishing
`unachievable`. Also errors when goal mode is off or there's no active session.

### `update_goal_plan`

```python
def update_goal_plan(plan: str) -> str
```

Record or refresh the running plan for this session's active goal (what's done, what's next,
what failed). It's persisted and fed back into the next continuation prompt, which is how a
coherent plan survives across iterations. A harmless no-op when goal mode is off or no goal is
active.

### `abandon_goal`

```python
def abandon_goal(reason: str) -> str
```

Flag the active goal unachievable and stop the loop. The goal finishes `unachievable` after
the turn — **unless the verifier finds it already met, which wins**. No-op when no goal is
active.

### `create_watch`

```python
def create_watch(condition: str, check: str, check_args: dict | None = None, run_prompt: str = "",
                 watch_id: str | None = None, interval_s: float | None = None,
                 expires_in_s: float | None = None, stall_after: int | None = None,
                 repeat: bool = False, on_change: bool = False) -> str
```

Poll `condition` on a cadence, ground-truthed by the plugin verifier `check`; when it's met,
run `run_prompt` (if given) as a follow-up turn in this session. Many watches run in parallel —
that's the point: a deploy, a CI run and a metric are three watches, not one goal.

By default a watch is a **tripwire**: it fires once and is done. Two flags make it a standing
monitor:

- `repeat` — keep watching after it fires, firing each time the condition *becomes* true again
  (so a latching condition like `credits >= 1M` doesn't spam).
- `on_change` — fire whenever the checked **value** moves, whatever the condition says. Use it
  to track something rather than wait for it. Implies `repeat`.

Three knobs shape lifetime and cost; a watch with none of them polls at the default cadence
until something clears it:

| Knob | Effect |
|---|---|
| `interval_s` | Seconds between checks for this watch — a floor, never faster than the global cadence. |
| `expires_in_s` | Give up this many seconds **from now**; the watch finishes `expired`. Relative on purpose — a model asked for an absolute timestamp guesses, and a guess in the past expires the watch on its first tick. Must be positive. |
| `stall_after` | After N consecutive checks with unchanged evidence, fire the stall signal (the watch stays active) — how you notice a deploy that's wedged rather than slow. |

`watch_id` defaults to a slug of the condition; pass one to hold two watches on the same
condition. Set `expires_in_s` on any repeating watch unless you really mean forever.

### `list_watches`

```python
def list_watches() -> str
```

Every watch for this agent — id · status · condition · verifier — or a note when there are
none.

### `update_watch`

```python
async def update_watch(watch_id: str, condition: str | None = None, run_prompt: str | None = None,
                       interval_s: float | None = None, expires_in_s: float | None = None,
                       stall_after: int | None = None, clear_deadline: bool = False,
                       repeat: bool | None = None, on_change: bool | None = None) -> str
```

Adjust a live watch, passing only what changes. **Use this instead of clear-and-recreate** —
recreating resets the stall history and starts the evidence over.

`expires_in_s` is measured from now, as in `create_watch`. Because `None` already means "not
supplied", removing an expiry entirely needs its own flag: `clear_deadline=true` (passing both
is an error). A finished watch can't be edited — set a new one.

### `clear_watch`

```python
def clear_watch(watch_id: str) -> str
```

Remove a watch by the id `list_watches` shows. Reports whether it existed.

## Introspection & onboarding

### `show_config`

```python
def show_config(section: str = "") -> str
```

Returns the agent's own **effective, merged configuration** — the same view `GET /api/config`
serves — as JSON. Pass a top-level `section` (`"model"`, `"mcp"`, `"filesystem"`, or any
plugin section such as `"project_board"`) to keep the output small; omit it for the whole
document, which falls back to a section index when it's too large to render at once. An
unknown section returns the available names plus a near-match suggestion.

**Read-only.** It never writes, and it binds whenever a config is available. Drop it with
`tools.disabled: [show_config]` like any other core tool.

**Why it exists.** An agent's own `config/langgraph-config.yaml` lives outside every filesystem
fence, so the file that says how the agent is wired was the one file it couldn't open — making
a misconfiguration indistinguishable from a bug. In the incident that prompted it
([#2540](https://github.com/protoLabsAI/protoagent/issues/2540)), an agent spent two sessions
diagnosing a board bound to the wrong repo — checking paths on disk and on GitHub, reading the
plugin's *defaults* from source — before its operator found the answer in one grep. Reading a
plugin's source tells you what it does unconfigured; this tells you what your instance set.

**Secrets are masked, not inherited as safe.** `config_to_dict` blanks secret-typed schema
fields and plugin-declared secrets, which is the right bar for the token-gated operator API —
but this output lands in the model's context and the chat transcript, so the tool applies its
own pass on top. `mcp.servers[].env` and `[].headers` are free-form string maps that routinely
hold tokens; every value in them is masked, along with any key that names a credential, at any
depth. Masked values read as `«redacted»`, so the agent still learns that a credential *is*
set — a blank stays blank, because masking one would claim a token is present when none is.

### `onboard_project`

```python
async def onboard_project(github_repo: str, name: str | None = None, write: bool | None = None) -> str
```

Clone a GitHub repo into the configured onboarding root and register it in the managed
[`projects`](/adr/0095-managed-projects-registry) registry, so the filesystem tools can reach
it. `github_repo` accepts `owner/repo`, `github.com/owner/repo` or the full URL; `name`
defaults to the repo name; `write` overrides the operator's default access mode.

**Off unless `onboarding.enabled: true`** — and off means *absent*, not a tool that exists only
to refuse. An operator who hasn't opted in gets no onboarding surface at all.

## Self-configuration

### `set_config`

```python
async def set_config(updates: dict) -> str
```

**Guarded, off by default.** Bound to the **lead agent only** when an operator sets
`tools.self_config_enabled: true`; bounded subagents never receive it. `updates` is a flat dict
of dotted keys → values (`{"project_board.coder": "proto"}`), applied through the same
`ops.config.set_config` write path as `protoagent config set` and the console's
`PATCH /api/config` ([ADR 0075](/adr/0075-external-interfaces-cli-mcp-api) D2 — one op, thin
adapters). The change persists and reloads the agent, and the operator is notified on the bus.

**Scope is operational settings only.** The tool refuses the whole write — never partially — when
any key falls in the trust surface, because those settings decide what the agent is *allowed* to
do rather than how it behaves:

| Refused | Why |
|---|---|
| `filesystem` | `allow_run` (shell) and the [ADR 0007](/adr/0007-directory-aware-operator-agent) project fence |
| `operator` | `allowed_dirs` / `project_dir` — the operator fence |
| `egress` | the [ADR 0008](/adr/0008-sandboxing-and-openshell) network fence |
| `plugins` | enabling a plugin runs its code in-process **as the agent** ([ADR 0071](/adr/0071-plugin-permissions-trust-model)) |
| `delegates` · `runtime` · `acp` | each entry names an **executable** the host spawns (`command`/`args`, often `permissions: auto`) |
| `auth` · `security` · `mcp` | operator credentials and out-of-process capability |
| `soul` | persona has its own guarded path (`edit_soul`) |
| `tools` | including `self_config_enabled` itself — no widening its own fence |

A key whose leaf name is `command`, `args`, `argv`, `executable`, `binary`, `cmd`, `entrypoint`,
or `interpreter` is refused in **any** section, including plugin ones — those can't be enumerated
in advance, and any plugin may grow a key naming a binary. The line is that an agent may *choose*
among the executables its operator provisioned (`project_board.coder: proto`) but never *define*
one.

Any **secret**-typed key is refused outright and never echoed back: the write path would
faithfully route it into `secrets.yaml`, which is right for the CLI and the console and wrong
here, since it would put a live credential in the turn transcript.

So an agent can retune how it runs without being able to change what it may do. Batching a denied
key with legitimate ones refuses everything, so nothing can be smuggled through alongside a
valid write.

## Persona

### `edit_soul`

```python
async def edit_soul(section: str, content: str, mode: str = "replace",
                    reason: str = "", source_session_id: str = "") -> str
```

**Guarded, off by default** ([ADR 0081](/adr/0081-self-authored-persona-edit-soul)). Bound to
the **lead agent** when an operator sets `soul.self_edit_enabled: true`. The one exception is
the built-in, policy-bounded `self-improve` reviewer: it receives the tool only when the master,
post-goal review, and persona modes are all `auto`. Other bounded subagents never receive it.
It lets the agent durably refine its own persona by rewriting one markdown
**section** of `SOUL.md` — `section` matches case-insensitively (a missing one is created),
`mode` is `replace` or `append`.

**Scope is persona only** — identity, voice, values, temperament — never operating doctrine
([ADR 0079](/adr/0079-autonomous-operating-model)). Guardrails: one section per call (it can't
blow away the file), a 64 KB cap on the whole persona (it rides in the system-prompt prefix
every turn), and empty / no-op / invalid-mode edits are refused with an error string.

Every edit goes through `write_soul`, so the outgoing persona is **snapshotted to soul-history**
and restorable from Settings ▸ Identity. The change takes effect on the agent's **next turn**:
the server injects its graph-reload as a callback so the compiled graph rebinds atomically —
the current turn is unaffected — without `tools/` importing `server/`. Where no callback is
wired (subagent, eval, script), the save still lands and applies on the next natural reload.

**Never silent.** Every accepted edit publishes a `persona.self_edited` event (section, mode,
new revision, producing session, and optional `reason`) on the event bus, so the operator sees an identity change in the console even
when it happens on an autonomous turn — and it leaves a trail if a prompt injection ever drove
one. That transparency guardrail came out of ADR 0081's due diligence against prior art: Hermes
keeps `SOUL.md` operator-only; OpenClaw invites unguarded self-edit and treats the soul as a
prompt-injection surface; Letta added a read-only persona guard after unconstrained self-edits
degraded identity.

## Progressive disclosure

### `search_tools`

```python
def search_tools(query: str = "", limit: int = 10) -> str
```

Added only when `tools.deferred.enabled` is on
([ADR 0005](/adr/0005-tool-pollution-and-progressive-disclosure)). With deferral on, the model
sees a small always-on base set plus this meta-tool each turn; everything else stays bound and
callable but its schema is withheld until the agent searches for it. `ToolDeferralMiddleware`
reads the backticked names out of the result and binds those tools on subsequent turns.

Keyword-matches deferred tools by name and description, returning `- \`name\` — purpose` lines.
An empty `query` lists everything; `limit` is clamped to 1–50; a query that matches nothing
falls back to the full list rather than a dead end. Override the always-on base with
`tools.deferred.keep` — `search_tools` itself is always kept, since without it nothing could be
loaded back.

## Plugin tools {#plugin-tools}

Everything else the agent can call comes from a [plugin](/guides/plugins) (`register_tools`),
not this registry. First-party plugins that ship **in-tree**:

| Plugin | Tools | Default |
|---|---|---|
| `notes` | `read_note` / `write_note` / `append_note` over one shared agent-global markdown notebook, plus the Notes console panel | **on** |
| `docs` | `docs_search` / `docs_read` over protoAgent's own documentation | **on** |
| `artifact` | `show_artifact` — generated HTML/React/SVG in a sandboxed panel | **on** |
| `craft` | *(no tools — ships engineering slash-command skills)* | **on** |
| `delegates` | `delegate_to(target, query)` — route a sub-task to another agent or endpoint over **a2a / openai / acp**, managed and hot-swappable from the console ([ADR 0025](/adr/0025-unified-delegate-registry-and-panel)). An `acp` delegate drives a CLI coding agent over ACP ([ADR 0024](/adr/0024-spawn-cli-coding-agents-acp)). Replaced the retired `peer_consult` / `peer_list` / `code_with`. See [Delegates](/guides/delegates) + [CLI coding agents](/guides/coding-agents) | built-in |
| `workflows` | `run_workflow` + Workflow Studio | off — `plugins.enabled: [workflows]` |
| `execute_code`, `coder`, `orgchart`, `telegram`, `friction`, `hello` | — | off |

Others — including the **GitHub** plugin (`github_get_pr`, `github_get_issue`,
`github_list_issues`, `github_get_commit_diff` and the Review API tools) — live in their own
repos and are installed from a git URL:

```bash
python -m server plugin install <git-url>
```

Installed plugins are pinned in `plugins.lock` and manageable from the console's
**Plugins ▸ Installed** panel. See [Plugins](/guides/plugins) and the
[plugin registry](/guides/plugin-registry).

## Adding your own

**Ship a plugin.** `register_tools` is the supported path: it survives upstream re-sync,
hot-reloads, and can carry its own config, secrets, Settings and console views. Editing
`get_all_tools()` still works, but it is a core edit that conflicts on every upstream merge.

The tool itself is the same either way:

```python
from langchain_core.tools import tool

@tool
async def my_tool(required_arg: str, optional_arg: int = 5) -> str:
    """First line becomes the LLM's summary of the tool.

    Args:
        required_arg: What this argument is. The LLM reads these docstrings.
        optional_arg: Optional, with a sensible default.
    """
    try:
        result = await do_the_thing(required_arg, optional_arg)
    except Exception as e:
        return f"Error: {e}"
    return f"Success: {result}"
```

Two conventions every tool here follows, and yours should too:

- **Never raise into the turn.** Return `"Error: …"` and let the model read it, retry with
  different arguments, or degrade. A raised exception costs the turn.
- **Don't hand-roll `subprocess`.** Build on `tools/shell.py::run_command` (async; handles
  timeout/kill, missing-binary → structured error, env merge, stdin/cwd) or `tools/gh_cli.py`
  for `gh` specifically.

If you're inside a tool body and need the current session, read it from injected graph state —
`current_session_id()` is empty there (the tool runs in a different execution context than the
middleware).

See [Write your first tool](/tutorials/first-tool) for the walkthrough, and
[Build a plugin](/guides/plugins) for the packaging.

## Related

- [Configure subagents](/guides/subagents) — tools are allowlisted per subagent
- [Configuration](/reference/configuration#tools) — `tools.disabled`, `tools.hidden`, `tools.deferred`
- [Environment variables](/reference/environment-variables) — SSRF allowlist vars affect `fetch_url`; scheduler backend selection lives there too
- [Eval your fork](/guides/evals) — the eval harness exercises these end-to-end
- [Schedule future work](/guides/scheduler) — the firing model and multi-agent isolation behind the scheduler tools
