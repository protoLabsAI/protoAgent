# 0103 — Programmatic tool calls: agent tools callable from execute_code

Status: **Proposed** (umbrella #2807; spike-gated) — **amended 2026-08-18: the
bridge already existed** (see Reality check below)

## Reality check (2026-08-18, the spike's first finding)

Starting S1 surfaced that **the bridge this ADR designs already shipped** with
the `execute_code` plugin itself (bd-pe2.6, the lean-core extraction): the
child process gets a `tools` proxy object that RPCs each call back to the
parent over a loopback TCP socket gated by a per-run token, and the parent
dispatches via the real tools' own handlers. The plugin's docstring names
programmatic tool-calling as its headline use. The ADR's authors (this one
included) designed against an audit that never read the plugin — the record
stands as a lesson in checking the tree before drafting.

What this changes, decision by decision:

- **D2 is SHIPPED, in a better wire shape than proposed.** The socket-RPC +
  per-run token is strictly smaller than the proposed `POST /api/ptc/call`: no
  new authenticated HTTP surface, an ephemeral port that dies with the run, a
  token that gates the only connection. The HTTP-route design moves to
  Rejected alternatives. The remaining D2 gap is real: dispatch is direct
  `tool.ainvoke`, which bypasses the graph-level enforcement middleware and
  subagent fences — now **S4**.
- **D3 was the live gap — closed by S1.** The default bridge set was
  *everything registered* (HITL, write tools, `task` included). S1 lands the
  curated read-mostly default and the structural `_NEVER_BRIDGED` denial
  (HITL + `task`/`task_batch` + self), applied even over an explicit config
  entry.
- **D4 (schemas visible to the model) is still open** — the shipped proxy is
  name-only; the model guesses kwargs. Stays **S2**.
- **D5 (audit/trajectory/telemetry for bridged calls) is still open** — direct
  `ainvoke` bypasses the audit middleware, so bridged calls are invisible
  today. Stays **S3**.
- **The measurement** — S1's deterministic half is a repo test
  (`test_ptc_collapse_mechanics_ten_reads_one_round`: ten 5KB reads behind one
  model-visible result, output <0.1% of the intermediate bytes); the
  model-in-the-loop half is `scripts/ptc_bench.py`, run against a live
  instance.

Re-scoped slices: **S1** = D3 hardening + measurement harness (shipped with
this amendment) · **S2** = schema-visible stubs · **S3** = observability ·
**S4** = binding-path parity + GA decision.

## Context

A long agentic task spends most of its budget on the loop itself: every tool
call is a full model round-trip, and every result — needed once — re-enters the
context and rides every later call. The Endless Context work (ADR 0101) made
those rounds *cheap* (cache discipline) and *coherent* (round governance, D8),
but the round count is still the structure: an issue-sync sweep that reads 16
issues is 16 model rounds because each `github_get_issue` needs the model to
look at the result and ask for the next one.

DeepSeek Harness's "code mode" (PTC) attacks the structure: the model writes a
script that calls tools *programmatically*, and only what the script prints or
returns re-enters the model's context. Hands-on reviews single it out —
"merging ten rounds of back-and-forth tool operations into a single execution."
This is the CodeAct pattern, and protoAgent already has half of it: the
`execute_code` plugin (opt-in, subprocess engine, scrubbed env, 6k-char stdout
cap). What's missing is the bridge — our tools live in-process in the server,
unreachable from the sandbox child.

The constraint that shapes everything: ADR 0089 closed a loopback RCE by
establishing the intra-instance trust boundary. A tool bridge is precisely the
kind of hole that reopens it if done casually.

## Decision

**D1 — Opt-in, twice.** The bridge ships behind `tools.ptc.enabled` (default
off), and `execute_code` itself remains opt-in. Slice 1 additionally sits
behind a developer flag until the spike's measurements justify the surface.

