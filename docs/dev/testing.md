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
Lint job) and compares the tree to ceilings in `tests/test_policy_baseline.json`.

| Metric | Kind | Meaning |
|---|---|---|
| `py_test_lines_per_source_line` | budget | Lines under `tests/` per line of Python under `src/`, `plugins/`, `scripts/` |
| `web_unit_test_lines_per_source_line` | budget | Console test lines per console source line |
| `web_e2e_tests` | budget | Playwright test count |
| `web_source_pin_files` | burndown | Console test files that read source text |
| `tombstone_test_files` | burndown | `*Removed` / `*Retired` / `*Deleted` test files |
| `issue_numbered_test_files` | burndown | Test files named for an issue number |
| `py_real_sleeps` | burndown | Real sleeps in Python tests |
| `e2e_fixed_waits` | burndown | `waitForTimeout` calls |

**Budgets** let tests grow with the code, not faster. When one fails, the fix is to
shrink: tables, one layer per behavior, delete what your change made redundant. Raising
a budget is a one-line diff in the baseline file and needs a sentence in the PR
description saying why the new tests could not replace old ones.

**Burndowns** are grandfathered counts of shapes the policy bans. They only go down.
Never raise one.

After a PR that removes tests, run `python scripts/check_test_policy.py --tighten` to
lock the lower numbers in. It never raises a ceiling.

Not machine-checked, and so the reviewer's job: second-layer duplicates, mock echoes,
Python tests that pin source text, and whether a new Playwright test needed a browser.
