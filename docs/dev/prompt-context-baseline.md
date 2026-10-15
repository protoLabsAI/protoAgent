# Prompt/context cleanup baseline

Epic #4216, baseline #4217. Source baseline: `8e0cffbc` (v0.204.0 plus its
release-note correction). Fixture/report format: `evals.prompt_context` v1.

## Run and interpret

Run from a synced source checkout. The command reads configured model connections,
then builds a synthetic graph with temporary, isolated box and instance roots. It
does not start a server or reuse production conversations, plugins, persona or
stores. It makes live provider calls. Choose an OpenAI-compatible connection with
its configured key; native OAuth credentials are not copied into the temporary box.

```sh
uv run python -m evals.prompt_context \
  --config /path/to/instance/config/langgraph-config.yaml \
  --model connection:model --repeat 3 \
  --output evals/results/prompt-context.json
```

The experiment performs three identical fresh-conversation requests and then a
calculator tool loop followed by a same-thread recall question, an identical graph
reload, a synthetic post-compaction history and two hot-memory projection fixtures.
Exit 1 means a
quality check or execution failed; inspect the report rather than discarding it.
An experiment has a 180-second deadline (`--timeout` overrides it). A failed run
retains completed observations and a failure type, never raw provider error text.

`RequestProbe` sits after provider shaping in the real graph. It records effective
LangChain request hashes, character counts, tool order, message order, history
cache controls, and response usage. This is **not raw provider HTTP serialization**.
Schema sizes use compact JSON in the existing order; text/argument attribution uses
`graph.message_blocks` so mirrored tool-use arguments are counted once. The content
fingerprint is a separate shape measurement, not another additive token count.
Reports contain no prompts, schema descriptions, argument values or credentials.
Incognito calls produce no probe rows.

`cache_read_ratio = cache_read_tokens / input_tokens`, using LangChain's normalized
input total (which includes reads and writes). `non_read_input_tokens` includes
cache writes; it is not a price estimate. Missing telemetry stays null, not zero.
The configured TTL is a requested hint, not proof that a gateway honored it.
Character counts are not billing tokens. A first observed request is not necessarily
cold: provider caches may already contain a shared prefix.

## Live observations, 2026-10-14

Configured gateway `protolabs`, OpenAI-compatible protocol. Requested TTL: 1h.
Three experiments per alias, each with three identical-input repetitions and six
other scenarios. Each process uses its own temporary roots. The initial graph has
no knowledge store/skills index; core, filesystem and delegation tools are exposed.
The two projection scenarios add a synthetic plain knowledge store. These workloads
do not represent a plugin-heavy live instance. Content-free raw reports are local
ignored `evals/results/prompt-baseline-final-{fast,smart}-{1,2,3}.json` artifacts.

| Model | Calls | Input tokens | Cache-read tokens | Read / input | Quality checks |
|---|---:|---:|---:|---:|---:|
| `protolabs/fast` | 30 | 281,627 | 240,000 | 85.22% | 25/27 |
| `protolabs/smart` | 27 | 255,705 | 230,400 | 90.10% | 21/27 |

| Scenario | Fast passed | Smart passed | Fast read / input | Smart read / input |
|---|---:|---:|---:|---:|
| Identical requests (three per trial) | 9/9 | 9/9 | 82.44% | 91.35% |
| Calculator invocation + numeric answer | 3/3 | 0/3 | 82.12% | 91.30% |
| Same-thread follow-up of that result | 1/3 | 0/3 | 99.66% | 90.96% |
| Graph reload, identical request | 3/3 | 3/3 | 82.44% | 91.35% |
| Compacted-history fixture | 3/3 | 3/3 | 82.23% | 91.12% |
| Blue hot-memory fixture | 3/3 | 3/3 | 86.99% | 86.81% |
| Green hot-memory fixture | 3/3 | 3/3 | 87.66% | 87.49% |

Smart supplied the arithmetic answer without calling the requested calculator,
so both calculator and its dependent follow-up correctly fail the tool-use check.
Fast called it but failed the strict follow-up answer twice. Earlier exploratory
runs checked answer text only; the table above includes the stronger tool-use
assertion and retains every failed trial. High cache reuse is not evidence of
correct instruction following.

The first exploratory smart call reported 0 cache reads, then identical repeats
reported 7,200 / 8,757 (82.22%); that observed cold call took 1,567 ms versus 588 and
482 ms for the repeats. Later experiments were already warm. No cache purge is
available, so this is an observed cold-to-warm transition, not a controlled eviction
experiment. Both lanes report cache reads but no cache writes. Cross-model fallback
is covered structurally; native provider hit rates and fallback timing are not
established by these gateway measurements.

