# Prompt contracts

protoAgent composes a model-visible prompt in four distinct ways. Each one is a
**contract**: a named builder, a fixed section order, a documented set of knobs, and a
size ceiling that CI enforces. This page is the reference for all four; the ceilings
live in `tests/test_prompt_budgets.py` and fail the build when a change silently
inflates a prompt.

Why this matters (ADR 0108, D3 and D8):

- **Honesty.** A prompt must only describe capabilities the model can actually call.
  The operating model and guidelines are *derived from the bound tool set*, so a
  stripped deployment gets a shorter prompt instead of instructions it cannot follow.
- **Caching.** The stable prefix is byte-identical turn to turn (ADR 0101). Anything
  per-turn — retrieved memory, the skills index, working state — is **projected**
  into the request as the last message, never appended to the stable prefix.
- **Parity.** Every runtime (native LangGraph loop, external/ACP, subagents) builds
  its prompt from the same functions, so there is one persona and one doctrine.

## 1. Lead agent — `build_system_prompt_parts()`

`graph/prompts.py::build_system_prompt_parts(workspace, include_subagents, context,
projects, bound_tool_names)` returns labeled `(label, text)` sections;
`build_system_prompt()` is exactly those texts joined by a blank line (a test pins
the equivalence, so the labels annotate the real prompt). Section order:

