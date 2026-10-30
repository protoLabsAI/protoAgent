# Narrow fine-tuning spike #4659 — not now

Decision: **not now** (2026-10-29). Investigate **tool-domain routing** only;
do not train a model or change the main agent. The available evidence does not
establish equal quality, removable latency, or positive net savings. This is the
research result required by [ADR 0126 D6](../adr/0126-eval-flywheel-traces-feedback-cases.md#d6--out-of-scope),
not an authorization to implement a router.

## Candidate and observed volume

Source inspection at `48e94b0f8`:

| Candidate | Reachable frequency evidence | Evaluation boundary |
|---|---|---|
| **Tool-domain routing (chosen)** | Tool selection is part of each model round. `src/graph/middleware/tool_deferral.py` transforms the tool set once per wrapped call when enabled; deferral is OFF by default. Thus R eligible rounds imply R selection opportunities, not R separate router calls. | User intent → time, calculation, web, knowledge, or no-tool domain; audit-log tool firing gives observable labels. |
| Friction classification | Local ledger: **137 rows**, **133 harness**, **4 model**. **35 rows** have `ts` in `[2026-10-22T00:00:00Z, 2026-10-29T00:00:00Z)`. These are stored events, not model calls or unique labelled examples. | `plugins/friction/__init__.py::record_friction` accepts a kind; automatic capture also exists. There is no dedicated prompted classifier call to replace. |
| Memory extraction | `src/graph/conversation_harvest.py` calls `extract_and_store_facts` once per eligible successful harvest, only with a knowledge store and `knowledge_facts` enabled. `_default_extractor` in `src/graph/memory_facts.py` makes one model request. | Clean auxiliary-model boundary, but retirement frequency is narrower than model-round selection; semantic completeness needs human judgement. |

Routing has the broadest **code-path opportunity**, not a demonstrated fleet-wide
high call rate. There is **no separate prompted domain router today**. A small
router would add a call: savings must come from fewer exposed tool tokens or
fewer discovery rounds, not from pretending an entire main-agent call disappears.
The middleware remains deterministic and unchanged.

Langfuse numbers are **missing**. No Langfuse connector or credential environment
variables were exposed; direct, read-only inspection found the configured endpoint
and secret pair. A bounded authenticated GET of `/api/public/traces` with `limit=1`
and the seven-day UTC window above failed with `URLError` / DNS `gaierror`
in the sandbox; an authorized retry outside the sandbox returned **HTTP 403**.
The configured endpoint therefore did not grant access to the trace counts.
No trace payloads or credentials are included here. The resolved instance's
`friction/friction.jsonl` supplied the counts above; its retained timestamps span
2026-07-22 through 2026-10-30 UTC (a partial local ledger, not a fleet denominator).
The telemetry file resolves to `instance_paths().store("telemetry.db")` (not a
`telemetry/` directory), per `src/server/stores.py`. A read-only SQLite query
outside the sandbox found **3,179 retained turn-leg rows**; the same seven-day
window contains **555 rows**, **1,106 reported LLM calls**, and **12,823 reported
tool calls**. These local totals include delegated/coding-agent activity and
repeated turn legs; they are not unique turns, fleet totals, or isolated routing
calls. They support investigating routing frequency, but do not establish R.
The aggregate query was `SELECT count(*), sum(llm_calls), sum(tool_calls) FROM
turns WHERE created_at >= '2026-10-22' AND created_at < '2026-10-29'`; the
connection used `mode=ro` and `PRAGMA query_only=ON`, with no message-body export.

Operator follow-up: on the host that resolves the tracing endpoint, export counts
for that same UTC window, grouped by instance and actual provider/model. Count
eligible model-generation observations, unique turns, discovery calls, friction
records and successful fact-extraction requests separately. Deduplicate retries by
observation ID; report retries, missing traces and sampling separately. Record
input/output/cache tokens, billed cost and p50/p95 duration. Trace totals alone
are not routing volume. Report R, eligible rounds/month, with window and coverage;
join local per-turn telemetry using `trace_id` where available. Do not export
message bodies merely to count traffic.

## Held-out eval and prompted baseline

Eight new cases in `evals/tasks.json`, category `routing-spike-4659`, all explicitly
**tracked**. Existing 37 cases are unchanged. The six positive probes cover clock,
compound arithmetic, a specified page, current announcements, knowledge inventory
and a durable preference; two negatives cover a definition and a rewrite.
The page probe distinguishes retrieval from web discovery. Inventory forbids web
search. The save probe requires both a successful tool call and a persisted unique
marker, with teardown. Negative probes require zero tool attempts. No prompt names
an expected tool or instructs delegation or tool avoidance.

These are frozen, synthetic, agent-authored **evaluation-only** probes, not human
training examples or production-distribution evidence. Exclude their text, labels,
markers, paraphrases and run transcripts from every training/development split.
Freeze this file revision before baseline measurement; leave failures tracked.
Do not tune prompts/assertions after seeing results. Add a separate human-authored
blind set before making an equal-quality claim.

The existing harness measures **end-to-end prompted-agent routing behavior**, not
isolated domain-classifier accuracy. Expected tools are subset assertions; not all
extra calls are forbidden. Audit raw traces for wrong-domain extras, record a
confusion matrix including abstention, and retain the task-success check separately.
Web failures can reflect infrastructure; a save-marker match does not prove the
whole extracted preference is correct. This small suite is a screening instrument,
not sufficient evidence of statistical non-inferiority.

**Baseline: operator TODO — unmeasured.** Zero live attempts made; pass count,
pass rate, latency, tokens and cost are unknown (not zero). The configured model
gateway failed a bounded TCP probe with DNS `gaierror` in the sandbox, but the
authorized retry outside it established TCP connectivity. TCP reachability is not
a completed model-backed eval. This baseline requires a live model and an isolated,
configured agent; it cannot run in offline CI, so it remains the explicitly
permitted operator TODO. No model output has been fabricated.

On a gateway-connected host, install with `uv sync --frozen`. Prepare a disposable
box at `/tmp/pa-4659-eval` containing the current prompted model's host config and
credentials using the normal config procedure. Enable the same core tools and audit
logging. Keep scheduled/background work off so the time-windowed audit verifier
cannot attribute another turn's tools to the case. Set `BASELINE_MODEL` to the
resolved current model alias (record its actual provider revision as well). Run:

```bash
: "${BASELINE_MODEL:?set to the current configured prompted model alias}"
env -u PROTOAGENT_HOME -u PROTOAGENT_SEED_CONFIG \
  -u AUDIT_PATH -u KNOWLEDGE_DB_PATH -u MEMORY_PATH \
  PROTOAGENT_BOX_ROOT=/tmp/pa-4659-eval \
  uv run python -m evals.sweep --models "$BASELINE_MODEL" \
  --category routing-spike-4659 --repeat 8 --port-base 7990 --keep
```

Expected result: **8 cases × 8 repeats = 64 attempts** in local `evals/results/`
reports. Require all eight IDs and zero skipped cases. Record each case's passes/8
and all-attempt aggregate, not merely sweep's majority-vote headline. Preserve
model revision, config, report paths, prompt revision, temperature, cache policy,
per-turn p50/p95 latency, tokens and telemetry cost. Measure errors/timeouts as
failures and separately classify infrastructure faults. Sweep changes only its
throwaway instances, never the main agent's configured model. No alternative model
has been trained or benchmarked by this spike.

## Dataset estimate and provenance

**Available now: 0 verified eligible training examples, 0 open-model traces
retrieved, 0 independently human-authored labels collected.** The 137 friction
rows are counts only: actor/model provenance and consent have not been verified,
and they are not routing labels. The eight new evals were generated by this coding
agent and are expressly ineligible for training. Existing repository tests, evals,
assistant messages, summaries, tool arguments and promoted cases have no blanket
training clearance.

Planning estimate, not inventory or a vendor quote:

| Source of example class | Planned count | Labour assumption at $60/hour | Estimated cost |
|---|---:|---|---:|
| Maintainers independently write user intents and gold domains from the tool specifications: time, calculation, web discovery, page retrieval, knowledge, abstention (200 each) | 1,200 | 3 minutes authoring + 1 minute independent review each = 80 hours | $4,800 |
| Independent adjudication budget for disputed human examples (assume 10%) | 120 of the same examples | 2 minutes each = 4 hours | $240 |
| Consented open-model production traces, exact model/version/license and complete lineage verified, human-labelled | 0 verified; no assumed future yield | Measure collection/review time after an audit | Unknown; excluded from budget |

Total prospective human dataset: **1,200 unique examples, 84 hours, $5,040**.
Split by scenario family/author/session BEFORE augmentation: 900 train, 150 dev,
150 blind test (150/25/25 per class). This is a feasibility budget, not a claim that
900 training examples suffice. The new eight eval cases remain separately held
out. Budget scales as `N × 4 minutes × $60/hour` plus adjudication; no inference,
training compute, serving, maintenance or engineering cost is included.

Source manifest required per example: author/consent, origin, input provenance,
label author/reviewer, license, split/family, timestamps and (for traces) provider,
exact open model revision and trace ID. Reject unknown or mixed provenance,
including open-model continuations whose context contains Claude/OpenAI outputs,
teacher labels, paraphrases, synthetic inputs or summaries. Human review does not
launder those outputs. Human authors must write independently from specifications,
not edit this agent's evals or distil proprietary-model traces. Apply ADR 0126 D6's
Claude/OpenAI-output exclusion to all training inputs and targets.

## Economics and decision threshold

Let `s = current cost/eligible decision − candidate cost/eligible decision`, including
router inference, downstream tokens, retries and fallbacks (attribute each cost
once, never charge a whole turn again for each round); let F be incremental
monthly hosting/maintenance. Net monthly savings are `R × s − F`. The $5,040 human
budget alone breaks even after `5040 / (R × s − F)` months if the denominator is
positive; engineering/training expenses only increase that time.

Illustrative sensitivity, **not observed prices or traffic**, with F = $0:

| Assumed net saving per eligible decision | Decisions to recover $5,040 | At an assumed 100,000 eligible decisions/month |
|---|---:|---:|
| $0.001 | 5,040,000 | 50.4 months |
| $0.01 | 504,000 | 5.04 months |

Revisit only after the baseline and traffic TODOs are filled, provenance is cleared,
and a separately authorized comparison can demonstrate equal quality on a larger
blind human set (pre-register a non-inferiority margin and confidence interval),
with either lower fully loaded cost or lower end-to-end p95 latency. Track
abstention and wrong-domain rates individually. A prompted small open-model control
must precede attributing any gain to fine-tuning. **Current decision: not now**;
there is neither a measured quality result nor a positive measured saving.

## Environment validity before any RL consideration

No RL environment is adopted or run. If a later proposal uses this A2A/audit/KB
sandbox as an RL environment, it must first pass this protocol on a disposable,
serial instance with fresh state per attempt:

1. Audit model-visible inputs and tool responses for gold labels, grader text,
   expected tool names injected by fixtures, test-file access, report access and
   reward leakage. Keep verifier code and expected answers outside the model's
   reachable filesystem. Public held-out cases must never become training episodes.
2. Inject known bad trajectories: plausible reply without a tool call, wrong-domain
   call, failed tool reported as success, missing KB write, extra forbidden tool,
   and stale or unrelated audit records. Each must fail the corresponding oracle.
   Prove the no-tool oracle has a working audit stream with a positive control;
   missing logs must not silently qualify it.
3. Replay independently reviewed valid trajectories, alternative valid wording,
   correct KB writes, and permitted discovery-plus-execution sequences. Each must
   pass. Check delayed logs, context/session isolation, teardown and external network
   outages for false fails. Mark unresolved infrastructure failures invalid trials,
   never positive rewards. Fix the environment rather than weakening task criteria.
4. On a separate qualification set, run **8 fresh attempts per task** with the
   proposed open model and frozen settings. Require **at least 1 genuine pass in 8
   for every task**, confirmed against raw trace and end state by a human. Log all
   eight outcomes, including failures; repeated invalid environments do not qualify.
   Zero passes, any leakage or unresolved false pass/fail blocks RL. This check is
   necessary, not proof that the reward is robust enough for training.

No environment qualification result is claimed. The 64-attempt prompted baseline
above is a different measurement and cannot substitute for this validity check.
