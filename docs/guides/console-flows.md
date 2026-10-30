# Run a console flow

Prerequisites: install the workspace dependencies with `npm ci` at the repository
root (npm 11 or newer), then run `npx playwright install chromium` from `apps/web`.
Run the commands below from `apps/web`. A successful run exits with a passing
Playwright test and writes screenshots plus a `summary.json` with `ok: true`.

A **flow** is a named, versioned Playwright script of operator-console UI steps —
"open the console, open Fleet, create a Career Coach agent, land on its page". Flows
live beside the console's end-to-end specs in `apps/web/e2e/flows/`, as
`*.flow.ts` files registered through a small `runner.ts` harness.

The same script runs two ways:

- **In CI, against the deterministic mock backend** — the `flows` Playwright project
  is part of the web E2E suite, so a selector that drifts (a renamed `data-testid`, a
  changed heading, a moved button) fails CI. That is the point of a flow: it pins the
  console walkthroughs a human would otherwise re-check by hand.
- **Live, against a running dev instance** — the same steps drive a real backend and
  capture a screenshot per step plus a machine-readable verdict, so you can confirm a
  real walkthrough end to end or attach the evidence to a board task.

Flows are deliberately separate from the ordinary `*.spec.ts` E2E specs: the base
Playwright config matches `**/*.spec.ts`, while the `flows` project matches
`flows/*.flow.ts`. A flow is a full-journey walkthrough with saved evidence, not a unit
assertion.

## Run a flow in mock mode

Mock mode is the default — no `FLOW_BASE_URL`, so the config boots
`e2e/mock-server.mjs` and the flow runs against it. From `apps/web`:

```bash
npm run build
npx playwright test --project=flows
```

`npm run build` is required: the E2E harness drives the **built** SPA from
`apps/web/dist`, not the Vite dev server. The whole web E2E suite
(`npm run test:e2e --workspace @protoagent/web`) runs the `flows` project too, so
these walkthroughs are already exercised on every console CI run.

In mock mode the Career Coach flow uses the fixed agent name `flow-cc-mock`, resets an
isolated fleet test scope, and injects the Career Coach archetype into
`/api/archetypes` via a route handler — so it needs no real archetype bundle and
creates no real agent. The live-only teardown step is skipped.

## Run a flow live against a dev instance

Point the flow at a running instance by setting `FLOW_BASE_URL`. That switches the
runner to **live** mode and tells Playwright to skip the mock server and use your URL
as the base:

```bash
FLOW_BASE_URL=http://127.0.0.1:7871 \
FLOW_TOKEN='replace-with-operator-token' \
FLOW_OUT_DIR=/tmp/flow-out \
npx playwright test --project=flows
```

Add `-g <flow>` to run a single flow by name — the test is titled `<name> v<version>`,
so `-g new-agent-careercoach` runs just that one.

The three environment variables:

| Variable | Meaning |
| --- | --- |
| `FLOW_BASE_URL` | Base URL of the target instance. Setting it is what selects **live** mode and disables the bundled mock server. Unset ⇒ mock mode. |
| `FLOW_TOKEN` | Operator bearer token. The runner seeds it into `localStorage['protoagent.authToken']` before the first step. **Required in live mode** — if it is missing the first step fails with `FLOW_TOKEN is required in live mode` before any UI action runs. The token is stripped (`[redacted]`) from any error written to the summary. |
| `FLOW_OUT_DIR` | Where evidence is written. Defaults to `test-results/flows`. Each flow gets its own subdirectory under it (see below). |

> **Point live runs at a dev instance only.** The Career Coach flow *creates a real
> agent* named `flow-cc-<timestamp>` and, in its final step, deletes exactly that one
> agent by the immutable id returned from its own create call. It tears down only what
> it made and leaves the rest of the fleet alone — but it is still mutating a real
> fleet, so use the sandbox dev instance (`:7871`, see
> [Run two instances](/guides/multi-instance)), not production.

The target must serve a built console and offer the Career Coach archetype. Cleanup
is a final step, not a `finally` hook: an earlier failure skips it and can leave the
created agent behind. Check the verdict and remove only that run's agent if needed.

## Output layout

Each run writes, under `<FLOW_OUT_DIR>/<flow-name>/`:

```
<out>/
  new-agent-careercoach/
    01-open-console.png
    02-open-fleet.png
    03-new-agent.png
    …
    summary.json
```

- **`NN-<step>.png`** — a full-page screenshot per step, numbered in order (two-digit,
  1-based) with the step name slugified (lowercased, non-alphanumerics collapsed to
  `-`). A step that fails still gets its screenshot if the page is reachable; steps
  after the first failure are skipped and produce no screenshot.
- **`summary.json`** — the machine-readable verdict. Each run clears the previous
  `summary.json` and screenshot filenames for its current steps first. Files from
  older step names or versions may remain; use a fresh `FLOW_OUT_DIR` for each run
  whose evidence you want to retain, and use the summary to identify its screenshots.

`summary.json` fields:

| Field | Meaning |
| --- | --- |
| `flow` | Flow name (a slug). |
| `version` | Flow version — bump it when the steps change. |
| `base_url` | The base URL the run drove. |
| `mode` | `"mock"` or `"live"`. |
| `started_at` | ISO-8601 start timestamp. |
| `ok` | `true` only if **every** step passed. |
| `steps[]` | One entry per step. |

Each `steps[]` entry:

| Field | Meaning |
| --- | --- |
| `name` | Step name. |
| `status` | `"pass"`, `"fail"`, or `"skipped"` (steps after the first failure). |
| `ms` | Step duration in milliseconds. |
| `screenshot` | The `NN-<step>.png` filename, or `null` if none was captured. |
| `error` | Failure message (token-redacted); omitted for passed or skipped steps. |

The first failing step stops the run — later steps stay `skipped` with `ms: 0` and no
screenshot — and `summary.json` is written either way, so a failed run still leaves the
screenshot (when capture succeeds) and error that explain it. A screenshot failure
also fails the step. Validation or output-directory setup errors before the step loop
can prevent a summary from being written.

## Run a flow from a board execution task

Because a live run produces a self-contained verdict (`summary.json`) plus screenshots,
a flow makes a good one-off, screenshot-backed check dispatched from a project board.
Create an **execution task** with `board_create_task(..., execute=true)` and an **empty
assignee**: the board hands it to a coder in a scratch git worktree checked out at the
base commit (not a feature branch), the coder runs the flow live against the target
instance, and the **deliverable is the `summary.json` verdict plus the screenshot
paths**. Execution tasks are **never self-assigned** — the board won't give an agent its
own execution card; a coder picks it up.

Point such a task at a dev instance for the same reason as any live run — it creates and
then removes a real agent. For the board delivery loop this builds on, see
[Build with a coding agent](/guides/build-with-a-coding-agent) and
[Delegate coding work](/guides/coding-agents).

## Add a flow

1. Create `apps/web/e2e/flows/<name>.flow.ts`. Call `defineFlow` with a `Flow`:

   ```ts
   import { defineFlow } from "./runner";

   defineFlow({
     name: "my-walkthrough", // a slug: lowercase, digits, single dashes
     version: 1,
     steps: [
       { name: "Open console", async run(page) { await page.goto("/app/"); /* … */ } },
       { name: "Do the thing", async run(page) { /* … */ } },
     ],
   });
   ```

   `defineFlow` registers one Playwright test titled `<name> v<version>` in the `flows`
   project and asserts the run's `ok`. The `name` must be a slug (the runner rejects
   anything else), and it becomes the output subdirectory, so keep it stable.

2. If a flow must behave differently in mock vs live (for example, create a real agent
   live but a routed stub in mock, and only tear down live), keep it as a factory that
   takes a `live` boolean and returns the `Flow` — this is how
   `careercoach.ts` adds its live-only "Delete created agent" step. The entry file
   calls `defineFlow(careerCoachFlow(!!process.env.FLOW_BASE_URL))`; `defineFlow`
   accepts the resulting flow object, not the factory.

3. **Bump `version` whenever the steps change.** The version is recorded in
   `summary.json` and in the test title, so a stored verdict always says which revision
   of the walkthrough produced it.

The flow harness and its contract tests live in `apps/web/e2e/flows/` (`runner.ts`,
`runner.spec.ts`). For the broader console build and test setup, see
[Build and test the console](/guides/build-console).