| # | Label | Source | Present when |
|---|-------|--------|--------------|
| 1 | `SOUL` | `instance_paths().soul_path` → `{workspace}/SOUL.md` → repo default | always (placeholder if none) |
| 2 | `Subagents` | `_build_subagent_section()` over `SUBAGENT_REGISTRY` | `include_subagents=True`; lists only `lead_visible` entries |
| 3 | `Managed projects` | `_build_projects_section(projects)` | `projects` non-empty (ADR 0007 fenced fs toolset) |
| 4 | `Collaboration` | `_build_collaboration_section()` | a delegate registry has names (#3042) |
| 5 | `Context` | the `context` argument under a `# Context` heading | legacy callers only — no runtime caller passes it since ADR 0108 D2 (context is a projected message) |
| 6 | `Operating model` | `_build_operating_model(bound_tool_names)` | at least one autonomous primitive is bound (goal / tasks / schedule / watch / wait) |
| 7 | `Guidelines` | inline, fork-override point | always; the `task` and `wait` lines only when those tools are bound |

**Deliberately excluded:** retrieved memory, the prior-session digest, the
`<available_skills>` index, and `<working_state>` — these are the *projected
context* (see [Memory & knowledge store](memory-and-knowledge.md)), composed per turn
and delivered as the final message so the prefix stays cacheable. Tool schemas are
bound at the model API, never rendered into prose.

**Knobs that change the text:**

- `bound_tool_names` — the capability map `graph/prompts.py::CAPABILITY_GROUPS`
  (goal / tasks / schedule / watch / wait → tool names) decides which operating-model
  paragraphs appear; the `task` and `wait` guideline lines are gated the same way.
  `None` means "emit everything" (legacy callers).
- `include_subagents` — whether the roster section exists at all.
- `SubagentConfig.lead_visible` — a workflow-internal subagent (`antagonist`,
  `verifier`, `synthesizer`, `codebase-mapper`, `review-finder`, `review-synthesizer`,
  `self-improve`) stays in the registry but never enters the lead's roster. Before this
  filter the roster measured 4,989 chars of a ~10k-char stable prompt. Plugin subagents
  registered at boot default to `lead_visible=True`, so a live roster is larger than the
  three-entry template fixture; the ceiling below guards the template's static composition.
- `config/SOUL.md` — the persona; the only part an operator is expected to edit.
- `projects` and a configured delegate registry add their sections.

## 2. Subagent — `build_subagent_prompt()`

`build_subagent_prompt(agent_name)` returns `SUBAGENT_REGISTRY[name].system_prompt`
**verbatim** — no SOUL, no roster, no operating model, no guidelines. A subagent's
prompt is its role description; its tool surface is the `tools` allowlist on the same
`SubagentConfig`, resolved by `graph/agent.py::_subagent_tools` against a tool map
snapshotted *before* the `task` / `task_batch` tools are appended — that is what makes
"subagents cannot spawn subagents" structural (the `disallowed_tools` field on the
config is not consulted; the HITL interrupt tools are additionally hard-denied there).
Unknown names get a one-line generic prompt.

`lead_visible` has **no effect** on this contract — it only filters the lead's roster.

## 3. External / ACP runtime — `build_stable_prefix()`

`runtime/context.py::build_stable_prefix(config, include_subagents, bound_tool_names,
projects)` **delegates to `build_system_prompt`**, so for the same `include_subagents`,
`bound_tool_names` and `projects` the external prefix is byte-equal to the lead prompt
(a test asserts it). `assemble_context()` / `ContextAssembler.assemble()` then attach
the per-turn volatile delta — the same projected context the native loop delivers
(ADR 0108 D8) — and `AssembledContext.as_prompt(message)` orders them *prefix, then
delta, then the turn's message*.

**The ACP prefix is honest by construction.** `runtime/acp_runtime.py` builds its
assembler with `include_subagents=False` (the `task` / `task_batch` tools exist only in
`graph/agent.py` and never ride the operator MCP bus) and a `bound_tool_names` set
resolved at the first turn from `runtime/operator_mcp_tools.py::resolve_exposed_names`
— the exact allowlisted-**and-bound** set the bus serves, never a guess (a factory that
fails leaves the legacy `None` with a warning). So the ACP prefix names no capability
the brain cannot call: an allowlist without goal tools yields no goal doctrine, and a
bus with no `task_create` yields no tasks doctrine. It carries no `Managed projects`
section on purpose: the fenced filesystem tools are appended in `graph/agent.py`
outside `get_all_tools`, so the bus never exposes them (a coding-agent brain has its
own file tools). `projects` is threaded through `build_stable_prefix` /
`assemble_context` / `ContextAssembler` for a runtime whose tool plane does carry them.

Callers that cannot know their tool set may still pass `bound_tool_names=None` and
receive the full doctrine — honest only when every capability is really reachable;
prefer a `bound_tool_names_factory` resolved at the first real turn.

## 4. Provider-transformed — `ProviderShapeMiddleware`

The three contracts above produce a `system_message`. Two providers cannot accept
it as-is, so `graph/middleware/provider_shape.py` applies **exactly one** structural
transform, chosen by the provider type of `request.model` on the current call
(ADR 0097). It runs innermost — after prompt caching and context injection — so it
always sees the final assembled prompt, and it is re-entered per fallback attempt so
one provider's shape can never leak into another's request.

| Provider type | Middleware | What changes | Size delta |
|---------------|------------|--------------|------------|
| `anthropic-oauth` | `ClaudeCodeIdentityMiddleware` | the system prompt becomes a block list whose **first block is exactly** the Claude Code identity line; the original text follows as its own block (idempotent — an already-prefixed prompt is repaired, not stacked) | + the identity line |
| `openai-codex` | `CodexResponsesInputMiddleware` | the system text (string or block list, flattened) moves to the Responses top-level `instructions` field via `model_settings`; `system_message` is dropped from the input | none — text is moved, not changed |
| anything else | — | unchanged | none |

Neither transform edits a single character of the composed text; they change *where*
and *in what container* it travels.

### What else touches the system message

Two more middleware re-container the system message on the way out, for every
provider:

- **`PromptCacheMiddleware`** (`graph/middleware/prompt_cache.py`, mounted first in
  `graph/agent.py`) turns the system text into a block list carrying `cache_control`,
  so the stable prefix is the cache anchor. Text unchanged.
- **`RoomCastMiddleware`** (`graph/middleware/room_cast.py`, mounted inside
  PromptCache so the anchor holds) appends one ephemeral per-thread `[room]` cast line
  as the **last** system block once a delegate has spoken on the thread — idempotent,
  never persisted, never part of the prefix.

So the model-visible system message is: *[identity line]* · stable prefix (cached) ·
*[room cast]*, with the identity line present only for `anthropic-oauth` and the cast
only on multi-participant threads.

## Measured sizes

Static composition under the test fixture — a 57-character SOUL, every autonomous
capability bound, no managed projects, no delegates. Tokens use the repo's
`chars // 4` heuristic (ADR 0101 — precise enough at decision thresholds, not for
billing).

| Contract / section | Chars | ≈ tokens |
|--------------------|------:|---------:|
| Lead · `Subagents` (3 lead-visible entries) | 3,024 | 756 |
| Lead · `Operating model` (all capabilities) | 2,531 | 632 |
| Lead · `Guidelines` (all capabilities) | 856 | 214 |
| **Lead · doctrine total (ex-SOUL)** | **6,411** | **1,602** |
| Lead · minimal (no subagents, no autonomous tools) | 412 | 103 |
| Subagent · smallest (`self-improve`) | 1,091 | 272 |
| Subagent · largest lead-visible (`researcher`) | 3,317 | 829 |
| Subagent · largest overall (`review-finder`) | 7,051 | 1,762 |
| ACP prefix (same `include_subagents` / `bound_tool_names` / `projects`) | = lead | = lead |
| ACP prefix as the runtime builds it (no roster, doctrine only for exposed tools, no projects) | ≤ lead | ≤ lead |
| Provider transform (`anthropic-oauth`) | + 57 (identity line) | + 14 |

The SOUL is excluded on purpose: it is operator content with no engineering ceiling.

## Budget ceilings

`tests/test_prompt_budgets.py` asserts each figure below. Ceilings are the measured
size × ~1.2, rounded up to a readable number — enough headroom for wording edits,
tight enough that a new roster entry, a new doctrine paragraph, or an accidental
duplicate section fails CI with the actual and allowed sizes printed.

| Golden | Ceiling (chars) |
|--------|----------------:|
| Lead doctrine total (ex-SOUL, all capabilities) | 7,700 |
| Lead `Subagents` section | 3,700 |
| Lead `Operating model` section | 3,050 |
| Lead `Guidelines` section | 1,050 |
| Lead minimal (no subagents, no autonomous tools) | 500 |
| Any subagent prompt | 8,500 |
| Any **lead-visible** subagent prompt | 4,000 |

Structural invariants the same file pins: the roster lists only `lead_visible`
subagents; binding one capability group never mentions another group's tools; an
empty bound set produces no operating model; the ACP prefix equals the lead prompt;
the two provider transforms preserve the composed text byte-for-byte.

### Raising a ceiling deliberately

1. Run `python -m pytest tests/test_prompt_budgets.py -q` — the failure prints the
   actual size and the ceiling.
2. Decide whether the growth is intended. A new lead-visible subagent or a new
   doctrine paragraph costs every turn of every session; prefer trimming, or marking
   a subagent `lead_visible=False` when only workflows pick it.
3. If it is intended, raise the constant in `tests/test_prompt_budgets.py` **and**
   update the tables on this page in the same PR, with one line saying why.

## See also

- [ADR 0108 — Context Architecture v2](../adr/0108-context-architecture-v2.md) — D3 (capability-derived prompt), D8 (one projection for every runtime)
- [ADR 0101 — Context lifecycle](../adr/0101-context-lifecycle-log-surface-pressure.md) — the cache discipline the stable prefix serves
- [ADR 0097 — Native OAuth-subscription providers](../adr/0097-native-oauth-subscription-providers.md) — why the two provider transforms exist
- [Memory & knowledge store](memory-and-knowledge.md) — what the projected context contains
- [Subagents](../guides/subagents.md) — the registry the roster is rendered from