**D2 — A loopback RPC scoped to one run.** At spawn, the engine mints a
single-run bearer token and passes it (plus the server's loopback URL) to the
child via env. A new `POST /api/ptc/call` accepts `{token, tool, args}`,
validates the token against the live run registry (constant-time compare,
expires with the run, one run = one token), and dispatches through the SAME
binding-layer path a model-issued call takes — the run's tool_map after
`drop_disabled_tools`, the enforcement middleware, and any active subagent
fence. A bridged call is never a second, softer path to a tool; it is the same
path with a different caller. This is the ADR 0089 posture: the token is the
capability, and nothing about being on loopback grants anything.

**D3 — Curated, read-mostly allowlist; HITL hard-denied.** Default bridgeable
set: `read_file`, `list_dir`, `find_files`, `search_files`, `fetch_url`,
`web_search`, `memory_recall`, `current_time` — read-only, already
individually capped. Operators extend via `tools.ptc.allow` (explicit names;
same shape as subagent tool lists). `ask_human` / `request_user_input` /
`run_command`-approval and every HITL tool are hard-denied for the same
structural reason subagents deny them: a LangGraph interrupt cannot park a
subprocess. Write tools are out of the default set deliberately — a script
that can write is a script whose blast radius the operator opted into by name.

**D4 — A generated stub makes the bridge legible to the model.** The sandbox
run receives a generated `protoagent_tools.py` — one typed function per
bridged tool, docstrings rendered from the live tool schemas — so the model
writes `from protoagent_tools import search_files` instead of hand-rolling
HTTP. The stub is regenerated per run from the run's actual allowlist: what's
importable IS what's callable.

**D5 — Context accounting is the point; observability keeps up.** Only the
script's stdout/return value re-enters the model's context (the existing
execute_code cap applies). Every bridged call still lands in the audit log and
the trajectory (ADR 0102) tagged `via: ptc`, counts toward per-tool telemetry
and durations, and passes each tool's own call-time caps — the intermediate
results are invisible to the MODEL, never to the operator.

**D6 — Out of scope**: nested delegation from code (`task()` stays
model-only), cross-instance calls, MCP tools in the default set (per-server
opt-in later — their caps are newer), and any streaming/interactive tool
shape.

## Slices

1. **S1 — the spike (dev-flagged)**: token mint + `/api/ptc/call` + three
   bridged tools (`read_file`, `search_files`, `fetch_url`) + a hand-written
   stub. Measure on a real multi-read task (the 16-issue sweep shape): rounds,
   tokens, wall clock vs the tool-loop baseline. The ADR graduates or dies on
   these numbers.
2. **S2 — generated stubs** from live schemas + prompt/skill guidance telling
   the model when a script beats a loop.
3. **S3 — telemetry/audit/trajectory integration** (D5 in full).
4. **S4 — curated default set + `tools.ptc.allow` + GA decision** (flag →
   `tools.ptc.enabled`).

## Rejected alternatives

- **In-process execution of model-written code** — no crash isolation,
  credentials readable in-env; ADR 0094 already rejected this shape for
  execute_code itself.
- **A separate tool-server subprocess** — a second process holding the tool
  map duplicates binding, fences, and hot-reload state; the server already owns
  all three.
- **Bridging everything by default** — the allowlist IS the security posture;
  a maximal bridge reopens ADR 0089's boundary through sheer surface.
- **MCP-in-the-middle** (expose our tools to the sandbox as an MCP server) —
  attractive symmetry, but it inherits MCP's process/session overhead per run
  and still needs the same token discipline; a scoped HTTP call is smaller.

## Consequences

A ten-read investigation becomes one round: the win compounds with ADR 0101
(fewer rounds × cheaper rounds) and D8 (fewer rounds to govern). Costs: a new
authenticated surface to maintain (mitigated by single-run tokens + the shared
dispatch path), model-written code calling tools (bounded by the read-mostly
set and per-tool caps), and stub generation as one more artifact of the run.
