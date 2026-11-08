# ADR 0128 — Generation profiles: each subagent role gets its own output, reasoning and context budget

- Status: Accepted (2026-11-08; Josh answered the four open questions, recorded under "Decisions taken" below)
- Date: 2026-11-08
- Deciders: Josh Mabry; protoAgent maintainers
- Implementation: not started. Planned slices: S0 measure, S1 config, S2 plumbing, S3 empty-at-cap retry, S4 review-finder roles, S5 Vera rollout (epic #4875).
- Amends: [ADR 0001](./0001-extensibility-and-plugin-architecture.md) (per-subagent `model` override becomes per-subagent `profile`). Respects [ADR 0101](./0101-context-lifecycle-log-surface-pressure.md) D1, [ADR 0106](./0106-provider-registry.md) "Behavior" block, [ADR 0115](./0115-gateway-inflight-limiter.md) D6, and the #3944 model precedence.

All code citations are `origin/main` at `dfad62568` (v0.233.1+). Paths are under `src/`. Anything marked **(unverified)** is inference not yet confirmed from code; S0 checks it.

## Context

The 2026-11-08 fleet audit (#4875, finding F6) measured Vera's PR-review panel from about 10-30 to 11-08:

- 296 of 20,225 calls (1.5%) emitted more than 8k output tokens. Those calls were **25% of all output tokens**. They appeared in 170 of 494 panels and were concentrated in finders: correctness 161, removed-behavior 70, crossfile 52.
- 12 calls hit the 32,768-token cap. **94 of the 296 returned empty visible content.** One example is careercoach#89 `find_correctness`: 32,768 output tokens, content `""`.
- A single call can take about 11 minutes (projectBoard#688: 28,978 output tokens).
- In 164 of 393 panels the slowest lane contained a call over 4k output tokens.
- Even a routine tool-call turn carries about 150–300 reasoning tokens.
- A regression what-if (R² ≈ 0.63) says capping reasoning at 4k per call would cut about 15% of finder-stage wall time (range 10–20%).

Finding F2 in the same issue adds the context side. Blank, reasoning-only replies are 0% below 40k tokens of prompt and **4.1% at 60–80k**. Per-stream decode falls about 40% as resident context grows.

### How generation settings flow today

There is **one generation block per process**, and every model build reads it.

| Knob | Where it is defined | Where it reaches the wire |
|---|---|---|
| `model.max_tokens` (default 32768) | `graph/config.py:867`, parsed `:2314` | gateway `graph/llm.py:1080`; anthropic-oauth `graph/providers/anthropic_oauth.py:576`; **ignored** by openai-codex (`openai_codex.py:101-102`, where the backend rejects it) |
| `model.thinking` ("" / enabled / disabled) | `graph/config.py:964-971`, parsed `:2337` | gateway `extra_body.thinking` `graph/llm.py:1133-1134`; anthropic-oauth adaptive or manual shape `anthropic_oauth.py:487-538` (#4418, v0.212.0) |
| `model.reasoning_effort` | `graph/config.py:968-972`, parsed `:2338` | gateway top-level `graph/llm.py:1121-1122`; anthropic-oauth `output_config.effort` `anthropic_oauth.py:536-538, 590-591`; codex `reasoning.effort` `openai_codex.py:109-111` |
| `model.chat_template_kwargs` | `graph/config.py:961`, parsed `:2336` | gateway `extra_body` `graph/llm.py` (the same block as `thinking`) |
| Manual thinking budget | none in config | derived from effort only for Claude models older than 4.6: `_LEGACY_THINKING_BUDGETS` `anthropic_oauth.py:460-462`, clamped below `max_tokens` `:524-530` |
| Per-turn effort (`/effort`) | request metadata `server/chat.py:966-967` | `ModelOverrideMiddleware._llm_for` caches one client per (model, effort) `graph/middleware/model_override.py:181-192`. **Lead graph only**: a subagent's `create_llm(config, model_name=sub_model)` passes no effort (`graph/agent.py:1123`) |
| `model.strip_prior_turn_thinking` | `graph/config.py:904-909` | anthropic-oauth only, `anthropic_oauth.py:594` (#4415) |
| Output budget fitted to the window | n/a | `_fit_output_budget` only **lowers** `max_tokens` when the prompt would not fit beside it (`graph/llm.py:576-603`, #3502). It skips budgets ≤ 8,192 (`:451-454, :584`) |
| `compaction.max_tokens` / `system_thread_max_tokens` | `graph/config.py:1053, 1062` | the lead thread's compaction trigger ceiling. It is not an output budget and does not apply to subagents |
| `context.budget_pct` | `graph/config.py:1362` (ADR 0108 D6) | the lead's projected injected context. It does not apply to subagents |

**Per-subagent choice is model only.**

- `SubagentConfig.model` is defined at `graph/subagents/config.py:64-67`.
- Precedence is pin > turn override > `routing.aux_model` > main model (`graph/subagent_model.py:86-106`, #3944). That one resolver is used by `task`, `task_batch`, slash runs, workflow steps (`graph/sdk.py:172-224`) and background jobs.
- The YAML overlay `subagents.<name>.model` exists only for `researcher`: `_SUBAGENT_MODEL_SLOTS = ("researcher",)` (`graph/config.py:97`), and `_apply_config_subagents` knows only that base (`server/agent_init.py:1057-1085`). An operator **cannot** retune `review-finder` from config today.
- Neither `task()` (`graph/agent.py:1665-1672`) nor `sdk.run_subagent` nor a workflow step (step keys: `id`, `subagent`, `prompt`, `depends_on`, `gate`, `timeout`) can pass a model budget.

**The subagent's context window already drives two guards.** `sub_window` (`graph/agent.py:1182`) sizes the tool-result pruner (`:1185-1194`, which fires at 0.6) and the completion guard's context wrap-up note (`completion_guard.py:56`, which fires at 0.8). Both read the *physical* window, which the gateway advertises as 196,608 for `protolabs/smart`.

**Empty completions today (#3584, #4115, v0.200.0).** `CompletionGuardMiddleware` is attached **only** to subagents that declare a deliverable (`graph/agent.py:1246-1258`). It is not on the lead graph. Inside it:

- A run-ending turn with at least 4,000 characters of `reasoning_content` and at most 200 characters of text is "reasoning-only" (`completion_guard.py:104-113`). It gets **one** retry with that turn's reasoning dropped and thinking off (`:316-364`).
- `thinking_off_settings` returns `None`, so the retry runs **with thinking still on**, unless the slot's `extra_body` already carries `thinking: enabled` or `chat_template_kwargs` (`:129-153`).
- The guard never reads `finish_reason`. A turn cut at `max_tokens` and a turn that stopped by EOS inside `<think>` take the same path, and `finish_reason` appears only in the give-up log (`:156-163`).
- Every protoAgent call streams, so the gateway's `blank_recovery` continuation never fires (`:23-25`).

### Who owns what in the audited path

The `review-finder` **subagent is core**: `REVIEW_FINDER_CONFIG`, `graph/subagents/config.py:438-552`, `max_turns=60`, registered `:857`. The **steps** that call it (`find_correctness`, `find_removed_behavior`, `find_crossfile`, `find_conventions`) are in **pr-reviewer-plugin** `workflows/code-review-structural.yaml:157, 238, 319, 409`, and the plugin names them in `LLM_FINDER_STEPS` at `dispatch.py:281`. The band-aids this ADR aims to retire also live in the plugin: `finder_timeout_s` `dispatch.py:1617-1645`, and `panel_attempt_timeout`. **Vera's deployment** is the qaEngineer bundle (`deploy/vera.langgraph-config.yaml`). That file deliberately does not pin `model:` (lines 42-43), so Vera's live `model:` block lives in her config volume.

Where Vera's thinking comes from is **(unverified)**. protoLab's notes say the `protolabs/smart` alias serves Qwen3.6-27B "thinking + preserve_thinking", with thinking on by template default (`protoLab/CLAUDE.md:77-80, 241`). If Vera's live config sets neither `model.thinking: enabled` nor `chat_template_kwargs`, then the #4115 thinking-off retry is a no-op on Vera (see above), which would explain why 94 blank calls remain. S0 checks this.

## Decision

**D1 — Named generation profiles.** A new `model.profiles` map holds named, partial generation settings:

```yaml
model:
  max_tokens: 32768          # global layer, unchanged
  thinking: ""
  reasoning_effort: null
  profiles:
    routing:   { thinking: disabled, max_tokens: 4096 }
    judgement: { thinking: enabled, reasoning_effort: high, max_tokens: 16384 }
    lane:      { context_budget: 48000 }
```

A profile can set any of:

- `max_tokens`
- `thinking`
- `reasoning_effort`
- `thinking_budget`: a hard reasoning cap, honoured only where the provider has one (D5)
- `chat_template_kwargs`: merged, not replaced
- `context_budget`: the input-token window that a subagent's pruner and wrap-up guard treat as "full", in place of the physical window. It never raises the physical window.

An unset field inherits from the layer below. Profile names are free-form. Core ships **no profile definitions**, so an untouched config behaves exactly as today (D7).

**D2 — Resolution order: per-call > subagent > role > global, merged field by field.**

| Layer | Set by | Example |
|---|---|---|
| per-call | a workflow step `profile:`; `sdk.run_subagent(..., profile=)`; a guard retry (D4) | the step `find_correctness` with `profile: judgement` |
| subagent | operator YAML `subagents.<name>.profile` / `.final_profile`, now for **any** registry subagent, plugin ones included (generalising `_apply_config_subagents`) | `subagents.review-finder.profile: lane-cheap` |
| role | the code: `SubagentConfig.profile` and `SubagentConfig.final_profile`, which name profiles | core `review-finder` declares `profile="routing"`, `final_profile="judgement"` |
| global | `model.max_tokens` / `thinking` / `reasoning_effort` / `chat_template_kwargs` | today's block |

- **The resolver.** One host-free module, `graph/generation_profile.py`, holds the resolver. It runs next to `resolve_subagent_model` and produces a `ResolvedProfile`. `create_llm(config, model_name=, reasoning_effort=, profile=)` applies it to a `dataclasses.replace` view of the config (`LangGraphConfig` is a dataclass, `graph/config.py:859-860`). Each provider builder keeps reading `config.max_tokens` / `.thinking` / `.reasoning_effort` unchanged.
- **Unknown names.** A role or subagent layer that names a profile the operator never defined resolves to nothing. It is not an error, and it logs once at boot.
- **Lead turns.** The per-turn `/effort` keeps its current scope, the lead graph only, and stays the per-call layer there. Subagents do not inherit it, as today (`graph/agent.py:1123`).
- **Plugins** declare a role the same way core does, through `SubagentConfig.profile` on `registry.register_subagent`. A recipe step can request a profile by name. Neither one defines budgets: budgets belong to the operator.

**D3 — The judgement turn is a phase of the lane, not a guess about the next call.** We cannot know in advance whether a call will produce a tool call or the final answer. A lane therefore runs its calls on `profile` (the routing role) and switches to `final_profile` (the judgement role) at points the host already detects:

1. after the completion guard's turn-budget or context wrap-up note (`completion_guard.py:202-238`);
2. on every guard nudge and closing-line re-ask;
3. *(Option B, gated on S0 data: see QUESTIONS Q2)* when a run-ending turn produced under `profile` arrives. That turn is kept as a draft, and the guard re-asks once under `final_profile` ("write the final deliverable; your draft follows").

The switch swaps `request.model` to a client cached per (model, profile). This is the pattern `ModelOverrideMiddleware` already uses (`model_override.py:181-192`). A subagent that declares no `final_profile` runs every call on `profile`.

**D4 — An empty completion at the cap is a failed turn, not progress.** "Empty at cap" means a model turn with no tool calls, visible text of 200 characters or less, and either `finish_reason` in {`length`, `max_tokens`} or a reasoning-only shape (`completion_guard.py:104-113`). Such a turn:

- **Is retried once, down a ladder.** First the same profile with thinking off (`thinking_off_settings`, now also emitting `chat_template_kwargs.enable_thinking: false` when thinking is on by gateway default rather than only when `extra_body` already carries it). Where the provider has no thinking-off switch (Claude 5.x), the retry uses `reasoning_effort: low` and half the `max_tokens`. The failed turn's reasoning is dropped from history, as it is today (`:116-126`).
- **Is classified, not hidden.** It counts as `empty_at_cap` in telemetry (D6), whether or not the retry recovers it.
- **Runs outside deliverable-declaring subagents too.** The empty-at-cap detector is split out of `CompletionGuardMiddleware` into a small middleware that every subagent stack gets. The lead graph also gets it, behind a flag (QUESTIONS Q3).

**D5 — Providers that ignore a knob degrade silently and say so once.**

| Provider | `max_tokens` | `thinking` | `reasoning_effort` | `thinking_budget` |
|---|---|---|---|---|
| Gateway → vLLM Qwen (`protolabs/smart`) | honoured | on/off via `chat_template_kwargs.enable_thinking` (probed 2026-10-10, `completion_guard.py:135-138`) | **(unverified)**: whether LiteLLM forwards it to vLLM is not known; assume ignored | **(unverified)**: needs a vLLM thinking-budget feature. Assume none, so `max_tokens` is the only bound and D4 catches the blank |
| Gateway → DeepSeek / OpenAI o-series | honoured | `extra_body.thinking` | honoured (`config.py:964-970`) | none |
| anthropic-oauth, Claude ≥ 4.6 / 5.x | honoured | adaptive. `disabled` is a 400 on 5.x and is already omitted (`anthropic_oauth.py:507-510`) | `output_config.effort` | ignored (no `budget_tokens` on 4.7+) |
| anthropic-oauth, Claude < 4.6 | honoured | manual | maps to the legacy budget | becomes `budget_tokens`, clamped `< max_tokens` (`:524-530`) |
| openai-codex | **ignored** (`openai_codex.py:101-102`) | n/a | `reasoning.effort` | none |
| `acp:` | none | none | none | none |

Every dropped knob is logged once per (provider type, knob) at the first build. It never fails a call. `context_budget` is provider-independent because it only feeds host-side guards.

Anthropic documents that changing thinking parameters invalidates cached message prefixes, while the system prompt and tools stay cached **(verify before rollout)**. A D3 phase switch on a Claude lane therefore costs one cache miss on the history. That fits ADR 0101 D1, "invalidation vectors … must not multiply", only because the switch happens at most once or twice per lane. On vLLM, `enable_thinking` changes only the generation-prompt tail, so prefix caching is unaffected **(unverified, but consistent with the Qwen template)**.

**D6 — Telemetry that proves the win.** Each per-call usage row (`_extract_subagent_usage`, `graph/agent.py:833-872`) gains these fields:

- `reasoning_tokens`, from `usage_metadata.output_token_details.reasoning` when the provider reports it, else null;
- `finish_reason`;
- `text_chars`;
- `has_tool_calls`;
- `profile`, the resolved name or `""`;
- `phase` (`explore` / `final` / `retry`);
- `empty_at_cap` (bool).

Prometheus `llm_calls_total{model,finish_reason}` (`observability/metrics.py:75-78`) gains a `profile` label. A new `llm_empty_completions_total{model,profile,reason}` counter is added. The S0 script reproduces the audit's metrics from these rows: share of calls over 8k output, those calls' share of output tokens, cap hits, empty-at-cap count, and finder-stage wall time per panel. It is run before and after.

**D7 — Back-compat.** With no `model.profiles`, the global block applies to every call, as today:

- A role name with no definition resolves to nothing.
- `subagents.researcher.model` keeps working.
- The new `subagents.<name>` keys are optional.
- The D4 retry ladder is today's thinking-off retry plus the `finish_reason` trigger and the chat-template switch. With profiles unset it changes behaviour only for turns that are empty at the cap, which today end the lane.
- `config_to_dict` / the golden (`tests/test_config_roundtrip.py:276, 303, 345`) gain `model.profiles: {}` and nothing else changes.

## Decisions taken (Josh, 2026-11-08)

1. **Core ships profile names only, not numbers.** Core defines the names (`routing`, `judgement`, `default`) and which role uses which. The values, such as a 4k reasoning cap on routing turns, live in the operator's config (Vera's qaEngineer bundle). With no profiles configured, behaviour is unchanged.
2. **Where the judgement turn comes from is decided from S0's data.** S0 measures first. The default is the wrap-up points the host already detects; a one-time re-ask of the finder's draft answer is added only if S0 shows the judgement turn isn't isolated.
3. **The empty-at-cap retry covers every subagent now. Main chat turns get it behind a flag, off by default,** because a retried chat turn is visible to the operator.
4. **The per-role `context_budget` stays in this ADR. The gateway and lane-scoping context fixes (finding F2) are tracked separately:** protoLabsAI/pr-reviewer-plugin#319 and protoLabsAI/homelab-iac#258.

## Consequences

- Finder navigation turns stop paying for open-ended thinking. A runaway turn is bounded by the routing `max_tokens` instead of 32,768 tokens and about 11 minutes. The audit estimates 10–20% of finder-stage wall time, plus less decode load on the shared two-replica pool.
- Once S0's post-measure confirms the win, pr-reviewer can retire `finder_timeout_s: 3150`, the `panel_attempt_timeout: 4050` overlays, and the "stop and answer" prompt clauses. Until then they stay.
- **Recall risk.** A capped or thinking-off judgement can miss hard logic. The judgement profile stays generous, and Vera's eval set (`qaEngineer/data/review-eval`, protoLab #24/#26) is the gate, not telemetry.
- `create_llm` gains one parameter, and client caches gain one key dimension. Settings ▸ Model ▸ Behavior (ADR 0106) gains a profiles editor. That is a UI follow-up; YAML and `POST /api/config` come first.
- Vera's profiles must be written to her live volume (`POST /api/config`), because the qaEngineer seed file applies only on first boot.
- F2's gateway-side fixes (#258, advertising a reliable window) are **not** covered. `context_budget` is the agent-side half: it lets a role treat 48k as full even while the gateway advertises 196k.

## Alternatives considered

- **Lower `model.max_tokens` globally (e.g. 8k).** One line, but it cuts the lead's long answers and the judgement turn alike. On a thinking model it turns runaways into *more* blank replies, because a cap with no content is exactly the 94-call failure.
- **Turn thinking off for Vera entirely (route finders to `protolabs/fast`).** Already possible through `SubagentConfig.model`, but it trades away judgement-turn quality across the board. protoLab measured thinking's benefit on tool-call turns as real but small, and a model switch also changes the KV pool and the fallback chain.
- **Pass budgets through `task()` arguments.** This lets the lead model pick budgets for itself. Budgets are an operator and role decision, not a model decision, and it would widen the tool schema on every turn.
- **A per-model profile (on the provider registry entry) instead of a per-role profile.** It does not separate a finder's navigation turn from its judgement turn on the same model, which is the whole finding.
- **Decide the profile per call by predicting whether the model will call a tool.** There is no reliable signal before the call. D3's phase points are observable.
- **Rely on the gateway's `blank_recovery` continuation.** It never sees streamed calls (`completion_guard.py:23-25`), and continuing a degenerate `<think>` loop resumes the loop (#4108).
