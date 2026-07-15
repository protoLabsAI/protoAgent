# 0081 — Self-authored persona: a guarded `edit_soul` tool

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** Josh Mabry
- **Tags:** persona, tools, self-modification, config, trust
- **Related:** ADR 0079 (autonomous operating model — "SOUL.md stays pure persona"),
  ADR 0066 (goal trust / operator channel), ADR 0048 (settings IA — Identity panel owns
  SOUL), ADR 0039 (event bus — the operator notice), ADR 0071 (plugin trust model),
  ADR 0028 (agent-owned goal), #1691 (SOUL version history)

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

### Prior art (due-diligence, 2026-07)

The two comparable runtimes sit at opposite extremes, and the memory-layer canon (Letta/
MemGPT) already learned the lesson this ADR encodes:

- **Hermes Agent (NousResearch)** — its identity file is *also* named `SOUL.md`, and it is
  **operator-only**: the docs say *"edit `~/.hermes/SOUL.md` to change Hermes' personality,"*
  and the post-turn self-improvement pass runs with a toolset "restricted to `memory` and
  `skill_manage`" — it *structurally cannot* touch the persona. The agent self-writes memory
  and skills, never identity. Notably, Hermes users are asking for exactly this capability
  ([hermes-agent#11919](https://github.com/NousResearch/hermes-agent/issues/11919), open:
  *"SOUL.md should evolve with usage, not just initialize once"*), and it remains unshipped.
- **OpenClaw** — the opposite: its default `SOUL.md` template *invites* self-edit
  (*"This file is yours to evolve. As you learn who you are, update it… If you change this
  file, tell the user — it's your soul, and they should know."*), reachable via the generic
  `write`/`edit` fs tools with only advisory guardrails. The cost is a documented
  **prompt-injection attack surface**: security researchers report real "SOUL.md
  modification attempts," where *"a compromised SOUL.md means a permanently hijacked agent
  that survives restarts"* ([Permiso](https://permiso.io/blog/inside-the-openclaw-ecosystem-ai-agents-with-privileged-credentials)).
- **Letta / MemGPT** (memory-layer analogue) — pioneered an agent-editable **persona block**
  (read-write by default), then *added a `read_only` guard* precisely because unconstrained
  self-editing degraded identity: practitioners observed *"the agent over-writing core memory
  until it loses sight of its persona"* ([Letta docs](https://docs.letta.com/guides/agents/memory-blocks)).
  Writes are full-replace with character limits.

The field's convergent guardrail set — read-only-by-default/off, versioned + rollback,
bounded scope, and **separation of identity from operating doctrine** — is what this design
adopts. `edit_soul` deliberately lands in the **guarded middle**: more capable than Hermes
(the persona *can* evolve, which its users want), far safer than OpenClaw (off by default,
section-scoped, snapshotted, notified). See PR #1985's due-diligence writeup.

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

7. **Never silent — operator notified (transparency guardrail).** Every accepted edit
   publishes a `persona.self_edited` event (section, mode, new revision) on the event bus
   (ADR 0039, best-effort via `HOST.publish`), so the change surfaces in the console even
   when it lands on an **autonomous turn** (scheduled / activity) with no human watching the
   chat — and leaves a trail if a prompt-injection ever drove one. This is OpenClaw's *"tell
   the user — it's your soul"* convention, made a real signal rather than advisory text.

## 3. Consequences

**Positive.** A persona that can be refined by the agent itself, gated by an explicit
operator opt-in, reversible from the console, and live within one turn — without touching
prompt-evaluation semantics, the trust ceiling for federated peers, or the layering
contract.

**Negative / risks.**
- *Persona drift — and our guard is reactive.* An agent could gradually rewrite itself into
  something the operator didn't intend. The failure is real prior art: Letta's community hit
  *"the agent over-writing core memory until it loses sight of its persona"*, and the drift
  literature (e.g. [arXiv 2601.04170](https://arxiv.org/html/2601.04170), "Adaptive
  Behavioral Anchoring") warns that self-referential edit loops degrade *gradually and
  invisibly* — each edit becomes context for the next. Our mitigations (default-off, #1691
  history + restore, 64 KB cap, the operator notice) are **reactive** — they let a human
  catch and roll back drift, but do not *detect* it. The field's recommended addition is a
  **periodic baseline anchor / drift check** (diff or re-weight against the original persona);
  we do not have one. **Follow-up:** a `soul_revision`-tagged drift check, and/or an
  operator-ack mode for higher-autonomy fleets. Documented, not built.
- *Doctrine leakage into SOUL.* The ADR 0079 persona/doctrine split is enforced only by the
  tool's contract, not mechanically — a model could still write instructions into SOUL. This
  is the exact lived failure of self-appended instruction files (the CLAUDE.md/AGENTS.md
  community reports operating cruft accreting until adherence degrades past the ~150–200
  instruction ceiling — [arXiv 2606.15828](https://arxiv.org/pdf/2606.15828)). Section-scope
  + the 64 KB cap bound the blast radius; a future mechanical guard (reject edits that look
  like imperative tool/step instructions) could harden it. Accepted for now.
- *Prompt-injection attack surface.* A writable, authoritative identity file is a known
  target — OpenClaw sees real-world "SOUL.md modification attempts," and a hijacked persona
  *survives restarts*. This is the primary reason `edit_soul` is **off by default,
  operator-gated, and section-scoped with a snapshot on every write** (a poisoned edit is
  visible via the notice and revertible from history). Operators enabling it widen this
  surface deliberately; the ADR 0071 trust model applies.
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
