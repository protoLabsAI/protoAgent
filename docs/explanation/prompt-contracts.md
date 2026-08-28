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
  into the request as the last message, never appended to the system prompt.
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
| 5 | `Context` | the `context` argument, verbatim | legacy callers only — the runtime delivers context as a projected message (ADR 0108 D2) |
| 6 | `Operating model` | `_build_operating_model(bound_tool_names)` | at least one autonomous primitive is bound (goal / tasks / schedule / watch / wait) |
| 7 | `Guidelines` | inline, fork-override point | always; the `task` and `wait` lines only when those tools are bound |

**Deliberately excluded:** retrieved memory, the prior-session digest, the
`<available_skills>` index, and `<working_state>` — these are the *projected
context* (see [Memory & knowledge store](memory-and-knowledge.md)), composed per turn
and delivered as the final message so the prefix stays cacheable. Tool schemas are
bound at the model API, never rendered into prose.

**Knobs that change the text:**

- `bound_tool_names` — the capability map in `graph/prompts.py` (`_GOAL_TOOLS`,
  `_TASK_TOOLS`, `_SCHEDULE_TOOLS`, `_WATCH_TOOLS`, `_WAIT_TOOLS`) decides which
  operating-model paragraphs and guideline lines appear. `None` means "emit
  everything" (legacy callers).
- `include_subagents` — whether the roster section exists at all.
- `SubagentConfig.lead_visible` — a workflow-internal subagent (`antagonist`,
  `verifier`, `synthesizer`, `codebase-mapper`, `review-finder`, `review-synthesizer`,
  `self-improve`) stays in the registry but never enters the lead's roster. Before this
  filter the roster was ~49% of the stable prompt.
- `config/SOUL.md` — the persona; the only part an operator is expected to edit.
- `projects` and a configured delegate registry add their sections.

## 2. Subagent — `build_subagent_prompt()`

`build_subagent_prompt(agent_name)` returns `SUBAGENT_REGISTRY[name].system_prompt`
**verbatim** — no SOUL, no roster, no operating model, no guidelines. A subagent's
prompt is its role description; its tool surface is the `tools` /
`disallowed_tools` allowlist on the same `SubagentConfig`, and `task` is always
disallowed (subagents cannot spawn subagents). Unknown names get a one-line generic
prompt.

`lead_visible` has **no effect** on this contract — it only filters the lead's roster.

## 3. External / ACP runtime — `build_stable_prefix()`

`runtime/context.py::build_stable_prefix(config, include_subagents, bound_tool_names)`
**delegates to `build_system_prompt`** with the same arguments, so for the same
inputs the external prefix is byte-equal to the lead prompt (a test asserts it).
`assemble_context()` / `ContextAssembler.assemble()` then attach the per-turn
volatile delta — the same projected context the native loop delivers (ADR 0108 D8) —
and `AssembledContext.as_prompt(message)` orders them *prefix, then delta, then the
turn's message*.

Because tools on this path resolve lazily (an MCP bus at session start), callers may
pass `bound_tool_names=None` and receive the full doctrine — honest only when every
capability is really reachable; thread the real set as soon as it is known.

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
| ACP prefix | = lead | = lead |
| Provider transform (`anthropic-oauth`) | + identity line | + ~20 |

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
