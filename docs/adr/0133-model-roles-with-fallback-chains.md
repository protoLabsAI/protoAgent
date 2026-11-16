# ADR 0133 — Every core model role takes a fallback chain, set in config

- Status: Proposed (2026-11-15). Awaiting Josh's sign-off before anything is boarded.
- Date: 2026-11-15
- Deciders: Josh Mabry; protoAgent maintainers
- Related: [ADR 0132](./0132-chat-context-carryover.md) (the chat gist is a new role that uses this). projectBoard's coder ladders (`coders: {tier: [model, …]}` with `difficulty_tiers`) are the model to follow.

## Context

Josh, 2026-11-15: "we need to make sure this isn't just a haiku model hardcoded, we need to be able to set the model in config with fallback chains like we do with the coder models in project plugin board. similarly we'll need to make sure all the models for the core workflows can have fallback chains added."

Today only the **main model** (and the subagents built from it) has a fallback chain: `routing.fallback_models`, wired through langchain's `ModelFallbackMiddleware` (`graph/agent.py` ~677, ~1452). Every other core workflow takes **one model string with no fallback**:

| Role | Config field | Resolved where |
|---|---|---|
| Auxiliary (shared cheap model) | `routing.aux_model` | `graph/agent.py` `_resolve_aux_model` (~755), used by harvest, facts, compaction, subagents, self-edit, ACP |
| Compaction summaries | `compaction_model` (→ aux → main) | `graph/compaction_op.py`, `agent.py` |
| Harvest summary and fact extraction | none (→ aux → main) | `graph/conversation_harvest.py` ~659, `graph/memory_facts.py` ~114 |
| Goal verification (llm verifier) | `goal_eval_model` (→ aux → main) | `graph/goals/verifiers.py` ~341 (inline) |
| Memory selection | `memory_select_model` (→ aux → main) | `graph/projection.py` ~341 (inline) |
| SOUL drift judge | `soul_drift_judge_model` | `server/maintenance_loops.py` |
| Image description | `image_describe_model` | `graph/multimodal.py`, `middleware/multimodal_tool.py` |
| Subagent tiers | `subagents.<name>.model` | `graph/subagent_model.py` |

On 2026-11-13 this bit us. protoEngineer's aux was Codex only. When Codex hit its usage limit, every compaction summary, fact extraction and goal check failed with a 429, and compaction paused 240 s per three failures, all with nothing to fall back to.

## Decision

### D1 — A role's model is a chain

