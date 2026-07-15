# Starter tools

The default tool set (from `tools/lg_tools.py::get_all_tools`):

- Four keyless general-purpose tools — `current_time`, `calculator`, `web_search`, `fetch_url` — that work without any state.
- Two **HITL tools** — `ask_human` (a free-text question) and `request_user_input` (a multi-step Back/Next form **wizard** with text/number/choice fields, incl. AskUserQuestion-style option cards) — pause the turn (A2A `input-required`) for the operator and resume with their answer. Lead-agent only (hard-denied to subagents); on autonomous turns they don't block — the runtime auto-answers so the turn can't deadlock.
- The **render tool** `show_component(component, props, title?)` — render structured data inline in chat as a `table`, `keyvalue`, or `timeline` widget instead of a markdown blob ([ADR 0051](/adr/0051-a2a-realtime-streaming-and-component-rendering)). Data-only and safe (no code execution); the console renders it via an **extensible component registry** plugins can add to. For free-form generated HTML/React, use an artifact instead.
- Five **memory tools** — `memory_ingest`, `memory_recall`, `recall_session`, `memory_list`, `memory_stats` — bound to the bundled `KnowledgeStore` (sqlite + FTS5, see [Configuration](/reference/configuration#knowledge)). Omitted when no store.
- Three **scheduler tools** — `schedule_task`, `list_schedules`, `cancel_schedule` — bound to the bundled scheduler backend (local sqlite, see [Schedule future work](/guides/scheduler)). Omitted when no scheduler.
- Four **tasks tools** — `task_create`, `task_list`, `task_update`, `task_close` — the agent's in-process planning board, bridged to the console Tasks panel. Bound when a tasks store is present (default in `server/agent_init.py`).
- One **inbox tool** — `check_inbox` — bound to the durable inbound inbox (ADR 0003) when configured; pulls stimuli pushed to `POST /api/inbox`.
- **Goal + watch tools** — `set_goal` / `update_goal_plan` / `abandon_goal` drive a standing, plugin-verified goal; `create_watch` / `list_watches` / `clear_watch` supervise many external conditions at once ([ADR 0028](/adr/0028-plugin-goal-verifiers) / [ADR 0067](/adr/0067-standalone-watch-primitive); both plugin-verified, always on). See [Goal mode](/guides/goal-mode) + [Watches](/guides/watches).
- Three **curation tools** — `recent_activity`, `list_skills`, `save_skill` — let the agent review what it's recently done and manage its own skill library; they also back the scheduled `/dream` (memory consolidation) and `/distill` (workflow→skill) passes ([ADR 0054](/adr/0054-dream-distill-curation-subagents)).
- The **`search_tools`** meta-tool — added only when deferred-tool disclosure is on ([ADR 0005](/adr/0005-tool-pollution-and-progressive-disclosure)); the agent calls it to load tools that aren't in the per-call base set.
- One **persona tool** — `edit_soul` — lets the lead agent rewrite a section of its own `SOUL.md` ([ADR 0081](/adr/0081-self-authored-persona-edit-soul)). **Off by default, guarded**: bound only when an operator sets `soul.self_edit_enabled: true`, and never on a subagent. See [`edit_soul`](#edit_soul) below.

`get_all_tools(knowledge_store=None, scheduler=None, inbox_store=None, tasks_store=None, goal_enabled=False, soul_edit_enabled=False, reload_callback=None)` is the registry; the conditional groups above are included only when their backend is passed (all are constructed by default in `server/agent_init.py`; opt out via `middleware.knowledge: false` / `middleware.scheduler: false`). The render + curation tools are unconditional; the goal + watch tools ride `goal_enabled`, and `edit_soul` rides `soul_edit_enabled`. To **drop** a core tool without editing this function, list it in `tools.disabled`.

## Plugin-provided tools (not in `get_all_tools`)

Everything else the agent can call comes from a [plugin](/guides/plugins) (`register_tools`), not the core registry — to **add** tools, ship a plugin (editing `get_all_tools` is the legacy core-edit path that conflicts on re-sync). First-party plugins ship some out of the box:

- **`notes`** (on by default) — `read_note` / `write_note` / `append_note` over one shared, agent-global markdown notebook (ADR 0034); also the Notes console panel.
- **`docs`** (on by default) — `docs_search` / `docs_read` over protoAgent's own documentation.
- **`delegates`** (built-in) — `delegate_to(target, query)` routes a sub-task to another agent/endpoint over **a2a / openai / acp**, managed + hot-swappable from the console ([ADR 0025](/adr/0025-unified-delegate-registry-and-panel)). An **`acp`** delegate drives a CLI coding agent (proto/Claude/Codex/Gemini) over ACP ([ADR 0024](/adr/0024-spawn-cli-coding-agents-acp)). This single tool **replaced** the retired `peer_consult` / `peer_list` (env-var A2A federation) and `code_with`. See [Delegates](/guides/delegates) + [CLI coding agents over ACP](/guides/coding-agents).
- **`github`** (opt-in — `plugins.enabled: [github]`) — read tools (`github_get_pr`, `github_get_issue`, `github_list_issues`, `github_get_commit_diff`) over the `gh` CLI (auth via `GITHUB_TOKEN`/`GH_TOKEN`, else gh's ambient login); each needs an explicit `repo`.

## `current_time`

```python
@tool
async def current_time(timezone: str = "UTC") -> str
```

Returns the current wall-clock time in the given IANA timezone (e.g. `"UTC"`, `"America/New_York"`, `"Asia/Tokyo"`). Defaults to UTC.

Output:

```
2026-04-17T13:23:42.644606-04:00 (America/New_York)
Human: Friday, April 17 2026, 13:23:42 EDT
```

Unknown timezones return `"Error: unknown timezone 'Not/A_Zone'. ..."` — never raises.

## `calculator`

```python
@tool
async def calculator(expression: str) -> str
```

Safely evaluates a numeric expression using AST parsing. **Does not call `eval()`**.

Supported:

| Op | Example |
|---|---|
| `+ - * /` | `1 + 2 * 3` |
| `//` floor div | `10 // 3` |
| `%` mod | `10 % 3` |
| `**` power | `2 ** 10` |
| Unary `-` | `-5 + 3` |
| Parens | `(1 + 2) * 3` |

Rejected (returns error string):

- Names (`__import__`, any identifier)
- Function calls (`abs(-5)`)
- Attribute access (`(1).__class__`)
- Anything that's not pure arithmetic

Output on success: `"2 ** 10 = 1024"`. Division by zero returns `"Error: division by zero"`.

## `web_search`

```python
@tool
async def web_search(query: str, max_results: int = 5) -> str
```

DuckDuckGo text search via the `ddgs` package. No API key. `max_results` is clamped to 1–10.

Output:

```
3 result(s) for 'LangGraph tutorial':
1. LangGraph Introduction — https://langchain.com/langgraph
   LangGraph is a framework for building...
2. ...
```

Failures (network, rate-limit, import error) return `"Error: ..."` strings. The LLM reads the error and retries or degrades gracefully.

## `fetch_url`

```python
@tool
async def fetch_url(url: str, max_chars: int = 8000) -> str
```

Fetches a URL and returns cleaned plain-text content.

Guarantees:

- URL scheme must be `http://` or `https://`. `file://`, `javascript:`, `ftp://`, etc. are rejected.
- Response body is capped at 2MB before parsing (blast-radius cap).
- Text output is truncated at `max_chars` with `…[truncated]` marker.
- HTML pages: scripts, styles, nav, footer, noscript are stripped. Prefers `<main>` / `<article>` over the full body.
- Non-HTML content (JSON, plain text, CSV) is decoded and returned as-is.

User-Agent is `protoAgent/0.1 (+https://github.com/protoLabsAI/protoAgent)`. Customize in the tool body if your fork hits rate-limited APIs that need something specific.

Output:

```
[200] https://example.com

Example Domain
This domain is for use in documentation examples...
```

## `memory_ingest`

```python
@tool
async def memory_ingest(content: str, domain: str = "general", heading: str | None = None) -> str
```

Stores a chunk in the bundled `KnowledgeStore`. Use for things the operator wants you to remember across sessions — preferences, environment facts, decisions worth recalling later.

`domain` is a logical bucket (`"preferences"`, `"context"`, `"general"`, …). `heading` is an optional short label that doubles as a stable de-dupe key.

Returns `"Stored chunk 17 in 'preferences'."` on success, an error string when the store is unavailable.

## `memory_recall`

```python
@tool
async def memory_recall(query: str, k: int = 5) -> str
```

Top-k keyword search over the store via FTS5 (LIKE fallback). Returns one match per line:

```
[preferences] coffee: Operator's preferred coffee is a Gibraltar with oat milk.
[context] lab: Primary lab is Snickerdoodle in Spokane.
```

Returns `"No matches."` when nothing scores above the keyword threshold.

## `recall_session`

```python
@tool
async def recall_session(session_id: str) -> str
```

Expands one entry from the auto-injected `<prior_sessions>` digest into the full persisted session summary — messages and final output, reasoning-stripped, capped at ~6 000 chars ([ADR 0069](/adr/0069-memory-delivery-layer)). The digest itself carries only one attributed line per prior session (id · timestamp · surface · topic · message count), so this tool is the on-demand path to a prior session's content. Errors cleanly on an unknown or malformed id.

## `memory_list`

```python
@tool
async def memory_list(domain: str | None = None, limit: int = 10) -> str
```

Most-recent-first listing of stored chunks. Filter by domain when given. Useful for "what did I log today?" style queries.

## `memory_stats`

```python
@tool
async def memory_stats() -> str
```

Per-domain chunk counts plus a total. Useful for sanity-checking that ingest landed.

## `daily_log`

```python
@tool
async def daily_log(content: str) -> str
```

Convenience wrapper around `memory_ingest` that writes to `domain='daily-log'` with today's UTC date as the heading. Same-day entries cluster under the same heading for `memory_list(domain='daily-log')`.

## `schedule_task`

```python
@tool
async def schedule_task(prompt: str, when: str, job_id: str | None = None) -> str
```

Persist a future invocation. The agent receives `prompt` as a fresh turn when the schedule fires.

`when` is either a 5-field cron expression (`"0 9 * * 1-5"` = every weekday at 9am) or an ISO-8601 datetime (`"2026-05-01T15:00:00"` = once at 3pm UTC on May 1). Backends auto-detect.

`job_id` is optional — auto-generated as `<agent_name>-<uuid>` when omitted. You'll need it later for `cancel_schedule`.

Output: `"Scheduled job <id> next at <iso>."` on success. Returns `"Error: ..."` on malformed `when` or backend failure.

Prompts are self-contained — the agent has no memory of the scheduling moment when the task fires, so write the prompt as a fresh turn ("review last week's pipeline incidents and post a summary"), not a reference ("do that thing we discussed").

## `list_schedules`

```python
@tool
async def list_schedules() -> str
```

List the current scheduled jobs for *this* agent. Multi-agent isolation: each agent only sees jobs it created.

Output: one job per line with id, next-fire timestamp, schedule, and prompt preview. Returns `"No scheduled jobs."` when empty.

## `cancel_schedule`

```python
@tool
async def cancel_schedule(job_id: str) -> str
```

Cancel a scheduled job by id. Returns `"Canceled <id>."` or `"Error: no such job <id>."`.

Cross-agent cancellation is blocked — `gina-personal` cannot cancel `gina-work`'s jobs even when sharing a sqlite path.

## `wait`

```python
@tool
async def wait(seconds: int, then: str) -> str
```

Yield the turn and resume LATER instead of busy-polling a status tool to wait
something out ([ADR 0053](/adr/0053-wait-yield-and-resume)). Calling `wait` **ends
the current turn** (via `WaitYieldMiddleware`) and schedules a one-shot resume
`seconds` from now; when it fires the agent is re-invoked with `then` as its
instruction, in the **same conversation thread** (history intact) — so it acts
exactly once, when the thing is actually ready. This is the right way to run
long-horizon "do X, wait, do Y" work without burning the recursion budget on a
poll loop.

`then` is required and **self-contained** — it's the agent's only context on
resume, so it must name what to do and which entities are involved ("Dock
NOVAHAUL-5 at X1-UC87-K93, sell the ore, accept the next contract"), not a
back-reference. `seconds` is clamped to ≥ 1; pass the ETA a status tool gave you
(e.g. `st_navigate` returns `arriving in Ns` and the exact `wait(N, …)` call) and
wait the **full** duration in one call — under-waiting just wakes early to wait
again.

**One pending wait per thread** ([#1702](https://github.com/protoLabsAI/protoAgent/issues/1702)):
a new `wait` **supersedes** any still-pending wait for the same session (a stable
`wait:<session>` job id → cancel-then-add), so repeated waits can't stack up into
a pile of wake-ups that all fire into the thread. A user `schedule_task` uses its
own id and is untouched. Every scheduling is logged —
`[wait] thread=… in Ns (superseded a pending wait) → resume: …` — so a stacking
loop is visible in logs.

**Lead-agent only** (gated on the scheduler; subagents run bounded by `max_turns`
and don't get it). The yield is durable across restart (a persisted scheduler
job). For an absolute time or a recurring cadence use `schedule_task` instead —
`wait` is for "yield for a bit, then pick this back up".

## `ask_human`

```python
@tool
def ask_human(question: str) -> str
```

Pause the turn and ask the human operator a question, then continue with their
answer (human-in-the-loop, ADR 0003). Issues a LangGraph `interrupt()`, which
checkpoints the graph at the call site; A2A callers see the task transition to
`input-required` carrying the question, and resume it by sending a follow-up
message with the same `taskId`. **Lead-agent only** — it's hard-denied to
subagents (the interrupt is resumed by the lead turn's runner, and a subagent has
no checkpointer to resume one). On an **autonomous turn** (scheduler / inbox /
webhook / background) no operator is watching, so rather than parking the task
forever the runtime auto-answers the pause with a "no operator — proceed"
sentinel (bounded; it force-completes past the budget) — prefer proceeding with a
stated assumption there instead of asking. Use it only for a decision/input you
genuinely must wait on (an approval, a missing fact), never for narration.

## `request_user_input`

```python
@tool
def request_user_input(title: str, steps: list[dict], description: str = "") -> str
```

Ask the operator for **structured** input via a form dialog, then continue with
their response (the submitted fields, as a JSON object). Same HITL mechanics as
`ask_human` — LangGraph `interrupt()` → A2A `input-required` → resume on the same
`taskId`; **lead-agent only**, hard-denied to subagents; auto-answered on
autonomous turns.

`steps` is a list of form steps. Multiple steps render as a sequential **Back/Next
wizard** (step indicator; required fields gate Next/Submit), and the last step
submits every step's answers together. Each step is `{"schema": <JSON Schema
draft-07 of the step's fields>, "title"?: str, "description"?: str}` — supply at
least one step with fields (an empty `steps` is rejected). Field types per property
in a step's `schema.properties`:

- **text / number / boolean** — `{"type": "string" | "number" | "integer" | "boolean"}`; add `"format": "textarea"` for multi-line text.
- **single-choice cards** — `{"type": "string", "oneOf": [{"const": "pg", "title": "Postgres", "description": "Durable, multi-writer"}, …]}`. Each option renders as a selectable card with its label + description. (A bare `"enum": [...]` renders as a plain dropdown.)
- **multi-choice cards** — wrap the options in an array: `{"type": "array", "items": {"oneOf": [...]}}`; the value is a list.

Mark fields required via the step schema's `"required": [...]`. For a single
free-text or yes/no question, use `ask_human` instead.

## `check_inbox`

```python
@tool
async def check_inbox(priority_floor: str = "next", limit: int = 10) -> str
```

Pull pending **inbound messages** (ADR 0003) — webhooks, external systems, and
sister agents that posted to `POST /api/inbox` — and mark them delivered. Bound
only when an `InboxStore` is configured. `priority_floor` selects the tiers:
`now` (now only), `next` (now + next, default), or `later` (everything pending).
`now`-priority items have already fired an Activity turn; `next`/`later` wait for
this call so the agent decides when to surface them. Returns the items one per
line, or `"Inbox empty."`.

## `edit_soul`

```python
@tool
async def edit_soul(section: str, content: str, mode: str = "replace") -> str
```

**Guarded, off by default** ([ADR 0081](/adr/0081-self-authored-persona-edit-soul)). Bound to
the **lead agent only** when an operator opts in with `soul.self_edit_enabled: true`; bounded
subagents never receive it. Lets the agent durably refine its own persona by rewriting one
markdown **section** of its `SOUL.md` — `section` is matched case-insensitively (a missing
section is created), `mode` is `replace` (swap the body) or `append`.

**Scope is persona only** — identity, voice, values, temperament — never operating doctrine
(SOUL stays pure persona, [ADR 0079](/adr/0079-autonomous-operating-model)). Guardrails: a
single call edits one section (can't blow away the file), a 64 KB cap keeps the persona prompt
prefix bounded, and empty/no-op/invalid-mode edits are refused with an error string.

Every edit goes through `write_soul`, so the outgoing persona is **snapshotted to soul-history
([#1691](https://github.com/protoLabsAI/protoagent/issues/1691))** and is restorable from
Settings ▸ Identity. The change takes effect on the agent's **next turn**: the server injects its
graph-reload as a callback so the compiled graph rebinds (atomically — the current turn is
unaffected) without `tools/` importing `server/`. In builds with no callback wired
(subagent/eval/script), the save still lands and applies on the next natural reload.

**Never silent.** Every accepted edit publishes a `persona.self_edited` event (section, mode,
new revision) on the event bus, so the operator sees an identity change in the console even when
it happens on an autonomous (scheduled/activity) turn — and it leaves a trail if a prompt-injection
ever drove one. This is the transparency guardrail from ADR 0081's due-diligence against prior art
(Hermes keeps SOUL.md operator-only; OpenClaw invites unguarded self-edit and treats the soul as a
prompt-injection surface; Letta added a read-only persona guard after unconstrained self-edits
degraded identity).

## Adding your own

For tools that shell out, build on `tools/shell.py::run_command` (async; handles timeout/kill, missing-binary → structured error, env merge, stdin/cwd) or `tools/gh_cli.py` for `gh` specifically — don't hand-roll `subprocess`.

Follow the same pattern:

```python
from langchain_core.tools import tool

@tool
async def my_tool(required_arg: str, optional_arg: int = 5) -> str:
    """First line becomes the LLM's summary of the tool.

    Args:
        required_arg: What this argument is. LLM reads these docstrings.
        optional_arg: Optional with a sensible default.
    """
    try:
        result = await do_the_thing(required_arg, optional_arg)
    except Exception as e:
        return f"Error: {e}"
    return f"Success: {result}"
```

Then append it to the keyless tool list in `get_all_tools()` — keep the two conditional extensions below it so the bundled memory + scheduler tools still ship when their backends are configured:

```python
# Illustrative — the real signature is
# get_all_tools(knowledge_store=None, scheduler=None, inbox_store=None, tasks_store=None)
def get_all_tools(knowledge_store=None, scheduler=None, **backends):
    tools = [current_time, calculator, web_search, fetch_url, my_tool]
    if knowledge_store is not None:
        tools.extend(_build_memory_tools(knowledge_store))
    if scheduler is not None:
        tools.extend(_build_scheduler_tools(scheduler))
    return tools
```

> Prefer shipping new tools via a [plugin](/guides/plugins) (`register_tools`) — editing `get_all_tools` is the legacy core-edit path that conflicts on upstream re-sync.

See [Write your first tool](/tutorials/first-tool) for the full walkthrough.

## Related

- [Configure subagents](/guides/subagents) — tools are allowlisted per subagent
- [Environment variables](/reference/environment-variables) — SSRF allowlist vars affect `fetch_url`; scheduler backend selection lives there too
- [Eval your fork](/guides/evals) — the eval harness exercises every tool listed here end-to-end
- [Schedule future work](/guides/scheduler) — the firing model + multi-agent isolation story behind the scheduler tools
