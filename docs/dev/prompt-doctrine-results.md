# Delegation doctrine comparison

Epic #4216, slice #4218. Compare the #4217 baseline with the consolidated
instructions in `graph.prompts` and task tool descriptions. Cache middleware,
breakpoints, TTL and provider transforms are unchanged.

## Static composition

| Section | Before characters | After characters |
|---|---:|---:|
| Subagent roster | 3,024 | 1,637 |
| Operating model | 2,531 | 1,107 |
| Guidelines | 856 | 718 |
| Core total, excluding persona | 6,411 | 3,462 |
| Minimal guidelines | 412 | 340 |
| Optional collaboration | 1,623 | 809 |

The full core is 46% smaller. Live fixture schema serialization falls from 30,259
to 28,937 characters; adding knowledge tools changes these to 41,976 and 40,654.
Schemas retain their ordering. These are character measurements, not billing tokens.
Capability honesty for partial tool groups remains #4219's separate scope.

## Live gateway evidence

Same synthetic fixture, configured `protolabs` connection, requested 1h TTL,
three trials per model. Raw metadata reports remain local under
`evals/results/prompt-s2-{fast,smart}-{1,2,3}.json`. See the baseline report for
privacy and measurement limits. Both models passed 27/27 checks after the change,
including actual requested calculator invocation and its follow-up. Original
baseline checks passed 25/27 fast and 21/27 smart.

Ordinary warm requests use about 600 fewer input tokens. Fast's median non-read
input drops from 1,533 to 927, and its read ratio rises from 82.44% to 88.59%.
Smart's median non-read input rises from 758 to 951, while its read ratio falls
from 91.35% to 88.33%. Green-memory projection non-read input drops from 1,463 to
857 for fast and from 1,488 to 881 for smart. Cache writes remain unreported.

The first smart comparison also exceeded the latency investigation threshold.
A follow-up alternated before/after runs serially, reversing order for trial two,
with no local gate running. All six experiments are retained as
`prompt-s2-serial-{before,after}-{1,2,3}.json` in their respective worktrees.

| Smart scenario, median of three trials | Before input / non-read | After input / non-read | Before / after latency ms |
|---|---:|---:|---:|
| Identical request 2 | 8,757 / 757 | 8,151 / 951 | 745 / 608 |
| Identical request 3 | 8,757 / 757 | 8,151 / 951 | 635 / 593 |
| Graph reload | 8,757 / 757 | 8,151 / 951 | 593 / 583 |
| Green-memory projection | 11,887 / 1,487 | 11,281 / 881 | 792 / 745 |

Serial quality: before 23/27, after 27/27. The latency regression did not repeat;
the ordinary-request non-read regression did. Reported reads move from typically
8,000 to 7,200, despite a smaller request. This observation does not establish the
gateway's internal caching algorithm or actual billed cost. Stable bytes/order
remain identical within each unchanged configuration and across graph reload.

**Tradeoff pending final operator acceptance:** retain the clearer instruction
following and smaller request for epic testing, explicitly flagging smart's
approximately 194 additional non-read tokens per ordinary warm request. Do not
claim a universal cache improvement, pad requests to inflate the hit ratio, or
change cache boundaries without separate evidence. Final integrated measurements
must revisit this tradeoff after capability/projection changes.

A native `anthropic-oauth:claude-sonnet-5-5` attempt on 2026-10-14 reached the
provider but returned `AnthropicRateLimitError` on its first request. No native
usage or quality conclusion follows. No OAuth token store was copied or refreshed
for this experiment; the isolated resolver borrowed existing CLI credentials.

## Behavioral coverage and dev checks

The common rule now says continue independent work, yield when blocked on the
result, never poll or duplicate work, and synthesize after required fan-out results
arrive. Invocation details remain in tool schemas; returned job notices agree.
Foreground fallback without a background manager remains explicit.

Prompt tests pin that shared rule and its single home. Task-batch tests exercise
real tool invocation, exact job count, returned notices and foreground fallback.
Existing background-result tests cover one origin resume and no duplicate wake;
the full repository gate covers these unchanged runtime mechanisms. These tests
do not prove a live model will always choose the correct action. The final dev
check must exercise independent work while a job runs, blocked yield, fan-out
completion, and one resumed synthesis without duplicate jobs.
