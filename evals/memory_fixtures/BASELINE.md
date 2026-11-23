# PV4 memory-retrieval baseline

Part of #4897. **Merge hold remains in place.**

| Field | Value |
| --- | --- |
| Date | 2026-11-09 |
| Fixture git SHA | `57ecd4f305f513061db0aaf7556c3b90ecc2ee89` |
| Baseline git SHA | `57ecd4f305f513061db0aaf7556c3b90ecc2ee89` |
| Revision | `origin/main`, containing #4930 |
| Extraction model | `anthropic-oauth:claude-haiku-5-5` |
| Answer model | `anthropic-oauth:claude-haiku-5-5` |
| Embedder | `qwen3-embedding` |
| Arm / runs | `topk` / 3 |

Fixtures were copied byte for byte from `/Users/kj/dev/pv4-run/evals/memory_fixtures/pv4/`. Scores below are computed from `/Users/kj/dev/pv4-run/baseline.json`; extraction and evaluation were completed outside the board and were not rerun for this change.

The baseline ran with a driver-side in-memory embedding cache: **9,056 distinct texts embedded, 114,436 cache hits**. The cache does not change results; see #4943.

## Store row counts

Live rows have `superseded_by = null`; all other rows are superseded audit history.

| Store | Live | Superseded | Total |
| --- | ---: | ---: | ---: |
| `user_130.jsonl` | 323 | 293 | 616 |
| `user_131.jsonl` | 328 | 367 | 695 |
| `user_133.jsonl` | 296 | 289 | 585 |
| `user_135.jsonl` | 301 | 381 | 682 |
| `user_137.jsonl` | 347 | 488 | 835 |
| `user_144.jsonl` | 285 | 325 | 610 |
| `user_145.jsonl` | 309 | 374 | 683 |
| `user_149.jsonl` | 413 | 451 | 864 |
| `user_151.jsonl` | 352 | 400 | 752 |
| `user_152.jsonl` | 366 | 397 | 763 |
| `user_154.jsonl` | 359 | 425 | 784 |
| `user_155.jsonl` | 363 | 410 | 773 |
| **Total** | **4042** | **4600** | **8642** |

## Top-k pass counts

| Suite | Run 1 | Run 2 | Run 3 | All runs |
| --- | ---: | ---: | ---: | ---: |
| pv4 | 11/40 | 12/40 | 13/40 | 36/120 |
| changed-fact | 17/20 | 17/20 | 17/20 | 51/60 |
| Overall | 28/60 | 29/60 | 30/60 | 87/180 |

## Per-case passes

