# Capability honesty results

Epic #4216, slice #4219. Prompt composition now tests each named operation against
the final lead tool catalog. Partial goal/task groups, independently disabled
`task`/`task_batch`, delegates without `delegate_to`, and partial filesystem tools
have explicit regression coverage. Known empty catalogs emit no capability section.
Legacy unknown catalogs retain potential guidance with an availability qualification.

Native graph tests cover disabled delegation, held filesystem tools and deferred
schemas. External stable-prefix callers share the same composer and are compared
byte-for-byte. Existing tool-deferral and subagent-only suites verify execution and
discovery restrictions; this change does not alter either tool plane.

The full core is 3,429 characters (roster 1,612, operating model 1,099, guidelines
718). Schema descriptions/order and cache controls are unchanged from #4218.
Tool discovery does not regenerate stable prose. Repeated graph builds with the
same configuration produce identical system/schema fingerprints.

Three live gateway trials per model passed 27/27 checks each. Local metadata
reports: `evals/results/prompt-s3-{fast,smart}-{1,2,3}.json`.

| Scenario (median) | Fast input / non-read / read ratio | Smart input / non-read / read ratio |
|---|---|---|
| Warm identical request | 8,110 / 910 / 88.78% | 8,135 / 935 / 88.51% |
| Graph reload | 8,110 / 910 / 88.78% | 8,135 / 935 / 88.51% |
| Changed memory projection | 11,240 / 840 / 92.53% | 11,265 / 865 / 92.32% |

Compared with #4218, ordinary input/non-read drops another 16–17 tokens without
changing reported reads. Cache writes remain unreported. The original S1 smart
warm non-read figure (758) remains lower: #4218's documented tradeoff is still
pending final operator acceptance. There is no universal cache-improvement claim.

Warm/reload/projection median latency was 369/332/378 ms fast and 834/907/883 ms
smart. These runs overlapped the local gate, so latency is recorded but not
attributed to the small composition edit; final integrated serial comparison is
the acceptance measurement. Native live testing remains rate-limited as recorded
in the doctrine report. See the baseline for fixture and telemetry limitations.
