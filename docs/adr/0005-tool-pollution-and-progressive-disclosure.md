# ADR 0005 — Tool Pollution & Progressive Tool Disclosure

- **Status:** Proposed (2026-05-31) — findings + ranked plan; no code yet
- **Date:** 2026-05-31
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** architecture, agent, tools, mcp, skills, context, prompt
- **Supersedes / Superseded by:** —

> Proposed. As the agent gains tools — especially when an MCP server exposes a
> large catalog — every bound tool's name + description + JSON schema lands in
> the model's function-calling array on **every** turn. Past ~10–15 tools this
> burns context and measurably degrades tool-selection accuracy ("tool
> pollution"). The fix is **not** a tool roster in the system prompt (that
> duplicates what the model already receives and adds bloat). It is to control
> the problem at the **binding layer**: gate large tool groups, let the existing
> skill system disclose the right tools per task, and — only once tool counts
> are high — defer tool schemas behind a search/expand step. This ADR records
> the findings (incl. how OpenClaw and Hermes solve it, and the current SOTA),
> audits where protoAgent already stands, and ranks the gaps. No implementation
> is committed here.

---

## 1. Context & Problem Statement

The lead agent's tools are bound via `create_agent(model, tools=all_tools, …)`
(`graph/agent.py`). LangChain binds each tool's **name + description + full JSON
input schema** into the provider's function-calling array, sent on **every** LLM
call. That is the canonical way the model "sees" a tool — so:

- A **tool roster injected into the system prompt is redundant**: it re-states
  names/descriptions the model already has, costing tokens for nothing. (This
  was the original "Auto-list only" idea from the tool-set audit; on inspection
  it solves a non-problem and was dropped.)
- The real cost scales with **how many tools are bound**, not with the prompt.
  The native tool set is modest (~19, see §3), but a single MCP server can
  export dozens-to-hundreds of tools, and protoAgent currently **binds every
  discovered MCP tool eagerly** (`tools/mcp_tools.py`). That is the pollution
  risk.

"Tool pollution" has two distinct faces, worth separating:

1. **Tool-count pollution** — too many *tool definitions* in context (tokens +
   selection accuracy). Addressed by binding fewer tools / deferring schemas.
2. **Result pollution** — large *intermediate tool outputs* filling context.
   Addressed by subagent isolation (already present here) and, at the extreme,
   code-execution sandboxes.

This ADR is primarily about (1), with (2) noted where relevant.

## 2. Reference Survey

### 2.1 OpenClaw

Anti-pollution is a **skills-as-progressive-disclosure** layer (the Anthropic
`SKILL.md` pattern), not tool retrieval. Only a skill's `name` + short
`description` are surfaced; the full `SKILL.md` body **and the tool subset it
wires up** load when the skill is selected. Tools are also grouped into named
*families* (bash, browser, canvas, cron, sessions, …) and scoped per session via
a sandbox allow/deny policy. Model: **a skill = a lazily-loaded bundle of
(instructions + tool subset)**, gated by config + sandbox. Curation, not search.

### 2.2 Hermes (Nous Research)

~64 built-in tools across ~10 toolsets, plus MCP — but the model rarely sees all
of them. A central `ToolRegistry`:

- **Toolset grouping** — `resolve_toolset(name) -> set[str]`, composable/nested;
  bind only the relevant toolsets per platform/profile.
- **`check_fn` capability gating** — each tool reports availability (API key
  present, binary installed, service reachable); unavailable tools aren't
  surfaced. Availability cached with a ~30s TTL.
- **Per-server MCP `include`/`exclude` filtering** — whitelist the few tools you
  actually use from a server (include wins over exclude); MCP tools namespaced
  `mcp_<server>_<tool>`; servers contributing zero tools don't become a toolset.
- **Subagent toolset isolation** — `delegate_task` runs in its own session with a
  scoped toolset; intermediate results never enter the parent context (this is
  their main *result*-pollution control).

No global `search_tools` meta-tool — Hermes relies on grouping + gating +
isolation.

### 2.3 State of the art

- **Anthropic deferred tools / Tool Search** — pass all tool defs but mark most
  `defer_loading: true`; only their *names* sit in context until the model calls
  a search tool that expands matches to full schemas. Reported ~85% token
  reduction on a 50+ MCP-tool setup and a selection-accuracy jump once past ~10
  tools (less confusion between similarly-named tools). *This very Claude Code
  harness uses exactly this pattern (ToolSearch + `select:<name>`).*