Every model role accepts either **a string** (one model, today's behaviour, unchanged) or **an ordered list** (the primary first, then the fallbacks):

```yaml
routing:
  aux_model: [anthropic-oauth:claude-haiku-5-5, anthropic-oauth:claude-sonnet-5-5]
compaction_model: [anthropic-oauth:claude-haiku-5-5, openai-codex:gpt-6-luna]
goal_eval_model: anthropic-oauth:claude-sonnet-5-5   # a string is a chain of one
```

Existing configs load unchanged.

**The main model is a chain too.** `model.name` accepts a list the same way, so the main model itself falls back to the next model when it is rate- or usage-limited:

```yaml
model:
  name: [anthropic-oauth:claude-opus-5-5, openai-codex:gpt-6.1-sol]
```

`routing.fallback_models` keeps working as the legacy way to write the main model's fallbacks: `model.name` plus `routing.fallback_models` is the main chain. Today it is empty (`[]`) on every fleet agent, so no agent has a main-model fallback yet.

### D2 — Blank inherits a whole chain; a set chain is exactly what's listed (confirmed by Josh 2026-11-15)

- A blank role inherits its parent's **entire chain**. Specific roles inherit the aux chain, and a blank aux inherits the main chain. This is today's inheritance rule, applied to chains.
- A set chain is **exactly** what's listed. Nothing is appended implicitly: an aux chain of two cheap models never silently escalates to the expensive main model. To get that, list it. Same rule as the board's coder ladders.

### D3 — One seam builds every role's model

A new module (`graph/model_roles.py`) provides two functions:
- `resolve_chain(config, role) -> list[str]`
- `create_role_llm(config, role, **kwargs)`, which returns the first model wrapped with `.with_fallbacks(...)` over the rest.

Every call site in the table above goes through it, replacing `_resolve_aux_model` and the inline resolutions. A fallback fires on **retryable** errors only: rate limit and usage limit (429, `rate_limit_error`, `usage_limit_reached`), 5xx, timeouts and connection errors. A bad request or a content refusal is not retried on another model.

### D4 — Visible when a fallback fires

When a role answers from a fallback, the log and the trace record the role, the model that answered, and why the earlier ones failed, once per failure streak rather than on every call. Settings › Models (or the existing provider status surface) shows the last fallback per role.

### D5 — Roles to cover

The roles are main, aux, compaction, harvest (new, inherits aux), facts (new, inherits harvest), goal_eval, memory_select, memory_gist (new, ADR 0132, inherits aux), soul_drift_judge, image_describe, and each subagent tier.

**Out of scope:** embeddings (`embed_model`) and transcription (`transcribe_model`). An embedding fallback changes vector dimensions and silently corrupts search, and transcription is a different API. They stay single models.

### D6 — Validated at load, editable in Settings later

- Each chain entry gets today's coherence check (the gateway-alias rule in `graph/config.py`). An incoherent entry is dropped with a log line naming the role and position, and a chain left empty falls back to inheriting.
- `src/graph/config.py` ↔ `tests/test_config_roundtrip.py` golden covers the new list shapes.
- A Settings UI for editing chains (an ordered list per role) is a separate slice. It goes to designSystem for a spec and preview, with Josh's approval, before it's built.

## Build slices (each ≤ 4 files, changelog excluded)

| Slice | What | Main files | Depends on |
|---|---|---|---|
| **M1** | Config: each role field accepts a string or a list; load-time validation; roundtrip golden | `src/graph/config.py`, `tests/test_config_roundtrip.py`, `src/graph/settings_schema.py`, a config test | — |
| **M2** | The seam: `resolve_chain` / `create_role_llm`, the retryable-error classifier, fallback logging | `src/graph/model_roles.py` (new), `src/graph/llm.py`, `tests/test_model_roles.py` (new) | M1 |
| **M3** | Move the aux-family call sites to roles: harvest, facts, compaction | `src/graph/conversation_harvest.py`, `src/graph/memory_facts.py`, `src/graph/compaction_op.py`, `src/graph/agent.py` (`_resolve_aux_model` becomes a thin shim) | M2 |
| **M4** | Move the inline resolvers: goal_eval, memory_select, drift judge, image describe | `src/graph/goals/verifiers.py`, `src/graph/projection.py`, `src/server/maintenance_loops.py`, `src/graph/multimodal.py` | M2 |
| **M5** | Subagent tiers take chains | `src/graph/subagent_model.py`, `src/graph/agent.py`, a test | M2, M3 (shares `agent.py`) |
| **M6** | Provider/status surface shows each role's chain and last fallback | `src/operator_api/provider_routes.py`, its test | M2 |
| **M7** | Settings UI for chains | after a designSystem spec and Josh's approval | M1, M6 |

ADR 0132's gist slice (S4) builds its model through `create_role_llm(config, "memory_gist")`, so it depends on M2.

## Acceptance scenarios

- A config with `aux_model: [a, b]`, where `a` returns 429, gets its compaction summary, harvest summary and goal verdict from `b`, and the log names the role and the fallback. Tested with fake models at the seam.
- A string `aux_model` behaves exactly as today. Existing config fixtures load unchanged.
- A blank `compaction_model` uses the aux chain, and a blank aux uses the main chain.
- A 400 bad request on `a` is not retried on `b`.
- A set aux chain never falls through to the main model unless it's listed.
- Embeddings and transcription are unchanged.

## Consequences

- A provider outage on a cheap model stops taking down compaction, harvest, facts, goal checks and gists, which is the 11-13 failure.
- Fleet configs can express a chain per role. Josh's chosen fleet aux chain (2026-11-15; Codex usage is available again after a midweek reset): `routing.aux_model: [anthropic-oauth:claude-haiku-5-5, openai-codex:gpt-6-luna, protolabs/fast]`. Specific roles left blank inherit it. Applying it to the fleet is an operator step once M3/M4 ship. A main-model chain per agent is the operator's call, not part of this ADR.
- One seam replaces about six ad-hoc resolvers, which makes future roles cheap to add.
