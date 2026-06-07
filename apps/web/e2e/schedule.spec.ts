import { expect, test } from "@playwright/test";

// The Schedule tab's "New schedule" modal builds the cron/ISO `schedule` string for
// you — calendar for one-off, presets for recurring, raw cron as the escape hatch —
// with a live plain-English preview. (No hand-written cron required.)

async function openScheduleModal(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  await page.getByTestId("schedule-new").click();
  await expect(page.getByTestId("schedule-modal")).toBeVisible();
}

test("modal opens on Once with a calendar input", async ({ page }) => {
  await openScheduleModal(page);
  await expect(page.getByTestId("schedule-once")).toBeVisible();
  await expect(page.getByTestId("schedule-once")).toHaveAttribute("type", "datetime-local");
});

test("repeat presets build cron with a plain-English preview", async ({ page }) => {
  await openScheduleModal(page);
  await page.getByRole("tab", { name: "Repeat" }).click();
  // default daily 09:00 → preview describes it + shows the cron
  const preview = page.getByTestId("schedule-preview");
  await expect(preview).toContainText("every day at");
  await expect(preview.locator("code")).toContainText("0 9 * * *");
  // switch to weekdays
  await page.getByTestId("schedule-freq").selectOption("weekdays");
  await expect(preview).toContainText("every weekday at");
  await expect(preview.locator("code")).toContainText("* * 1-5");
});

test("cron mode describes a raw expression", async ({ page }) => {
  await openScheduleModal(page);
  await page.getByRole("tab", { name: "Cron" }).click();
  await page.getByTestId("schedule-cron").fill("0 9 * * 1-5");
  await expect(page.getByTestId("schedule-preview")).toContainText("every weekday at");
});

test("submit is gated until prompt + schedule are set", async ({ page }) => {
  await openScheduleModal(page);
  await page.getByTestId("schedule-once").fill("2099-01-01T09:00");
  await expect(page.getByTestId("schedule-submit")).toBeDisabled(); // no prompt yet
  await page.getByTestId("schedule-prompt").fill("Run the daily brief");
  await expect(page.getByTestId("schedule-submit")).toBeEnabled();
});
