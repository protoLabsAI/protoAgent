# PV4 memory-retrieval baseline

> **STATUS: BLOCKED — dataset fetch failed; no baseline generated.** The explicit
> Haiku 5.5 extraction override passed a live extractor preflight. The builder then
> failed fetching PV4, before any fixture extraction. The missing-fixture review
> finding remains unresolved; no pass counts or stores have been fabricated.

| Field | Value |
| --- | --- |
| Date | 2026-11-08 (America/Los_Angeles; 2026-11-09 UTC) |
| Git sha before local retry/logging fixes | `2db29f8522338d29a36977cf037e5d419774e37b` |
| Extraction model (explicit, resolved) | `anthropic-oauth:claude-haiku-5-5` |
| Main model | `anthropic-oauth:claude-sonnet-5-5` |
| Answer model (requested; evaluation not run) | `anthropic-oauth:claude-haiku-5-5` |
| Embedder (configured; evaluation not run) | `qwen3-embedding` |
| Requested cohort | 12 users, seed 125, default selection from IDs 126–155 |
| Requested cases | 40 PV4 + 20 changed-fact |
| Generated fixtures / baseline results | None |

## Latest retry (review round 3)

Retried on 2026-11-09 UTC (2026-11-08 America/Los_Angeles), from
`4d93af65a4ba036a5afc4e423af5b58a9e6ac64e`, with the same explicit command below.
Both invocations logged `Extraction model: anthropic-oauth:claude-haiku-5-5`
before fetching. A read-only configuration check confirmed the main model is
`anthropic-oauth:claude-sonnet-5-5`; no instance configuration was changed.

The first invocation ended with `TimeoutError: The read operation timed out`.
The second exhausted all three HTTP attempts at offset 0:

```text
Extraction model: anthropic-oauth:claude-haiku-5-5
PV4 offset 0: HTTP 502; retrying
PV4 offset 0: HTTP 502; retrying
urllib.error.HTTPError: HTTP Error 502: Bad Gateway
```

A separate one-row HEAD probe also returned HTTP 502. Neither builder invocation
reached extraction or created fixtures, so the missing-fixture blocker remains
unresolved and the three-run baseline could not be collected. Recovery remains
the builder and runner commands below after dataset service access recovers.

Focused validation on this head: **27 passed, 3 failed** across
`test_memory_fixtures_pv4.py`, `test_memory_fixtures_frozen.py`, and
`test_memory_retrieval_eval.py`. All failures are the absent frozen fixtures;
no tests, questions, or assertions were changed.

The local full gate passed lint, import contracts, attribution, and lockfile
checks with `UV_CACHE_DIR="$PWD/.uv-cache"`. Its pytest phase was interrupted
after the dataset blocker was confirmed: **42 failed, 2,779 passed, 76 skipped**.
Failures included sandbox-denied loopback socket binding (`PermissionError:
[Errno 1] Operation not permitted`); this interrupted run is not a successful
gate or evidence of matching failures on main. No unrelated code was changed.

[PR Python CI at this head](https://github.com/protoLabsAI/protoAgent/actions/runs/42544239894/job/128925663168)
likewise reports **3 failed, 17,922 passed, 118 skipped**: exactly the three
frozen-fixture tests. Windows Python (both shards), Fleet integration, A2A smoke,
and zed-acp pass in that workflow. On
[main at `7219cdbbf16e`](https://github.com/protoLabsAI/protoAgent/actions/runs/42548788589),
Python, both Windows Python shards, Fleet integration, A2A smoke, and zed-acp
have all completed successfully. The fixture failures therefore do not also
fail on main; there are no unrelated failing CI suites to repair.

## Previous run evidence

The preflight loaded the instance configuration, assigned the explicit override
in memory, and checked `_resolve_aux_model(config, "")` was nonempty and distinct
from `config.model_name`. Calling the runtime `_default_extractor` on a synthetic
tea preference returned a structured fact successfully. No instance config was
edited. The earlier blank-aux-model block is therefore resolved.

The builder was invoked using the worktree's `.venv/bin/python`:

```sh
python -u -m evals.memory_fixtures.build_pv4 --yes --model anthropic-oauth:claude-haiku-5-5
```

The first invocation failed with `urllib.error.HTTPError: HTTP Error 429: Too Many
Requests`. A subsequent request to the same endpoint succeeded, but the second
builder invocation failed with `urllib.error.HTTPError: HTTP Error 502: Bad Gateway`.
After adding bounded retries for transient HTTP errors, the final invocation logged:

```text
Extraction model: anthropic-oauth:claude-haiku-5-5
PV4 offset 0: HTTP 503; retrying
PV4 offset 0: HTTP 503; retrying
urllib.error.HTTPError: HTTP Error 503: Service Temporarily Unavailable
```

The failing endpoint was `https://datasets-server.huggingface.co/rows`, with
`dataset=memorilla/PersonalizationV4`, `config=default`, `split=test`, `offset=0`,
and `length=100`. All three attempts failed; delays were 10 and 20 seconds.
The builder stopped before creating the output directory. There are no per-store
row counts, per-run scores, per-case scores, or zero-of-three findings to report.

## Resume after dataset access recovers

Run the builder command above. It prints the resolved extraction model before
fetching; confirm it is `anthropic-oauth:claude-haiku-5-5`. Keep the explicit override
and leave instance configuration unchanged. Review all twelve generated stores,
then run:

```sh
python -m evals.memory_retrieval --arms topk --runs 3 --model anthropic-oauth:claude-haiku-5-5 --json memory-retrieval-pv4-baseline.json
```

Replace this report with the actual per-store live/superseded row counts, per-run
PV4/changed-fact/overall pass counts, and each case's pass count out of three. Flag
every zero-of-three case as a candidate harness bug without modifying its question
or assertions. The runner writes its report under `evals/results/`.

## Previous validation and CI comparison

The builder/runner tests, including bounded retry and model-log coverage, pass:
**27 passed**. The three frozen-fixture tests still fail because the requested
artifacts could not be generated; their assertions are preserved.

`python scripts/gate.py` passed lint, import contracts, attribution, and lockfile
checks. Its full pytest phase was interrupted after 4,143 passed and 78 skipped
once the dataset block and focused missing-fixture failures were confirmed; this
is not a successful full gate. Raw build, gate, and PR Python CI logs are retained
locally in `evals/results/pv4-attempt-20261108/` (ignored run artifacts).

At PR head `2db29f852233`, [Python CI](https://github.com/protoLabsAI/protoAgent/actions/runs/42542985496/job/128921819644)
reports **3 failed, 17,916 passed, 118 skipped**. All three failures are in
`tests/test_memory_fixtures_frozen.py`, caused by the absent `pv4/cases.jsonl`.
Windows Python (both shards), Fleet integration, A2A smoke, and zed-acp pass in
that same workflow. These are not currently unrelated failing suites.

[Main checks](https://github.com/protoLabsAI/protoAgent/actions/runs/42539637173)
at `e162a027b16c77d97af9f8f1b2367405321f4b40` pass, including Python, both Windows
Python shards, Fleet, A2A smoke, and zed-acp. No unrelated code was changed.
