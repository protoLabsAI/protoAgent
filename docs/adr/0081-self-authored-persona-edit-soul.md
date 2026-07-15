# 0081 — Self-authored persona: a guarded `edit_soul` tool

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** Josh Mabry
- **Tags:** persona, tools, self-modification, config, trust
- **Related:** ADR 0079 (autonomous operating model — "SOUL.md stays pure persona"),
  ADR 0066 (goal trust / operator channel), ADR 0048 (settings IA — Identity panel owns
  SOUL), ADR 0028 (agent-owned goal), #1691 (SOUL version history)

## 1. Context & problem statement

`SOUL.md` is the instance's persona — its identity, voice, and temperament — read live by
`graph/prompts.build_system_prompt` and prepended to every turn's system prompt. Today it
is **operator-only**: the sole write path is the console Settings ▸ Identity panel →
`PATCH /api/config` → `server.agent_init._apply_settings_changes(soul=…)` →
`graph.config_io.write_soul` → graph reload. No agent-facing tool can touch it, and two
ADRs deliberately drew that line:

- **ADR 0079** — *"SOUL.md stays pure persona."* Operating doctrine belongs in
  `graph/prompts.py`, not SOUL.
- **ADR 0066 / 0048 / 0001** — "config/SOUL rewrite" is classified as an **operator `/api`
  capability**, deliberately *denied* to federated A2A peers. It sits above the trust
  ceiling on purpose.

The ask: let a long-lived agent **durably refine its own persona** — the way it shows up —
when it learns something about how it should behave, without a human editing YAML. That is
genuine self-modification, and it is in tension with both guardrails above. The question is
not "can we" (the write plumbing exists) but "how do we let the agent cross that line
*safely*, as an explicit operator choice, without eroding the ADR 0079 persona/doctrine
split or the ADR 0066 trust boundary."

A second, concrete constraint shaped the design: the system prompt is a **static string
baked at graph-construction** (`create_agent(system_prompt=…)`), so a bare `write_soul`
does not take effect until the graph is rebuilt — and **`tools/` must never import
`server/`** (the import-layering contract), so the tool cannot call the reload path
directly.

## 2. Decision

Add a single agent-facing tool, **`edit_soul`**, that is **off by default** and lives
entirely within the existing seams:

1. **Guarded, off by default.** New config field `soul.self_edit_enabled`
   (`LangGraphConfig.soul_self_edit_enabled`, default `False`). The tool binds to the lead
   agent only when an operator opts in. It is a `settings_schema.Field` (so it round-trips
   through config and is YAML/`/api`-settable) but `ui_hidden` — the dedicated Identity
   panel already owns SOUL, and a rendered toggle can join it later.

2. **Section-scoped, never whole-file.** `edit_soul(section, content, mode="replace"|"append")`
   edits one markdown section (matched case-insensitively; a missing section is created).
   A single call can't blow away the persona, which is the smallest useful blast radius.

3. **Persona only (ADR 0079 upheld).** The tool's contract instructs the model that SOUL is
   identity/voice/values *only* — never operating instructions, task doctrine, or tool
   rules. The scope guardrail is behavioral (prompt-level), reinforced by section editing
   and a size cap (64 KB) that keeps the persona prefix bounded.

4. **Reversible for free (#1691).** Writes go through `graph.config_io.write_soul`, which
   snapshots the outgoing persona to `soul-history/` before overwriting — so every
   self-edit is listed and restorable from Settings ▸ Identity, and a rollback is itself
   reversible.

5. **Live next turn via dependency injection, not an import.** The server passes its own
   `_reload_langgraph_agent` down as a `reload_callback` parameter through
   `create_agent_graph → get_all_tools → edit_soul`. This is runtime DI (the same shape as
   the injected `scheduler` / `tasks_store` / `goal_enabled` deps), so `tools/` and `graph/`
   never import `server/` and the import-linter contract stays green. The tool offloads the
   (synchronous, heavyweight) reload to a thread; rebinding `STATE.graph` is atomic, so the
   **current** turn keeps the old persona and the **next** turn gets the new one — the only
   coherent semantics for an in-flight edit. Absent a callback (subagent / eval / script
   builds) the save still lands and applies on the next natural reload.

6. **Lead-agent only.** No subagent build passes `soul_edit_enabled`, so a bounded
   subagent (`task()`) never receives `edit_soul` — consistent with subagents not being
   allowed to self-set goals (ADR 0028).

## 3. Consequences

**Positive.** A persona that can be refined by the agent itself, gated by an explicit
operator opt-in, reversible from the console, and live within one turn — without touching
prompt-evaluation semantics, the trust ceiling for federated peers, or the layering
contract.

**Negative / risks.**
- *Persona drift.* An agent could gradually rewrite itself into something the operator
  didn't intend. Mitigations: default-off, #1691 history + restore, and the 64 KB cap. A
  future guard could diff-review or require operator ack on self-edits.
- *Doctrine leakage into SOUL.* The ADR 0079 persona/doctrine split is enforced only by the
  tool's contract, not mechanically — a model could still write instructions into SOUL.
  Accepted for now; revisit if it happens in practice.
- *Reload cost.* Each accepted edit rebuilds the graph (re-warms cache). Self-edits are
  rare, so this is acceptable; the tool no-ops (no reload) when the content is unchanged.

## 4. Alternatives considered

- **Dynamic per-turn system prompt** (read SOUL live each turn, no reload). Cleaner
  take-effect story and would fix the REST route too, but it changes prompt-evaluation
  semantics and interacts with the cached prefix (`cache_warmer`, prompt-cache middleware).
  Out of scope; the injected-reload path achieves next-turn liveness without that change.
- **Reach SOUL via the fenced filesystem tools** (`tools/fs_tools.write_file`). SOUL lives
  at `config/SOUL.md`, outside the default `workspace` fence; enabling it would require an
  operator to register a writable project rooted above `config/`, and it would bypass the
  #1691 snapshot + reload machinery entirely. Rejected — a dedicated tool is safer and
  keeps the guardrails.
- **Whole-file `write_soul(text)`.** Maximum power, maximum blast radius. Rejected in favor
  of section-scoped edits.
- **Event-bus reload** (publish `soul.changed`, server reloads async). More decoupled but
  racy for "live next turn"; the injected synchronous callback is deterministic.
