# Native-provider and fallback validation

Epic #4216, integrated runtime through #4220. Native target:
`openai-codex:gpt-6.1-sol`, discovered through the authenticated model-list endpoint.
Compare the #4217 baseline with doctrine, capability and operational-projection
changes. No production conversations, plugins or stores were opened by the graphs.

## Authentication and fixture method

The native probe borrowed an already-live access credential in memory from the
configured desktop login. Both Codex resolver entry points were scoped to that
access-only snapshot for the experiment. No rotating refresh token was copied,
refreshed or imported, and no production credential store was modified. An expired
access credential would make the experiment unavailable. Anthropic used the normal
borrowed CLI credential path in the isolated box and returned its actual quota error.

The same `evals.prompt_context._live` scenarios and request probe ran in both
worktrees. The first comparison used a new temporary root per experiment; a second
alternating three-trial comparison reused ONE temporary box/instance root, with the
same synthetic SOUL and managed-project path across before/after processes. This
separates legitimate path/config invalidation from unchanged-prefix behavior.
The scoped local driver scripts are `/tmp/pa-prompt-native-access-probe.py` and
`/tmp/pa-prompt-native-stable-probe.py`; they contain no credentials. Native OAuth
is not automatically copied by the regular baseline CLI.

## Fixed-root comparison

Local reports: `evals/results/prompt-native-stable-{before,after}-{1,2,3}.json` in
the baseline and working-state worktrees. Each arm has three trials, ten model
calls and nine quality checks per trial. Native reported writes were zero in the
normalized usage; that is not a claim of an explicit cache-write billing interface.

| Arm | Quality | Input | Cache reads | Non-read input | Read / input | Median model-call ms |
|---|---:|---:|---:|---:|---:|---:|
| baseline | 27/27 | 222,387 | 42,112 | 180,275 | 18.94% | 2,026 |
| revised | 27/27 | 203,667 | 45,952 | 157,715 | 22.56% | 1,951 |

Total input fell 8.4%; non-read input fell 12.5%. Ordinary request input fell from
6,912 to 6,288 tokens; green-memory projection input from 9,308 to 8,684.
All request system hashes were identical within each arm across trials/rebuilds.
Schema order was stable, with the expected knowledge-tool addition for projections.

Cache reads remained intermittent: revised second identical requests reported
6,144, 6,144 and 0 reads; baseline reported 0, 5,888 and 0. Reloads and changed
projection calls did not reliably hit cache. Do not describe these data as a
uniform native cache-hit improvement or infer the backend's routing/eviction policy.

The earlier varying-root comparison also passed 27/27 each, but reported 222,487
input / 32,128 reads before and 203,777 input / 11,648 reads after. Non-read input
rose about 0.9% there despite lower total input. Those reports are retained as
`prompt-native-codex-{before,after}-{1,2,3}.json`; the inconsistent read results
prompted the fixed-root investigation rather than being discarded.

## Actual fallback and same-thread switching

`evals/results/prompt-live-provider-switch.json` records three successful assertions
in one isolated conversation:

1. Native Claude returned `AnthropicRateLimitError`; the configured gateway fallback
   `protolabs/fast` answered the fixture request.
2. A per-chat override selected native `gpt-6.1-sol`, which recalled the same value.
3. Switching back to the gateway recalled it again.

The probe observed Claude/gateway system-message delivery, Codex Responses
`instructions`, then gateway system-message delivery again. This exercises actual
provider failures and transport calls, supplementing the deterministic identity,
cache-control and encrypted-reasoning replay tests. It does not establish Claude's
warm-cache performance: that lane remains unavailable because of its quota.

## Remaining operator decision

Gateway smart's ordinary warm non-read regression from #4218 remains explicit in
`prompt-doctrine-results.md`, even though projections and this native comparison
improve. Final dev acceptance should judge instruction following and actual workload
telemetry alongside input size. The epic has no cache-boundary or default-policy
change intended to conceal that tradeoff; main stays held for operator approval.
