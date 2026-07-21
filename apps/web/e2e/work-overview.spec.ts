import { expect, test } from "@playwright/test";

// The Work hub's card-first overview (2026-07, no tabs): the landing is four live cards
// (Goals · Watches · Tasks · Schedule) that ARE the navigation — whole-card click-through
// to the nested panel, a "← Overview" back bar, live counts + a pulse line per card, and
// a corner "+" quick-add that opens the same creator dialog the panel uses (adding never
// navigates). Watches is the odd one out: agent-created, so no quick-add.

async function openWork(page) {
  await page.goto("/app/", { waitUntil: "load" });
  // Work is the default-active right panel — clicking its rail icon when active would
  // TOGGLE the panel closed, so only click when it's not the active surface.
  const workBtn = page.locator(".pl-rail--right").getByRole("button", { name: "Work", exact: true });
  const cls = (await workBtn.getAttribute("class")) ?? "";
  if (!cls.includes("--active")) await workBtn.click();
}

test("all four cards render with live counts and pulse lines", async ({ page }) => {
  await openWork(page);

  const goals = page.getByTestId("work-card-goals");
  await expect(goals.locator(".work-card-head .pl-badge")).toHaveText("1");
  await expect(goals.locator(".work-card-pulse")).toHaveText("1 driving · iteration 1/6");
  await expect(goals.getByText("All tests pass")).toBeVisible();

  const watches = page.getByTestId("work-card-watches");
  await expect(watches.locator(".work-card-head .pl-badge")).toHaveText("1"); // active only
  await expect(watches.locator(".work-card-pulse")).toHaveText("1 watching · 1 met today");
  await expect(watches.getByText("CI is green on main")).toBeVisible();
  // Non-active watches still list, tinted by status.
  await expect(watches.locator(".work-row", { hasText: "The staging deploy finishes" }).locator(".pl-badge")).toHaveText("met");

  const tasks = page.getByTestId("work-card-tasks");
  await expect(tasks.locator(".work-card-head .pl-badge")).toHaveText("1");
  await expect(tasks.locator(".work-card-pulse")).toHaveText("0 ready · 1 in progress");
  await expect(tasks.getByText("Wire the telemetry rollup")).toBeVisible();

  const schedule = page.getByTestId("work-card-schedule");
  await expect(schedule.locator(".work-card-head .pl-badge")).toHaveText("1");
  await expect(schedule.locator(".work-card-pulse")).toContainText("next");
  await expect(schedule.getByText("Summarize overnight activity")).toBeVisible();
});

test("a card clicks through to its panel; ← Overview (and Escape) return", async ({ page }) => {
  await openWork(page);
  await page.getByTestId("work-card-goals").click();

  // Nested view: the full Goals panel under the slim back bar.
  await expect(page.getByRole("heading", { name: "Goals" })).toBeVisible();
  await expect(page.getByTestId("work-back")).toBeVisible();

  // Back → the overview grid again.
  await page.getByTestId("work-back").click();
  await expect(page.getByTestId("work-card-goals")).toBeVisible();
  await expect(page.getByTestId("work-back")).toHaveCount(0);

  // Escape (focus inside the Work surface, no dialog open) also backs out.
  await page.getByTestId("work-card-watches").click();
  await expect(page.getByRole("heading", { name: "Watches" })).toBeVisible();
  await page.getByTestId("work-back").focus();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("work-card-watches")).toBeVisible();
});

test("Tasks quick-add opens the create dialog without navigating; creating updates the card", async ({ page }) => {
  // Stateful tasks feed for THIS page only (the shared mock server's writes are
  // generic-ok/stateless): after the POST, the list serves one more issue so the
  // invalidate → refetch visibly updates the card.
  let created = false;
  await page.route("**/api/tasks/issues", async (route) => {
    if (route.request().method() === "POST") {
      created = true;
      return route.fulfill({ json: { issue: { id: "task-9", title: "Overview quick-add", status: "open" } } });
    }
    const issues = [
      { id: "bd-1", title: "Wire the telemetry rollup", status: "in_progress", priority: 1, issue_type: "task", created_at: "2026-06-02T09:00:00Z" },
      ...(created ? [{ id: "task-9", title: "Overview quick-add", status: "open", priority: 2, issue_type: "task", created_at: "2026-07-01T09:00:00Z" }] : []),
    ];
    return route.fulfill({ json: { issues } });
  });

  await openWork(page);
  const tasks = page.getByTestId("work-card-tasks");
  await expect(tasks.locator(".work-card-head .pl-badge")).toHaveText("1");

  // The "+" is a quick-add, not a navigation: the dialog opens over the overview.
  await page.getByTestId("work-add-task").click();
  await expect(page.getByTestId("task-create-dialog")).toBeVisible();
  await expect(page.getByTestId("work-back")).toHaveCount(0); // still on the overview

  await page.getByTestId("task-create-title").fill("Overview quick-add");
  await page.getByTestId("task-create-submit").click();
  await expect(page.getByTestId("task-create-dialog")).toHaveCount(0);

  // The card updates live: count 1 → 2, and the new row appears.
  await expect(tasks.locator(".work-card-head .pl-badge")).toHaveText("2");
  await expect(tasks.getByText("Overview quick-add")).toBeVisible();
});

test("the Watches card has no quick-add (watches are agent-created)", async ({ page }) => {
  await openWork(page);
  const watches = page.getByTestId("work-card-watches");
  await expect(watches).toBeVisible();
  await expect(watches.locator(".work-card-foot")).toHaveCount(0);
  // The other cards do offer one.
  await expect(page.getByTestId("work-add-goal")).toBeVisible();
  await expect(page.getByTestId("work-add-task")).toBeVisible();
  await expect(page.getByTestId("work-add-schedule")).toBeVisible();
});

