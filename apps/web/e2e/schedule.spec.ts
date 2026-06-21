import { expect, test } from "@playwright/test";

// The Schedule tab's "New schedule" modal builds the cron/ISO `schedule` string for
// you — calendar for one-off, presets for recurring, raw cron as the escape hatch —
// with a live plain-English preview. (No hand-written cron required.)

async function gotoSchedule(page) {
  await page.goto("/app/", { waitUntil: "load" });
  // Schedule folded into the Work hub (2026-06): the right-rail Work surface, Schedule tab.
  // Work is the default-active right panel; in the narrow panel the DS responsive Tabs
  // collapse the role="tab" strip into a native <select.pl-tabs__select>.
  const workBtn = page.locator(".pl-rail--right").getByRole("button", { name: "Work", exact: true });
  const cls = (await workBtn.getAttribute("class")) ?? "";
  if (!cls.includes("--active")) await workBtn.click();
  await page.locator(".pl-tabs__select").first().selectOption("schedule");
}

async function openScheduleModal(page) {
  await gotoSchedule(page);
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
  // DropdownSelect (#274): open the trigger, then pick the portaled menu item.
  await page.locator("#schedule-freq").click();
  await page.getByRole("menuitemradio", { name: "Every weekday" }).click();
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

test("clicking a job opens a detail dialog with the FULL (un-truncated) prompt", async ({ page }) => {
  await gotoSchedule(page);
  // The row only shows a truncated prompt + delete; clicking it pops the full detail.
  await page.getByTestId("schedule-row-job-1").click();
  const detail = page.getByTestId("schedule-detail");
  await expect(detail).toBeVisible();
  // The full prompt (longer than the 80-char row truncation) is shown in full.
  await expect(page.getByTestId("schedule-detail-promptbody")).toContainText(
    "open issues for anything that needs follow-up before standup",
  );
  // Schedule (human + raw cron), timezone and id are all surfaced.
  await expect(detail).toContainText("every day at");
  await expect(detail).toContainText("0 9 * * *");
  await expect(detail).toContainText("America/Chicago");
  await expect(detail).toContainText("job-1");
});

test("the detail dialog edits prompt + schedule, gated until something changes", async ({ page }) => {
  await gotoSchedule(page);
  await page.getByTestId("schedule-row-job-1").click();
  await page.getByTestId("schedule-detail-edit").click();
  // Edit fields are pre-filled with the current values.
  await expect(page.getByTestId("schedule-detail-schedule")).toHaveValue("0 9 * * *");
  await expect(page.getByTestId("schedule-detail-prompt")).toHaveValue(/Summarize overnight activity/);
  // Save is gated until a real change.
  await expect(page.getByTestId("schedule-detail-save")).toBeDisabled();
  await page.getByTestId("schedule-detail-schedule").fill("0 8 * * 1-5");
  await expect(page.getByTestId("schedule-detail-save")).toBeEnabled();
});

test("the row delete button confirms first — Cancel keeps the job", async ({ page }) => {
  await gotoSchedule(page);
  // The row's trash no longer deletes on one click; it summons a ConfirmDialog.
  await page.getByRole("button", { name: "Delete job" }).click();
  const confirm = page.getByRole("dialog", { name: "Delete scheduled job?" });
  await expect(confirm).toBeVisible();
  // The confirm names what's being deleted (human schedule + prompt).
  await expect(confirm).toContainText("every day at");
  await expect(confirm).toContainText("Summarize overnight activity");
  // Cancel aborts — dialog closes, the job row is still there.
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(confirm).toBeHidden();
  await expect(page.getByTestId("schedule-row-job-1")).toBeVisible();
});

test("Escape aborts the delete confirm; confirming fires it", async ({ page }) => {
  await gotoSchedule(page);
  await page.getByRole("button", { name: "Delete job" }).click();
  const confirm = page.getByRole("dialog", { name: "Delete scheduled job?" });
  await expect(confirm).toBeVisible();
  await page.keyboard.press("Escape"); // click-outside / Esc cancels (no delete)
  await expect(confirm).toBeHidden();
  await expect(page.getByTestId("schedule-row-job-1")).toBeVisible();

  // Re-open and actually confirm → the dialog closes (the cancel mutation fires).
  await page.getByRole("button", { name: "Delete job" }).click();
  await confirm.getByRole("button", { name: "Delete" }).click();
  await expect(confirm).toBeHidden();
});

test("the detail dialog's Delete also routes through the confirm", async ({ page }) => {
  await gotoSchedule(page);
  await page.getByTestId("schedule-row-job-1").click();
  await page.getByTestId("schedule-detail-delete").click();
  // The detail dialog's Delete opens the same confirm (no immediate delete).
  await expect(page.getByRole("dialog", { name: "Delete scheduled job?" })).toBeVisible();
});