- **Code execution over MCP** — present servers as a filesystem of typed code;
  the model imports only the tools it needs and calls them in a sandbox, so
  intermediate data never hits context (~99% token cut in Anthropic's bench).
- **Cloudflare "Code Mode"** — collapse an entire API to two tools, `search()` +
  `execute()` (JS in a V8 isolate), fixed ~1K-token footprint regardless of API
  size.

Convergent pattern: **names-only by default → retrieve/expand on demand →
execute so intermediate data stays out of context.**

## 3. Where protoAgent Already Stands

Better than expected — several pieces of the above already exist:

| Capability | Status | Where |
|---|---|---|
| Native-tool capability gating (scheduler/inbox/knowledge/peers only bind when wired) | ✅ Present | `tools/lg_tools.py` `get_all_tools` (the `if knowledge_store:` / `if scheduler:` gates) — Hermes' `check_fn` equivalent |
| Skill system w/ on-demand disclosure of methodology | ✅ Present | `KnowledgeMiddleware.before_model` FTS5-retrieves top-k `SKILL.md` by the turn's context, 2K-token budget, injected as `<learned_skills>` (`graph/middleware/knowledge.py`; index in `graph/skills/`) |
| MCP namespacing + per-tool denylist | ✅ Present | `tools/mcp_tools.py` (`<server>__<tool>` prefix; `mcp_denylist`); collision-skip vs core names |
| Subagent tool isolation (result pollution) | ✅ Present | allowlisted `sub_tools` per subagent (`graph/agent.py`) |
| Skills declare a `tools:` frontmatter array | ⚠️ Parsed, unused | `graph/skills/loader.py` parses it; nothing consumes it for gating/surfacing |
| MCP per-server **allowlist** (`include`) + lazy connect | ❌ Missing | `mcp_tools.py` binds **all** discovered tools (minus denylist) eagerly |
| Deferred tool schemas / `search_tools` meta-tool | ❌ Missing | all tools bound up front |

The native tool set today (~19, lead agent, all stores enabled):

- **Keyless (5):** `current_time`, `calculator`, `web_search`, `fetch_url`,
  `ask_human` (lead-only HITL).
- **GitHub (4):** `github_get_pr`, `github_get_issue`, `github_list_issues`,
  `github_get_commit_diff`.
- **Notes (4):** `notes_list/read/write/revert`.
- **Memory (5, gated on knowledge_store):** `memory_ingest/recall/list/stats`,
  `daily_log`.
- **Scheduler (3, gated):** `schedule_task`, `list_schedules`, `cancel_schedule`.
- **Inbox (1, gated):** `check_inbox`.
- **Peers (2, gated on `PEER_*` env):** `peer_list`, `peer_consult`.
- **Task/workflow:** `task`, `task_batch`, `run_workflow`, `save_workflow`.
- **`execute_code`** (gated; never given to subagents).
- **MCP (variable, unbounded):** every discovered tool from every enabled server.

At ~19 native tools the count alone is under the ~10–15 "start deferring"
threshold for *concern*, but MCP makes the total unbounded — that's the lever.

## 4. Decision

**No implementation is decided in this ADR.** The decisions captured are
directional:

1. **Reject the system-prompt tool roster.** It duplicates the bound-tool
   schemas the model already receives and adds context with no upside.
2. **Treat tool pollution as a binding-layer concern**, attacked in this order:
   bind fewer / gate more first; defer schemas only when counts justify it.
3. **The skill system is the per-task disclosure surface** — lean on it (and the
   already-parsed-but-unused `tools:` frontmatter) rather than inventing a
   parallel mechanism.

## 5. Ranked Plan (impact → effort) — *for a follow-up, not committed here*

1. **MCP per-server allowlist + lazy connect** *(low effort, biggest win).*
   Add `mcp.servers[].tools.include` (+ keep `exclude`/global `denylist`); filter
   discovered tools in `build_mcp_tools` so a large server can't dump its whole
   catalog; skip connecting a server that's disabled or contributes zero tools.
   Pure Hermes pattern; fits the existing config shape (`graph/config.py`
   `mcp_servers`, `mcp_denylist`).
2. **Wire skills' `tools:` frontmatter** *(medium).* When a skill is retrieved
   for the turn, surface/prioritize its declared tool subset (and let heavy
   groups stay quiet otherwise). Turns the existing skill layer into the
   per-task tool selector — the OpenClaw model.
3. **Deferred `search_tools` meta-tool** *(medium-high; only past ~15 routinely
   relevant tools).* Bind a names-only roster + one retrieval tool that expands
   matching schemas and re-binds them for subsequent turns (Anthropic
   Tool-Search / this harness's ToolSearch). Real change to how `create_agent`
   binds tools (dynamic per-turn tool set).
4. **Code-execution facade over MCP** *(high; only if hundreds of endpoints and
   1–3 are insufficient).* Highest ceiling, most infrastructure.

Subagent isolation (result pollution) already exists and needs no work.

## 6. Consequences

**If pursued (esp. #1):**

- *Positive* — bounded, predictable context regardless of MCP catalog size;
  better tool-selection accuracy; the skill system earns its keep as the task
  router; no redundant prompt tokens.
- *Negative / costs* — #1 adds config surface (per-server filters) to document;
  #2 couples skill retrieval to tool surfacing (needs care so a mis-retrieved
  skill doesn't hide a needed tool); #3 is a non-trivial change to the binding
  model and needs a fallback for "tool not yet expanded".

**If deferred (status quo):** fine while MCP usage is light; the first
large-catalog MCP server is the trigger to implement #1.

## 7. Alternatives Considered

- **System-prompt tool roster ("auto-list only").** Rejected — duplicates the
  function-calling tool array the model already receives; adds tokens, helps
  nothing. (This ADR exists partly to record *why* not to do this.)
- **Hand-curated when-to-use guidance in the prompt.** Rejected as the primary
  lever — drifts from the live tool set and doesn't reduce binding cost; the
  skill system already carries methodology on demand.
- **Do nothing.** Acceptable short-term given the modest native count; explicitly
  the "deferred" branch above, with the large-MCP-server trigger noted.

## 8. Related

- [ADR 0001 — Extensibility & Plugin Architecture](/adr/0001-extensibility-and-plugin-architecture) — introduced SKILL.md + MCP + plugins.
- [ADR 0002 — Reusable Subagent Workflows](/adr/0002-reusable-subagent-workflows) — subagent isolation (result-pollution control).
- Code: `tools/mcp_tools.py` (MCP binding), `tools/lg_tools.py` `get_all_tools`
  (native gating), `graph/middleware/knowledge.py` + `graph/skills/`
  (skill retrieval/disclosure), `graph/prompts.py` (prompt assembly).
- Separately tracked: **directory-aware agent** (working-dir resolution +
  injection) — a related but distinct prompt/runtime concern, under design.