test("an empty card shows the DS Empty with the quick-add as its CTA", async ({ page }) => {
  // No active goals for THIS page → the Goals card renders its empty state.
  await page.route("**/api/goals", (route) =>
    route.fulfill({ json: { enabled: true, goals: [] } }),
  );
  await openWork(page);

  const goals = page.getByTestId("work-card-goals");
  await expect(goals.locator(".work-card-head .pl-badge")).toHaveText("0");
  await expect(goals.getByText("No active goals")).toBeVisible();

  // The Empty's action IS the quick-add — it opens the goal-creation wizard, no navigation.
  await goals.locator(".pl-empty__action").getByTestId("work-add-goal").click();
  const dialog = page.getByTestId("goal-create-dialog");
  await expect(dialog).toBeVisible();
  await expect(page.getByTestId("work-back")).toHaveCount(0);

  // The guided two-step wizard (ADR 0073) — the SAME GoalCreateDialog the Goals panel opens.
  // Step 1's Next is gated on the required condition (the first textbox).
  const next = dialog.getByRole("button", { name: "Next" });
  await expect(next).toBeDisabled();
  await dialog.getByRole("textbox").first().fill("Ship the overview");
  await expect(next).toBeEnabled();
});

test("the Goals PANEL hosts the guided goal form via its New goal header action", async ({ page }) => {
  // The Goals panel's "New goal" header action opens the guided completion-contract wizard
  // (ADR 0073) — the SAME GoalCreateDialog + goalFormPayload the Work-overview quick-add
  // opens (covered by the "empty card" spec above) and the chat `/goal new` composer form
  // renders. One creator, two hosts (mirroring TaskCreateDialog / ScheduleModal).
  let posted: Record<string, unknown> | null = null;
  await page.route("**/api/goals", async (route) => {
    if (route.request().method() === "POST") {
      posted = route.request().postDataJSON();
      return route.fulfill({ json: { ok: true, message: "goal set" } });
    }
    return route.fallback();
  });

  await openWork(page);
  await page.getByTestId("work-card-goals").click();
  await expect(page.getByRole("heading", { name: "Goals" })).toBeVisible();

  await page.getByTestId("goal-new").click();
  const form = page.getByTestId("goal-create-dialog");
  await expect(form).toBeVisible();
  // Two-step wizard (ADR 0073): step 1's "Next" is gated on the required condition (the first
  // textbox); step 2 holds the optional contract, so its "Submit" is enabled straight away.
  const next = form.getByRole("button", { name: "Next" });
  await expect(next).toBeDisabled();
  await form.getByRole("textbox").first().fill("All tests pass twice");
  await expect(next).toBeEnabled();
  await next.click();
  const submit = form.getByRole("button", { name: "Submit" });
  await expect(submit).toBeEnabled();
  await submit.click();

  // Operator goal-set payload; success closes the dialog + toasts. The verifier
  // defaults to llm when no card is picked; the contract fields are omitted when empty.
  await expect(form).toHaveCount(0);
  await expect(page.locator(".pl-toast__title", { hasText: "Goal set" })).toBeVisible();
  expect(posted).toMatchObject({
    session_id: "operator",
    condition: "All tests pass twice",
    verifier: { type: "llm" },
  });
  // Still in the nested Goals view — creating never navigates.
  await expect(page.getByTestId("work-back")).toBeVisible();
});

test("clicking a goal row opens the detail drawer (plan, contract, timeline, actions)", async ({ page }) => {
  // The Goals panel row opens a right drawer surfacing what the console couldn't see before:
  // the completion contract read-back (ADR 0073), the durable plan artifact + per-iteration
  // timeline (ADR 0079), and the lifecycle actions.
  await openWork(page);
  await page.getByTestId("work-card-goals").click();
  await expect(page.getByRole("heading", { name: "Goals" })).toBeVisible();

  await page.getByTestId("goal-row").first().click();
  const detail = page.getByTestId("goal-detail");
  await expect(detail).toBeVisible();

  // Verifier summary mirrors the backend (`command: pytest -q`).
  await expect(detail).toContainText("command: pytest -q");

  // Completion-contract read-back — outcome, constraints, boundaries, stop_when.
  const contract = page.getByTestId("goal-detail-contract");
  await expect(contract).toContainText("The suite is green on main");
  await expect(contract).toContainText("public API unchanged");
  await expect(contract).toContainText("graph/goals/");
  await expect(contract).toContainText("schema migration");

  // The `.plan.md` artifact renders (markdown or its raw-text fallback).
  await expect(detail).toContainText("Fix the no-progress streak assertion");

  // Per-iteration timeline (drive-loop history).
  await expect(page.getByTestId("goal-detail-timeline")).toContainText("2 tests still failing");

  // The fixture goal is ACTIVE → the extend action; clicking it re-arms (POST → toast).
  await page.getByTestId("goal-extend").click();
  await expect(page.locator(".pl-toast__title", { hasText: "Goal re-armed" })).toBeVisible();
});

test("watches empty state explains agent-created watches and offers no CTA", async ({ page }) => {
  await page.route("**/api/watches", (route) =>
    route.fulfill({ json: { enabled: true, watches: [] } }),
  );
  await openWork(page);

  const watches = page.getByTestId("work-card-watches");
  await expect(watches.getByText(/The agent sets watches/)).toBeVisible();
  await expect(watches.locator(".pl-empty__action")).toHaveCount(0);
});
