# Tool disclosure and cache comparison

Epic #4216, evaluation #4222. **Keep full exposure as the default.** Deferred tools
and subagent-only ownership remain opt-in. In this synthetic 40-tool workload,
smaller deferred requests consumed more uncached input and took longer per task.
Domain ownership preserved isolation but also added cost and latency.

For operators: enable `tools.deferred.enabled` only after testing the actual
catalog and provider with discovery, warm repeats and compaction. Use
`tools.subagent_only` to establish a domain-owned execution boundary, with a
configured worker that can resolve those tools; do not assume it reduces cost.
Both settings are reversible configuration choices. This slice changes no defaults.

## Reproduce and interpret

```sh
python -m evals.tool_disclosure --config /path/to/langgraph-config.yaml \
  --model protolabs:protolabs/fast --repeat 3 \
  --output evals/results/tool-disclosure.json
```

Each policy receives the same 40 synthetic read-only catalog tools. Three graph
builds per policy each perform: first lookup, repeated lookup in the same thread,
a second record lookup, and rediscovery from a fresh synthetic compacted-history
fixture. This last case is not an actual archival/compaction lifecycle run; the
existing compaction/deferral tests cover history removal and re-exposure.
Full/deferred prompts require direct invocation and discovery if needed; the domain
arm requires the catalog worker. Quality requires the correct returned value AND
an actual current-turn invocation, including the worker invocation for domain work.

The shared request probe captures both lead and in-graph worker calls. The worker
has only `fixture_*` schemas, so its scope is labelled separately. Totals below
include both scopes, with no double-counting; median task wall time includes tool
and worker execution. There are no external worker calls in this fixture.
Requests are observed after provider shaping in LangChain, not at raw HTTP.

## Final gateway results

Configured `protolabs/fast`, requested 1h TTL, three trials per policy. Local
metadata report: `evals/results/tool-disclosure-fast-final.json`.

| Policy | Quality | Model calls (worker calls) | Total input | Cache reads | Non-read input | Median task ms |
|---|---:|---:|---:|---:|---:|---:|
| full | 12/12 | 24 (0) | 248,490 | 216,000 | 32,490 | 1,033 |
| deferred | 12/12 | 33 (0) | 144,588 | 96,800 | 47,788 | 1,328 |
| domain-owned | 12/12 | 48 (24) | 257,507 | 211,200 | 46,307 | 2,118 |

Each of the four scenarios passed 3/3 per policy. Deferred exposure cut total
input by about 42%, but raised non-read input by 47% and median task time by 29%.
Domain ownership raised non-read input by 43% and roughly doubled task time.
Cache writes were not reported; non-read input is not billed cost. Read/input
ratios were approximately 86.92%, 66.95%, and 82.02%. Median individual model calls
were 439, 380, and 412 ms; judging only that number would hide discovery/worker
round trips.

Lead schema sizes were 36,797 compact-JSON characters under full exposure;
10,730 → 12,696 → 12,893 under deferred discovery; and a stable 28,955 in the
domain arm. Worker schemas were 7,861 characters. Deferred schema changes are
expected discovery-driven prefix invalidations, not accidental ordering churn.
Compacted-history fixtures require discovery again. Full and domain lead schemas
remain stable across the workload and rebuilt graphs.

An earlier pilot offered the catalog worker in all arms and allowed delegation;
the deferred arm chose delegation instead of exercising discovery. Its reports
(`tool-disclosure-fast.json` and an invocation-verified derivative) are retained
but excluded from the comparison. The final prompts constrain the intended route,
and both lead and worker calls are accounted for. These are synthetic catalog
results on one gateway alias, not universal provider/pricing claims.

## Coverage and decision

The baseline request tests and existing `test_tool_deferral.py`,
`test_tools_subagent_only.py`, `test_provider_shape_isolation.py`, and compaction
suites cover schema widening, removal of discovery messages, rediscovery,
configuration disable/reload, provider fallback, background fences and proxy-tool
exclusion. The capability slice keeps deferred tools in graph-build guidance while
excluding held tools from direct-call prose. No per-call schema sorting or dynamic
stable-prompt regeneration was introduced.

A native Claude run remains rate-limited as recorded in the doctrine report. A
plugin-heavy production catalog or another provider may behave differently; a
future policy proposal needs that workload and total worker accounting. The
measured increase in uncached input already fails this epic's comparison threshold,
so there is no evidence to globally enable deferral or recast tools as delegated
solely for token savings. Retaining defaults completes this evaluation's scope.
