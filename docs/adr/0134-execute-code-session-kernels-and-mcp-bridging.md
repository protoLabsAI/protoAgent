# 0134 — execute_code keeps state across a session's runs, and operators can bridge MCP tools into it

- Status: Proposed
- Date: 2026-11-17
- Builds on: ADR 0103 (the tool bridge, the curated default set and per-call policy parity that this ADR keeps unchanged for every call), ADR 0098 (the process-tree lifecycle a long-lived child must join), ADR 0094 (the managed Python runtime the kernel runs on in the desktop app), ADR 0089 (the per-run token discipline, extended here to a per-kernel token), ADR 0117 (no side door: a bridged call still meets the lead's denylist and fences), ADR 0132 (incognito chats, whose kernels must die with them)

## Context

`execute_code` runs each script in a fresh child process (`plugins/execute_code/engine.py`, `run_code`). Since ADR 0103 the script can call agent tools through a loopback bridge. Two recent changes closed most of the gap with Claude Code's Code Mode: bridged calls from one script now run in parallel, up to `_MAX_PARALLEL_CALLS = 8` (#5418), and a script can return a structured value by setting `result` (#5420).

Two gaps remain.

1. **No state between runs.** Code Mode runs scripts in a REPL that stays open for the whole session. A script can store a large tool result in a variable, show the model a small preview, and a later script can filter the same variable. Our child exits after each run. To work on a large result twice, the model must either fetch it again or print it into context, and keeping it out of context is the reason the bridge exists.
2. **No MCP tools.** ADR 0103 D6 left MCP tools out of the bridge. That is where the pattern pays off most: MCP servers often return far more than the model needs. Cloudflare cut its MCP surface from about 2,600 tools (about 1.1M tokens of schema) to two, a search tool and an execute tool, and other gateways followed.

**Evidence so far.** The ADR 0103 bench (`evals/ptc_bench.py`, `--reps 3`, Sonnet 5.5, prompt caching on) measured the code lane at **0.36×** the loop lane's cost (0.53× with a warm cache), with 6 of 6 cases passing. That task is the best case for the bridge: 10 small read-only files. One public review of Code Mode on real open-source issues reported 0.71×. The same run showed that the gain in model round-trips has mostly disappeared (1.33×), because the plain loop already calls tools in parallel. Today the bridge saves **cost and context**, not round-trips, and that is the gain this ADR extends.

## Decision

**D1 — One kernel per session, behind a flag.** With `execute_code.session_state: true` (default `false`), the first `execute_code` call in a session starts a long-lived child (the *kernel*), keyed by `session_id`. Later calls in that session run their code in the kernel's existing globals, so variables, imports and functions carry over. With the flag off, nothing changes. A call can still ask for a clean slate with a new `fresh: bool = False` argument, which restarts that session's kernel before running.

**D2 — The kernel lifecycle is bounded and visible.**
- Each call keeps today's per-call `timeout`. A call that times out **kills the kernel** (state is lost), and the result says so in plain words: `kernel restarted: earlier variables are gone`.
- An idle kernel stops after `execute_code.kernel_idle_ttl` (default 600 s).
- At most `execute_code.max_kernels` (default 4) run per instance. Starting one more evicts the least recently used kernel.
- A kernel stops when its chat is deleted or cleared, and always when an incognito chat closes (ADR 0132).
- Kernels are spawned with `group_kwargs()` and registered with `track_tree` (ADR 0098), so server shutdown and the watchdog reap them like any other child tree.
- State never outlives the process: it is not checkpointed and does not survive a server restart. The tool description says this, and the first call in a new kernel tells the model `new kernel: no earlier state`.

**D3 — Policy binds to the call, not the kernel.** The kernel keeps one bridge connection, gated by a token minted for that kernel (the ADR 0089 discipline, applied to the kernel's lifetime). Each `execute_code` call registers its **own** context with the parent: the turn's `session_id`, its subagent fence and the enforcement gate. Every bridged call is checked against the context of the call that is running now, exactly as in ADR 0103 S4. A bridged call that arrives while **no** call is running (for example, from a thread a previous script left behind) is refused with `Blocked by policy: no execute_code call is active` and recorded as a failed `ptc:` audit row. Parallel calls (#5418) and `result` (#5420) work unchanged inside a kernel; the kernel clears `result` before each call so one run's value never leaks into the next run's output.

**D4 — Operators can bridge MCP tools, by name.** `execute_code.tools` and `execute_code.extra_tools` accept MCP tool names, plus one glob form, `mcp:<server>/*`, which admits every tool from that one server. The default bridge set stays the ADR 0103 curated set, with no MCP tools. `_NEVER_BRIDGED` still applies, and the late-tools seam keeps building the bridge from the denylist-final toolset (ADR 0117 D3), so an MCP tool the operator has denied cannot come back through a glob.

**D5 — Signatures on demand, not in the description.** A wide MCP bridge would blow the description's signature budget (`_MAX_SIGNATURE_LINES = 25`). Past that budget the description already lists only tool names. The kernel and the one-shot child both gain `tools.help(name)`, which returns one tool's signature and description from the live schema over the bridge. `tools.help()` with no argument lists the bridged names. The schema itself does not count as a bridged tool call, and it is not audited as one. This is the search half of Cloudflare's search-and-execute pair, without adding another model-facing tool.

**D6 — Out of scope.** Background runs with a poll tool (Code Mode's two-hour mode). Bridging tools that need approval: a pending LangGraph interrupt still cannot pause a child. Letting the model edit its own conversation history. Network or filesystem sandboxing of the kernel: it is "isolation, not a true sandbox", the same as today's child, for longer.

**D7 — GA rests on a realistic benchmark.** Before `session_state` defaults to on, a new two-lane bench (`fresh` lane: `session_state: false`; `kernel` lane: `true`) runs a task that needs state across calls. One example: list the 30 most recent issues, keep only those touching a named subsystem, then pull the comment threads of the survivors in a second step. Thresholds, registered before the first run: kernel-lane cost ≤ **0.85×** the fresh lane, and kernel-lane pass rate ≥ the fresh lane's, at 100% over `--reps 3`. A miss keeps the flag off by default and goes back to this ADR.

## Slices

1. **S1 — Kernel and lifecycle** (D1, D2) behind `session_state`, with tests for state carry-over, timeout kill and restart notice, idle TTL, the LRU cap and teardown on chat delete or clear.
2. **S2 — Per-call policy binding** (D3): the context registry, refusal between calls, `result` reset, and fence and denylist tests run inside a kernel.
3. **S3 — MCP bridging and `tools.help`** (D4, D5).
4. **S4 — The benchmark and the GA decision** (D7).

## Consequences

- **Gains.** A large result is fetched once and filtered over several calls without entering context. That is where the measured saving comes from, and the mode Code Mode's REPL is built for. MCP servers that over-return become usable through the bridge without paying their schema or their payloads in context.
- **Costs.** One more long-lived process per active session (bounded by `max_kernels`). A new failure mode: state the model expected is gone after a timeout, eviction or restart. D2's notices make the loss visible; they don't prevent it. Debugging gets harder, because one call's behaviour can depend on earlier calls. The audit trail has the bridged calls but not the code of the earlier calls that shaped them.
- **Security posture unchanged in kind, longer in duration.** The kernel runs as the server user, with that user's file and network access, for up to `kernel_idle_ttl` instead of one call. Per-call policy (D3) keeps the bridge no softer than the direct path. The token gates one connection per kernel.
- **Revisit when** the D7 bench misses its thresholds; when operators ask for runs longer than the per-call timeout (that reopens background runs plus poll); or when kernel memory shows up in instance health.
