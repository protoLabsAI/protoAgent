import { expect, test } from "@playwright/test";

test("Activity pill opens the Work Activity section and clears its unread badge", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByTestId("activity-badge")).toBeVisible();
  await page.getByTestId("activity-widget").click();
  const work = page.locator('.work-view[data-frame="modal"]');
  await expect(work).toBeVisible();
  await expect(work.getByRole("tab", { selected: true })).toContainText("Activity");
  await expect(page.getByTestId("activity-badge")).toHaveCount(0);
  await expect(page.getByTestId("unified-feed")).toHaveCount(0);

  // Leaving the modal resumes unread tracking on the live event bus.
  await page.keyboard.press("Escape");
  await expect(work).toHaveCount(0);
  await expect(page.getByTestId("activity-badge")).toBeVisible();
});

test("Decisions pill opens the Work Decisions section", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("decisions-widget").click();
  const work = page.locator('.work-view[data-frame="modal"]');
  await expect(work).toBeVisible();
  await expect(work.getByRole("tab", { selected: true })).toContainText("Decisions");
});
