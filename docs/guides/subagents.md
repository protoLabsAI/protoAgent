# Configure subagents

Subagents are specialized LLM workers the lead agent delegates to via the `task()` tool. The template ships one worked example: a `researcher` (web + memory, plan→search→synthesize→cite). This guide walks through adding more, trimming down, or turning the pattern off entirely.

## When to use subagents

- You have clearly separable phases in your agent's work (e.g. *research*, *synthesize*, *publish*).
- You want each phase to get its own focused system prompt and tool allowlist.
- You want each phase's tool calls audited + traced under the same session as the lead.

When *not* to use:

- For a single-loop agent. Adding a subagent hop for every call just wastes turns.
- When one delegation's output feeds the next — use sequential `task()` calls (or a chain) for that. For *independent* delegations, see `task_batch` below.

## Single vs. batch delegation

The lead gets two delegation tools:

- **`task(description, prompt, subagent_type)`** — one focused delegation. Unbounded output.
- **`task_batch(tasks)`** — several *independent* delegations run **concurrently** (e.g. research three topics at once). Each `tasks` item is `{description, prompt, subagent_type?}`. Results come back in input order; an individual task's failure is reported inline and doesn't abort the batch. Concurrency is capped by `subagents.max_concurrency` (default 4) and each result is truncated to `subagents.output_truncate` chars (default 6000) so a wide fan-out can't blow the parent context. Total latency is roughly the slowest task rather than the sum.

Prefer `task_batch` whenever the delegations don't depend on each other.

## 1. Define the config

First inspect the **Subagents** roster to see available roles. This panel is
read-only. For the built-in researcher, adjust the existing `subagents.researcher`
configuration; use [configuration reference](/reference/configuration#subagents).
For a new role, create a [plugin](/tutorials/first-plugin) and register it:

```python
from graph.subagents.config import SubagentConfig

def register(registry):
    registry.register_subagent(SubagentConfig(
        name="summarizer",
        description="Summarize supplied text in at most 200 words.",
        system_prompt="Return a concise summary and identify material uncertainty.",
        tools=["current_time"],
        max_turns=5,
    ))
```

Enable or reload the plugin, confirm the role appears in **Subagents**, then ask
the lead to delegate a small summary to `summarizer`. Inspect the result before
using it in a workflow. Tool names must exist and be permitted in the runtime.
See [`register_subagent`](/reference/plugin-registry-api#registry-register-subagent)
for the extension contract.

## 2. Expose the config shape

A plugin role does not require a new core dataclass field or a `from_yaml` loop
edit. If it needs operator controls, declare [plugin configuration](/guides/plugins#config-secrets-settings)
and use that configuration when constructing its `SubagentConfig`.

## 3. Add to the YAML

For the built-in researcher, use the active instance's configuration, located
with `config explain`:

```yaml
subagents:
  researcher:
    enabled: true
    tools: [current_time, web_search, fetch_url, memory_recall, memory_list, memory_ingest]
    max_turns: 40
```

Other plugin roles use their plugin's declared configuration; an arbitrary
`subagents.<name>` block does not automatically create a role.

## 4. Teach the lead agent

The delegation tool exposes registered role descriptions. Make each description
say when that role is useful and what it returns. Put recurring task instructions
in a [skill](/guides/skills) or [workflow](/guides/workflows); use persona settings
for the agent's identity and general style.

## 5. Turn subagents off entirely

Disable roles through their supported configuration: `subagents.researcher.enabled: false`
for the researcher, or disable the contributing plugin for plugin roles. Check
the roster afterward. Embedders can build a graph with `include_subagents=False`
to omit the delegation machinery; this is a Python integration option, not a
setting in the read-only roster.

## The adversarial-research roles

Beyond `researcher`, the template ships three roles that exist specifically to
make a research report *honest* — used by the `deep-research`
[workflow](/guides/workflows) ([ADR 0011](/adr/0011-deep-research-workflow)).
They're separate agents on purpose: no agent should grade its own homework.

- **`antagonist`** — adversarial reviewer. Steelmans the strongest *opposing*
  case, attacks weak/unsupported claims, and uses its own `web_search`/`fetch_url`
  to hunt disconfirming evidence. Outputs an "Opposition & weaknesses" memo.
- **`verifier`** — independent claim-checker. Extracts the load-bearing factual
  claims and labels each supported/unsupported/uncertain against sources.
- **`synthesizer`** — writes the final balanced report, folding the antagonist's
  opposition into a "Counterpoints & caveats" section, dropping anything the
  verifier didn't support, and only earning a high `Confidence` if the
  opposition was answered.

They're ordinary `SubagentConfig`s in the registry — reuse, retune, or drop them
like any other; the `deep-research` recipe just wires them into a DAG.

## Background jobs (run detached)

A delegation can run **detached** so it never blocks the turn: `task(run_in_background=true)`
(or `task_batch`) fires the subagent, returns a `bg-…` job id immediately, and the result is
drained back into the conversation when it finishes — with a live card on the console's
background-jobs surface ([ADR 0050](/adr/0050-background-subagents-reactive-notifications)).
Durable (survives restart), concurrency-capped (`BACKGROUND_MAX_CONCURRENCY`), and cancellable.

**Completion is a push, not mail waiting for pickup**
([ADR 0070](/adr/0070-background-results-push-resume)). When a job finishes, the server
**resumes the origin session** with a nudge turn: the pending `<task-notification>` drains into
that turn and the agent briefs the operator right where the work was requested (config
`background.auto_resume`, on by default; `false` restores pull-only delivery on the session's
next manual turn — the ADR 0050 Activity idle-wake then covers autonomous reaction instead).
A **substantial report** (> ~800 chars) is also **indexed into the knowledge store** at
completion, keyed to the origin session (`source_type: background_report`, trust tier 2 —
agent-derived), so only a 3 000-char excerpt rides in-context while the full text stays
searchable via `memory_recall` and openable from the console report card
(`GET /api/background/{job_id}`). Worker transcripts are **disposable**: `background:*`
sessions are excluded from session-summary persistence, the `<prior_sessions>` digest, and
retirement harvest — the jobs DB is the system of record. Jobs spawned from an **incognito**
thread inherit the no-memory-trail contract: no auto-resume, no indexing; the report still
drains normally.

For plugin jobs and deterministic background work, see [Background integration](/reference/automation#background-work).

## What you get for free

Every subagent call:

- Runs inside the same `trace_session` context as the lead → nested Langfuse span.
- Inherits the same `session_id` → audit-log entries from the subagent's tools land alongside the lead's.
- Emits the same `autonomous.cost.*` events on terminal completion.
- Is bounded by `max_turns`, a count of **tool rounds**: the subagent can call tools that many times, then it must answer. The runner converts it into LangGraph's step-based `recursion_limit` using the compiled middleware stack, so adding middleware never shrinks it. Round `max_turns + 1` hard-stops, and whatever the subagent had comes back marked `hard-stopped at max_turns` instead of an error.

Neither `task` nor `task_batch` is ever in a subagent's tool allowlist (subagents only get the tools named in their `tools:` list), so subagents can't spawn further subagents. This is intentional; one level of delegation is almost always enough.

## Related

- [Architecture explanation](/explanation/architecture) — how the task tool fits into the LangGraph runtime
- [Starter tools reference](/reference/starter-tools) — which tool names you can add to an allowlist
