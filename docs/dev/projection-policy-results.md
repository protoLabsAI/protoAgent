# Projection policy evaluation

Epic #4216, evaluation #4221. **Retain the existing defaults:** newest-session
recall, instance-wide commitments, and the 16,000-character floor. No recall or
budget policy change ships in this slice.

## Prior decisions and scope

#3186 closed after corrected seeded-session evaluation retained `newest`; the
observed digest was about 283 tokens/turn, not its maximum 2,000-token allowance.
#3312/#3324 already fixed the seeding/policy harness. #3187 delivered bounded
projection and explicitly deferred per-kind quotas and session-frozen profiles.
This evaluation reuses shipped fixtures and tests, rather than reopening that work.
#4200 still owns retrieval-provenance deduplication outside this epic.

## Current continuity comparison

Reproduce with an isolated fixture graph and configured gateway connection:

```sh
python -m evals.prompt_policy --config /path/to/langgraph-config.yaml \
  --model protolabs:protolabs/fast --repeat 3 \
  --output evals/results/prompt-policy.json
```

The runner seeds the five shipped continuity scenarios before graph construction,
then tests `newest`, `relevant`, and `off`, each three times. It reuses their
expected/forbidden patterns and grading rubrics. The same model grades rubric
questions; this is an automated rubric score, not independent human verification.
All synthetic answers are retained for inspection. Grader calls are excluded from
agent-call metrics. No production stores, persona or plugins are reused.

| Policy | Passed checks | Agent calls | Total input | Cache reads | Non-read input | Median call latency ms |
|---|---:|---:|---:|---:|---:|---:|
| newest | 9/15 | 22 | 261,848 | 229,600 | 32,248 | 1,061 |
| relevant | 9/15 | 24 | 285,968 | 252,800 | 33,168 | 807 |
| off | 4/15 | 23 | 254,362 | 239,200 | 15,162 | 638 |

| Scenario | newest / 3 | relevant / 3 | off / 3 |
|---|---:|---:|---:|
| Named earlier decision, including attribution | 0 | 0 | 0 |
| Vague recall of recent topics | 1 | 0 | 0 |
| Changed deployment fact | 2 | 3 | 0 |
| Unrelated recent sessions | 3 | 3 | 3 |
| Adjacent-but-absent decision / abstention | 3 | 3 | 1 |

Local artifact: `evals/results/prompt-policy-fast-final.json`. An incomplete pilot
(`prompt-policy-fast.json`) hit a 30-step graph ceiling; it is retained but excluded
from this table. The final run uses the baseline harness's 100-step ceiling.

`Off` saves uncached input but loses continuity quality. `Relevant` does not improve
overall quality or non-read input; its zero-match/empty-query fallback deliberately
retains newest sessions, which explains why it is not a universal noise filter.
Some failures returned tool-call-looking text without executing a tool; others gave
the right number without the required earlier-session attribution. Do not silently
relax these checks. The synthetic corpus and one model are insufficient evidence
for a global default change. A future model-specific proposal needs representative
operator workloads and a stronger independent quality review.

Median projected text was 2,313 characters for both `newest` and `relevant`, and
zero for `off`; these full-question queries did not produce a smaller relevance
projection. Their projected-content hashes were identical, as were stable-system
and schema hashes across all three policies. Thus the small quality/call-count
difference between newest and relevant here is sampling variance, not evidence
that a different selected context helped. The search converts a full question to
literal AND terms; zero matches invoke the documented newest fallback.
The FTS fallback is covered separately with matching short queries in
`test_digest_gating.py`. The observed digest layer is not its configured maximum.

Cache writes were unreported. Read/input ratios were 87.68%, 88.40%, and 94.04%.
The highest ratio is the weakest continuity policy here. Compared with S1's simple
hot-memory fixture, these calls include prior-session tools and multiple turns;
the table is a within-workload policy comparison, not a claim that policy alone
caused the difference from S1. Call latency also overlaps local validation load.

## Budgets and commitment scope

Existing budget tests cover shedding order, protected state/hot memory, skill
identity floors, missing model metadata and per-chat overrides. A direct composer
probe produced these configured ceilings at the default 8%:

| Window tokens | Projection character ceiling | Skill-index character ceiling |
|---|---:|---:|
| 8,000 | 16,000 | 640 |
| 32,000 | 16,000 | 2,560 |
| 128,000 | 40,960 | 10,240 |
| unknown | unbounded | 8,192 |

A synthetic 6,031-character working-state block plus 5,908 characters of always-on
memory rendered 12,595 characters including trust framing. Both a 16,000 ceiling
and a proposed proportional 2,560 ceiling retained it intact: the latter emitted
the existing never-shed warning. Lowering the floor alone therefore cannot enforce
a hard small-window limit without deciding which required commitments/facts to lose.
`budget.overflow` lists shed sections, not a boolean indicating used > ceiling;
it was empty because these sections cannot be shed. Unknown window metadata remains
explicitly unbounded, not a fabricated token estimate.

Goals/plans are session-selected; open tasks, watches, schedules and plugin queues
are instance commitments. A task's session id marks its origin, not permission to
hide it elsewhere. The added two-session regression retains an unscoped global task
and both project tasks while marking only the current session's task. Blind session
filtering would drop commitments without an explicit ownership/scope contract.
Retain current scope; any future narrowing needs that contract and operator examples.

Existing digest/projection tests cover own-session exclusion, goal turns, incognito,
trust framing, and stable turn snapshots. Working state still refreshes after tools;
projection stays after cache boundaries. Room-participant suffix changes can still
invalidate history reuse; this evaluation makes no participant-placement change.
No further policy implementation is required to close this evaluation slice.