The 2026-11-09 report originally flagged every 0/3 case as a candidate harness bug. Those 26 flags (23 PV4 cases and the three changed-fact cases `supersede-02`, `supersede-10`, `supersede-18`) are replaced below by their label from [TRIAGE.md](TRIAGE.md) (#4961, slice 1): **0 harness bug, 10 fixture gap, 16 retrieval miss**. Every label's fix column there reads "none, real finding". The pass counts are the unchanged 2026-11-09 numbers. No case, question, or assertion was edited to raise the score.

### pv4

| Case | Passes | Finding |
| --- | ---: | --- |
| `pv4-u130-q33` | 1/3 | — |
| `pv4-u130-q45` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u130-q53` | 1/3 | — |
| `pv4-u130-q63` | 1/3 | — |
| `pv4-u131-q122` | 3/3 | — |
| `pv4-u131-q15` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u131-q3` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u131-q76` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u133-q101` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u133-q114` | 0/3 | [Fixture gap](TRIAGE.md) |
| `pv4-u133-q115` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u133-q131` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u135-q116` | 3/3 | — |
| `pv4-u135-q56` | 3/3 | — |
| `pv4-u135-q73` | 1/3 | — |
| `pv4-u135-q8` | 3/3 | — |
| `pv4-u137-q119` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u137-q48` | 0/3 | [Fixture gap](TRIAGE.md) |
| `pv4-u137-q71` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u144-q111` | 1/3 | — |
| `pv4-u144-q126` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u144-q56` | 0/3 | [Fixture gap](TRIAGE.md) |
| `pv4-u145-q41` | 3/3 | — |
| `pv4-u145-q56` | 3/3 | — |
| `pv4-u145-q87` | 3/3 | — |
| `pv4-u149-q113` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u149-q17` | 1/3 | — |
| `pv4-u149-q74` | 3/3 | — |
| `pv4-u151-q103` | 0/3 | [Fixture gap](TRIAGE.md) |
| `pv4-u151-q60` | 0/3 | [Fixture gap](TRIAGE.md) |
| `pv4-u151-q61` | 3/3 | — |
| `pv4-u152-q125` | 0/3 | [Fixture gap](TRIAGE.md) |
| `pv4-u152-q45` | 0/3 | [Fixture gap](TRIAGE.md) |
| `pv4-u152-q53` | 0/3 | [Fixture gap](TRIAGE.md) |
| `pv4-u154-q21` | 1/3 | — |
| `pv4-u154-q3` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u154-q48` | 0/3 | [Fixture gap](TRIAGE.md) |
| `pv4-u155-q0` | 0/3 | [Fixture gap](TRIAGE.md) |
| `pv4-u155-q105` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `pv4-u155-q138` | 2/3 | — |

### changed-fact

| Case | Passes | Finding |
| --- | ---: | --- |
| `supersede-01` | 3/3 | — |
| `supersede-02` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `supersede-03` | 3/3 | — |
| `supersede-04` | 3/3 | — |
| `supersede-05` | 3/3 | — |
| `supersede-06` | 3/3 | — |
| `supersede-07` | 3/3 | — |
| `supersede-08` | 3/3 | — |
| `supersede-09` | 3/3 | — |
| `supersede-10` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `supersede-11` | 3/3 | — |
| `supersede-12` | 3/3 | — |
| `supersede-13` | 3/3 | — |
| `supersede-14` | 3/3 | — |
| `supersede-15` | 3/3 | — |
| `supersede-16` | 3/3 | — |
| `supersede-17` | 3/3 | — |
| `supersede-18` | 0/3 | [Retrieval miss](TRIAGE.md) |
| `supersede-19` | 3/3 | — |
| `supersede-20` | 3/3 | — |

## 2026-11-23 rerun decision (#4961)

**No harness fix landed, so the top-k baseline was not rerun.** [TRIAGE.md](TRIAGE.md)
(slice 1 of #4961, merged at `0c7f410d02c154f0a8c8c51ed1980169c77ac400`) records
**0 harness bugs**. It also states that `evals/memory_retrieval.py` and its tests are
unchanged. A rerun on current main would use the same harness, fixtures, models
(`anthropic-oauth:claude-haiku-5-5`) and embedder (`qwen3-embedding`) as the run above,
so it would measure only model variance. The 2026-11-09 tables above stay the
reference top-k baseline for the #4898 comparison. No new pass counts are reported
here, and none were estimated.

One caveat, also recorded in TRIAGE.md: on 2026-11-23 the configured `qwen3-embedding`
raised `OpenAIError` before returning an embedding. A future rerun, for example to
compare against the whole-store arm, needs working gateway credentials first.

## Proposed whole-store defaults (#4898)

**Proposal only.** `src/graph/config.py`, `src/graph/projection.py` and the settings
defaults are unchanged. Today's defaults are `memory_whole_store_max_rows=500` and
`memory_whole_store_weak_score=0.5`; bd-23tnm planned a 300-row cap.

| Setting | Current default | Proposed |
| --- | ---: | ---: |
| `memory.whole_store_max_rows` | 500 | **450** |
| `memory.whole_store_weak_score` | 0.5 | **0.5** (unchanged, still provisional) |

### Row cap

`_select_whole_store` counts every live, deliverable private row plus every eligible
shared-profile row. It falls back to top-k as soon as that count exceeds the cap
(`live_count > whole_store_max_rows`), so a store is covered when its count is at most
the cap. Live means `superseded_by` and `invalidated_at` are both null. In these
fixtures every live row has `delivery: retrieved`, so all of them would be offered to
the selector.

| Store | Live rows | Live content chars | Covered at 300 | Covered at 450 | Covered at 500 |
| --- | ---: | ---: | :---: | :---: | :---: |
| `user_144.jsonl` | 285 | 28,842 | yes | yes | yes |
| `user_133.jsonl` | 296 | 28,854 | yes | yes | yes |
| `user_135.jsonl` | 301 | 34,098 | no | yes | yes |
| `user_145.jsonl` | 309 | 32,309 | no | yes | yes |
| `user_130.jsonl` | 323 | 34,169 | no | yes | yes |
| `user_131.jsonl` | 328 | 33,076 | no | yes | yes |
| `user_137.jsonl` | 347 | 34,447 | no | yes | yes |
| `user_151.jsonl` | 352 | 36,143 | no | yes | yes |
| `user_154.jsonl` | 359 | 35,307 | no | yes | yes |
| `user_155.jsonl` | 363 | 36,379 | no | yes | yes |
| `user_152.jsonl` | 366 | 35,936 | no | yes | yes |
| `user_149.jsonl` | 413 | 40,407 | no | yes | yes |
| **Stores covered** | | | **2 / 12** | **12 / 12** | **12 / 12** |

Changed-fact cases seed one more live row into a copy of `user_130` (617 superseded
by 618), which gives 324 live rows. That copy is covered by any cap of 324 or more.

Reasoning:

- **300 is too low for a fair comparison.** The selector could run on only 2 of 12
  stores (`user_144`, `user_133`). On the other 10 it would silently fall back to
  top-k, so a whole-store run would mostly re-measure the top-k arm. Those 10 stores
  hold 17 of the 23 never-passing PV4 cases. The three never-passing changed-fact
  cases run on the 324-row `user_130` copy, which is also over 300.
- **450 covers every fixture store with about 9% headroom over the largest (413).**
  That leaves room for a few weeks of growth in a real store of this size, and for a
  small shared profile, which counts toward the same cap.
- **450 rather than 500 keeps the selector prompt bounded.** Live rows average about
  100 characters. The largest store is about 40k characters of content, roughly 10k
  tokens before ids and framing. At 450 rows the worst case is about 45k characters,
  which a cheap select model handles in one call. Going past that adds cost for stores
  that no fixture represents.
- If a real deployment regularly exceeds 450 live rows, it is outside what this
  baseline measured. That calls for a new measurement, not a bigger default.

### Weak-score threshold

The gate compares the best **cosine** similarity of the top-k hits
(`MemoryStore.best_similarity`) with `whole_store_weak_score`. A missing query
vector, or no hits, scores -1, which is always weak.

**No cosine scores are available to calibrate this value.** Slice 1's retrieval
probe ([TRIAGE.md](TRIAGE.md), [receipts](TRIAGE-evidence.json)) ran the FTS-only
fallback because `qwen3-embedding` was unavailable. It recorded BM25 scores, which use
a different scale (lower is better) and cannot stand in for a cosine threshold. The
2026-11-09 `baseline.json` that would hold Qwen retrieval data is absent, and no rerun
was done (see above). The `config.py` comment already marks 0.5 as provisional after
the 2026-11-11 attempt failed to authenticate.

Proposal: **keep 0.5** until a Qwen-backed run records `best_similarity` for every
case. Reasons:

- The 16 retrieval-miss cases (13 PV4, three changed-fact) are where whole-store
  selection should help. In 15 of them at least one supporting live row was missing
  from the FTS top five. In `supersede-10`, FTS returned the new row first even though
  Qwen top-k failed historically. Without their cosine scores, any value other than
  0.5 is a guess.
- 0.5 sits mid-range for normalized embedders. Moving it either way without data
  risks the opposite failure: firing on most turns (selector cost on every turn), or
  almost never firing (the whole-store arm equals top-k).
- To calibrate, log `best_similarity` for each case in a Qwen run. Then put the
  threshold just above the scores of the retrieval-miss cases and below the scores
  of the 3/3 cases (`pv4-u131-q122`, `pv4-u135-q116`, …). Fixture-gap cases should not
  drive it, because no selection can recover a fact the store does not hold.

## Validation and CI

`python -m pytest tests/test_memory_fixtures_frozen.py -q`: **4 passed**. The tests exercise the checked-in files without model calls, including all twelve stores with a trivial fake embedding function. `ruff check .` and `git diff --check` pass. All 13 copied files were checked byte for byte against the supplied source. The full repository gate is left to the edit-only loop.

At PR head `181b44b1e193`, [Python CI](https://github.com/protoLabsAI/protoAgent/actions/runs/42560828216/job/128976643846) reports **3 failed, 18,129 passed, 122 skipped**. All failures are the three existing frozen-fixture tests, caused by the missing `pv4/cases.jsonl`. Windows Python (both shards), Fleet integration, A2A smoke, and zed-acp pass in that run. QA panel, protoReview, and Review at head are also failing; this change requires renewed review.

On [main at `78dd35b416144b1080e18e0a1cb9bc09621a52ae`](https://github.com/protoLabsAI/protoAgent/actions/runs/42579047472), Python, Fleet integration, A2A smoke, and zed-acp pass; both Windows Python shards were still pending when checked. The missing-fixture Python failures do not also fail on main. No unrelated CI code was changed.
