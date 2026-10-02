import { expect, test } from "@playwright/test";

// A goal must visibly GO GREEN. The Work ▸ Goals card used to drop from "1 driving" straight
// to "No active goals" the moment the verifier passed, so the success was invisible. Now the
// finished goal stays under "Recent" with a success state (green check + "achieved ·
// <verifier> · <when>"), and the flip happens LIVE off the `goal.changed` bus push — no reload.
//
// The spec owns `/api/goals` (page.route) and flips it from driving to achieved; the mock's
// event stream repeats `goal.changed` for the `x-e2e-goal-changed` session so the console
// refetches on its own.

test.use({ extraHTTPHeaders: { "x-e2e-goal-changed": "goal-tab" } });

async function openWork(page) {
  await page.goto("/app/", { waitUntil: "load" });
  const workBtn = page.locator(".pl-rail--right").getByRole("button", { name: "Work", exact: true });
  const cls = (await workBtn.getAttribute("class")) ?? "";
  if (!cls.includes("--active")) await workBtn.click();
}

test("a goal transitions driving → achieved and the card turns green live", async ({ page }) => {
  let achieved = false;
  const base = {
    session_id: "goal-tab",
    condition: "Make the test suite pass",
    verifier: { type: "command", command: "pytest -q" },
    max_iterations: 8,
    started_at: Date.now() / 1000 - 120,
  };
  await page.route("**/api/goals", (route) =>
    route.fulfill({
      json: {
        enabled: true,
        goals: [
          achieved
            ? {
                ...base,
                status: "achieved",
                iteration: 1,
                finished_at: Date.now() / 1000 - 5,
                last_reason: "command exited 0",
              }
            : { ...base, status: "active", iteration: 1 },
        ],
      },
    }),
  );

  await openWork(page);
  const card = page.getByTestId("work-card-goals");

  // Driving: counted as active — never a false "No active goals".
  await expect(card.locator(".work-card-head .pl-badge")).toHaveText("1");
  await expect(card.locator(".work-card-pulse")).toHaveText("1 driving · iteration 1/8");
  await expect(card.getByTestId("work-goal-active")).toContainText("Make the test suite pass");
  await expect(card).not.toContainText("No active goals");

  // The verifier passes server-side; the next `goal.changed` push refetches — no reload.
  achieved = true;

  const row = card.getByTestId("work-goal-recent");
  await expect(row).toHaveAttribute("data-status", "achieved");
  await expect(row).toContainText("Make the test suite pass");
  await expect(row).toContainText("achieved · command: pytest -q · just now");
  await expect(card.getByTestId("work-goal-active")).toHaveCount(0);
  await expect(card.locator(".work-card-pulse")).toHaveText("1 achieved recently");
  await expect(card).not.toContainText("No active goals");
  // Green: the success token, not a muted/neutral row.
  const color = await row.locator(".work-row-icon").evaluate((el) => getComputedStyle(el).color);
  const success = await row.evaluate((el) =>
    getComputedStyle(el).getPropertyValue("--pl-color-status-success").trim(),
  );
  expect(success).not.toBe("");
  expect(color).not.toBe(await card.locator(".work-card-title").evaluate((el) => getComputedStyle(el).color));

  // Dismiss hides it without navigating into the panel.
  await row.getByRole("button", { name: /Dismiss finished goal/ }).click();
  await expect(card.getByTestId("work-goal-recent")).toHaveCount(0);
  await expect(page.getByTestId("work-back")).toHaveCount(0);
});