Scenario median latency ranges in the three-trial baseline: fast 285–631 ms;
smart 614–1,066 ms. Lanes ran concurrently, so do not attribute small latency
changes to the prompt without repeated comparison under the same arrangement.
Initial schemas were 30,259 compact-JSON characters. Schema/system hashes stayed
identical within each configuration, including graph reload. Temporary project
roots appear in the managed-project section, so different temporary configurations
legitimately have different system hashes. Compare unchanged configurations, not
unrelated roots. Memory fixture changes left the system and schemas unchanged;
adding knowledge tools caused one expected schema-set change and a one-shot tool
notice. The earlier 19,649-character sample counted only 14 core tools with a
different JSON encoding, so it is not a before/after saving.

Native Anthropic/OAuth and Codex cache rates are **not measured** by these gateway
runs: the runner does not copy production OAuth token stores into its temporary
box. Their request transforms are covered structurally. Live native lanes, provider
switch/fallback and final integrated-dev measurements remain explicit epic checks.

## Deterministic coverage and remaining live checks

`tests/test_prompt_context_eval.py` exercises the probe against actual graph builds
for minimal/full, projects/delegates, a 40-tool synthetic catalog, background fenced
calls, incognito and identical rebuilds. It also measures real middleware transforms
for all three provider shapes, projection-only changes after cached history,
deferred-schema widening and post-compaction loss of discovery messages. The fake
model verifies request structure, not provider cache hits or language-model quality.

Reuse existing characterization instead of replacing it:

| Contract | Existing evidence |
|---|---|
| Static lead/foreground subagent sizes and provider text preservation | `test_prompt_budgets.py`, `test_prompt_sections.py` |
| Shared native/external projection, incognito and attribution | `test_projection.py` |
| Concurrent turns, resume and checkpoint exclusion | `test_turn_scoped_projection.py` |
| Fresh operational state after tools | `test_working_state_refresh.py` |
| Background fences and domain-owned tools | `test_tools_subagent_only.py`, `test_turn_fence_every_pass.py` |
| Provider fallback, identity and replay | `test_provider_shape_isolation.py` |
| Cache control, TTL, history and rejected/absent telemetry | `test_prompt_cache.py` |
| Compaction and deferred discovery | `test_compaction_middleware.py`, `test_tool_deferral.py` |

The full-capability fixture doctrine is 6,411 characters: roster 3,024; operating
model 2,531; guidelines 856 (SOUL excluded). Minimal guidelines are 412. Configured
delegates add 1,623 characters. These are composer measurements, not observed live
request totals. The current room-cast suffix is after the system breakpoint but
before history: a participant change can invalidate history reuse, even when the
stable-system anchor survives. Evaluate this separately from stable prompt edits.

## Comparison thresholds fixed before behavior changes

- Zero unexplained stable-system or schema-order hash changes for identical inputs
  and configuration; zero changes to cached history caused solely by a new projected
  context. Genuine tool/config/model changes are named invalidation events.
- Zero new deterministic contract failures, lost commitments, duplicated jobs,
  unauthorized tool exposure or privacy regressions.
- Repeat live comparisons at least three times per selected model/configuration.
  Investigate a warm aggregate read-ratio loss of more than 5 percentage points,
  median non-read input increase above 5%, or median latency increase above 20%.
  These are investigation thresholds, not excuses to accept smaller regressions.
- Judge cache ratio alongside absolute input and task quality. A shorter stable
  prefix can lower the ratio while saving total input; a dynamic schema can shrink
  a request while increasing uncached work. Report both.
- Preserve every previously passing quality scenario. Repeat any new failure to
  distinguish deterministic regressions from model variance; retain failed trials.
  Do not relax a check just to turn the baseline green.
- Unknown provider telemetry remains unknown. Do not claim a cross-provider cache
  improvement from synthetic middleware tests or from one gateway alias.

## Prior work reconciled

#3184, #3190 and #3191 delivered the projection/contracts/observability foundation.
They are closed and are not implementation backlogs to recreate. #3186's closure
explicitly retains `newest`: its corrected comparison separated in favor of the
digest, about 283 tokens/turn in that measured instance, outside the cached prefix.
The session-seeding and policy-sweep harness already shipped in #3312/#3324.

#3187 shipped bounded projection and later per-chat budgets/inspector support.
Per-kind quotas and session-frozen profiles were explicitly rejected/deferred for
lack of supporting data and conflicts with per-turn freshness. This epic does not
silently reintroduce them. #4200 still owns retrieval-provenance deduplication.

Current working state intentionally refreshes **after tools execute**, while memory
and skill discovery remain turn snapshots. Preserve this exception when decoupling
operational delivery: keeping a stale task status for caching would regress a
shipped correctness fix. Fresh operational state belongs after cache boundaries.
