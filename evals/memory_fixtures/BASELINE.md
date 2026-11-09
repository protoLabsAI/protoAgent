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

Every 0/3 case is flagged as a **candidate harness bug**, not a confirmed diagnosis. There are 23 PV4 candidates and three changed-fact candidates (`supersede-02`, `supersede-10`, `supersede-18`). No case, question, or assertion was edited to raise the score.

### pv4

| Case | Passes | Finding |
| --- | ---: | --- |
| `pv4-u130-q33` | 1/3 | — |
| `pv4-u130-q45` | 0/3 | Candidate harness bug |
| `pv4-u130-q53` | 1/3 | — |
| `pv4-u130-q63` | 1/3 | — |
| `pv4-u131-q122` | 3/3 | — |
| `pv4-u131-q15` | 0/3 | Candidate harness bug |
| `pv4-u131-q3` | 0/3 | Candidate harness bug |
| `pv4-u131-q76` | 0/3 | Candidate harness bug |
| `pv4-u133-q101` | 0/3 | Candidate harness bug |
| `pv4-u133-q114` | 0/3 | Candidate harness bug |
| `pv4-u133-q115` | 0/3 | Candidate harness bug |
| `pv4-u133-q131` | 0/3 | Candidate harness bug |
| `pv4-u135-q116` | 3/3 | — |
| `pv4-u135-q56` | 3/3 | — |
| `pv4-u135-q73` | 1/3 | — |
| `pv4-u135-q8` | 3/3 | — |
| `pv4-u137-q119` | 0/3 | Candidate harness bug |
| `pv4-u137-q48` | 0/3 | Candidate harness bug |
| `pv4-u137-q71` | 0/3 | Candidate harness bug |
| `pv4-u144-q111` | 1/3 | — |
| `pv4-u144-q126` | 0/3 | Candidate harness bug |
| `pv4-u144-q56` | 0/3 | Candidate harness bug |
| `pv4-u145-q41` | 3/3 | — |
| `pv4-u145-q56` | 3/3 | — |
| `pv4-u145-q87` | 3/3 | — |
| `pv4-u149-q113` | 0/3 | Candidate harness bug |
| `pv4-u149-q17` | 1/3 | — |
| `pv4-u149-q74` | 3/3 | — |
| `pv4-u151-q103` | 0/3 | Candidate harness bug |
| `pv4-u151-q60` | 0/3 | Candidate harness bug |
| `pv4-u151-q61` | 3/3 | — |
| `pv4-u152-q125` | 0/3 | Candidate harness bug |
| `pv4-u152-q45` | 0/3 | Candidate harness bug |
| `pv4-u152-q53` | 0/3 | Candidate harness bug |
| `pv4-u154-q21` | 1/3 | — |
| `pv4-u154-q3` | 0/3 | Candidate harness bug |
| `pv4-u154-q48` | 0/3 | Candidate harness bug |
| `pv4-u155-q0` | 0/3 | Candidate harness bug |
| `pv4-u155-q105` | 0/3 | Candidate harness bug |
| `pv4-u155-q138` | 2/3 | — |

### changed-fact

| Case | Passes | Finding |
| --- | ---: | --- |
| `supersede-01` | 3/3 | — |
| `supersede-02` | 0/3 | Candidate harness bug |
| `supersede-03` | 3/3 | — |
| `supersede-04` | 3/3 | — |
| `supersede-05` | 3/3 | — |
| `supersede-06` | 3/3 | — |
| `supersede-07` | 3/3 | — |
| `supersede-08` | 3/3 | — |
| `supersede-09` | 3/3 | — |
| `supersede-10` | 0/3 | Candidate harness bug |
| `supersede-11` | 3/3 | — |
| `supersede-12` | 3/3 | — |
| `supersede-13` | 3/3 | — |
| `supersede-14` | 3/3 | — |
| `supersede-15` | 3/3 | — |
| `supersede-16` | 3/3 | — |
| `supersede-17` | 3/3 | — |
| `supersede-18` | 0/3 | Candidate harness bug |
| `supersede-19` | 3/3 | — |
| `supersede-20` | 3/3 | — |

## Validation and CI

`python -m pytest tests/test_memory_fixtures_frozen.py -q`: **4 passed**. The tests exercise the checked-in files without model calls, including all twelve stores with a trivial fake embedding function. `ruff check .` and `git diff --check` pass. All 13 copied files were checked byte for byte against the supplied source. The full repository gate is left to the edit-only loop.

At PR head `181b44b1e193`, [Python CI](https://github.com/protoLabsAI/protoAgent/actions/runs/42560828216/job/128976643846) reports **3 failed, 18,129 passed, 122 skipped**. All failures are the three existing frozen-fixture tests, caused by the missing `pv4/cases.jsonl`. Windows Python (both shards), Fleet integration, A2A smoke, and zed-acp pass in that run. QA panel, protoReview, and Review at head are also failing; this change requires renewed review.

On [main at `78dd35b416144b1080e18e0a1cb9bc09621a52ae`](https://github.com/protoLabsAI/protoAgent/actions/runs/42579047472), Python, Fleet integration, A2A smoke, and zed-acp pass; both Windows Python shards were still pending when checked. The missing-fixture Python failures do not also fail on main. No unrelated CI code was changed.
