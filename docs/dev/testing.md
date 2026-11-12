# Test policy

What gets a test in this repo, what does not, and how the limit is enforced. This page
owns the rules; [PROTO.md](../../PROTO.md) links here.

The suites more than doubled in ten weeks (5,355 → 13,291 Python tests between v0.150.0
and 2026-11-11; console test files 209 → 569). Very little of that is copy-paste — the
growth is each change adding tests at every layer and none ever being removed. The rules
below stop that at the source.

## The test for a test

Before adding one, name the **behavior a user or a caller would lose** if the code broke,
and check that no existing test already fails for it. If you cannot name the behavior, or
an existing test already catches it, do not add the test.

## What gets a test

| Change | Test |
|---|---|
| New behavior at a public seam — a tool, route, CLI verb, store API, plugin contract, wire format | Tests through that seam: the main path, each distinct failure the caller can see |
| Bug fix | **One** regression test, in the subject's existing file, named for the behavior. The issue number goes in a comment, not the file name |
| A contract others build on — config round trip, plugin API, A2A shapes | One golden per contract |
| Behavior only a real process, signal, lock or socket shows | A real-process test, marked `platform_sensitive`; keep these few |
| Refactor with no behavior change | No new tests. The existing ones passing is the evidence |
| Docs, copy, styling, config values | No tests |

## What does not get a test

- **Source-text pins.** A test that reads a source file (`?raw`, `readFileSync`,
  `read_text`, `inspect.getsource`) and matches text in it. It pins a spelling, breaks on
  every rename, and passes when the behavior is broken. Assert on rendered output or a
  return value. If the rule really is about source ("never import X here"), write a lint
  rule or an import contract.
- **Tombstones.** A test that deleted code stays deleted.
- **The same behavior at a second layer.** When a unit test covers the logic, the route or
  browser test covers the wiring: one path through, not the whole matrix again.
- **Mock echoes.** Asserting a mock was called with the arguments the test just handed
  over.
- **Private helpers, framework behavior, defaults and getters.** Test them through the
  seam that uses them.
- **One function per variant.** Cases that differ only in their inputs are one table
  (`pytest.mark.parametrize`, `it.each`).
- **A guard per extraction.** A tree-wide invariant gets one shared scanner
  (`tests/_seam_scan.py` is the model), not a new whole-suite walk per test.

## Which layer

| Layer | Use it for | Not for |
|---|---|---|
| pytest (`tests/`) | Python behavior, in-process, no network, no real sleep | Asserting on console source or plugin view markup |
| vitest (`apps/web/src`) | Console logic and component rendering | Reading source files |
| Playwright (`apps/web/e2e`) | What needs a real browser: layout, focus, scroll, drag, touch, iframes, storage, WebKit differences. One spec per user flow | Anything a vitest render can assert. The e2e suite runs against `mock-server.mjs`, so it proves nothing about the backend that vitest cannot |
| Real-process pytest | Process lifecycle guarantees | Logic that a fake at the boundary can exercise |

## Cost

- No real sleeps (`time.sleep`, `asyncio.sleep` of 0.2 s or more). Use
  `tests/_virtual_clock.py` or wait on an event. A sleep of 60 s or more that the test
  cancels is a "never returns" stub and is fine.
- No `waitForTimeout` in Playwright. Wait on the condition.
- A test that takes over a second needs a reason; over five seconds, say why in a comment.

## When you change code that has tests

Tests are part of the change. If your change makes a test redundant, delete it in the
same PR. If you add a case to a behavior that already has several near-identical tests,
fold them into a table while you are there.

## Enforcement

`python scripts/check_test_policy.py` runs with the lint checks (`scripts/gate.py`, CI's
Lint job). Limits live in `tests/test_policy_baseline.json`.

### Change budget — your diff only

The gate measures what **this change** adds, against its merge base. It never asks you to
fix tests you did not write.

| Limit | Value | Meaning |
|---|---|---|
| `free_test_lines` | 250 | Net test lines any change may add, whatever it does to source. A fix and its regression test always fit |
| `py_test_lines_per_source_line` | 2.0 | Beyond the free lines: net lines under `tests/` per net line of Python under `src/`, `plugins/`, `scripts/` |
| `web_unit_test_lines_per_source_line` | 1.5 | The same for console unit tests |
| `e2e_tests` | 5 | Net new Playwright tests |

Lines are net, so deleting or folding old tests in the same change buys room. Nine in
ten merges fit without thinking about it; the ones that do not have been adding two to
three test lines per source line.

When the gate fails it prints your numbers and how many lines to cut. Shrink first:
tables, one layer per behavior. If the change genuinely needs more — a hardening or
coverage PR with little or no source change — add the **`test-budget`** label, say why in
the PR description, and re-run the failed Lint job. The gate then reports the numbers
without failing. Locally: `TEST_POLICY_ALLOW=1`.

The repo-wide ratios and Playwright count are printed on every run as a trend. They are
not enforced: an earlier version failed on them, and blocked whichever PR arrived after
another one had spent the headroom.

### Burndowns — repo-wide, count down only

| Metric | Meaning |
|---|---|
| `web_source_pin_files` | Console test files that read source text |
| `tombstone_test_files` | `*Removed` / `*Retired` / `*Deleted` test files |
| `issue_numbered_test_files` | Test files named for an issue number |
| `py_real_sleeps` | Real sleeps in Python tests |
| `e2e_fixed_waits` | `waitForTimeout` calls |

These are grandfathered counts of shapes the policy bans. You only meet one if you add
that shape. Never raise one; the label does not waive them. After removing some, run
`python scripts/check_test_policy.py --tighten`.

Not machine-checked, and so the reviewer's job: second-layer duplicates, mock echoes,
Python tests that pin source text, and whether a new Playwright test needed a browser.
