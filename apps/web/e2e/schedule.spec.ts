import { expect, test } from "@playwright/test";

// The Schedule tab's "New schedule" modal builds the cron/ISO `schedule` string for
// you — calendar for one-off, presets for recurring, raw cron as the escape hatch —
// with a live plain-English preview. (No hand-written cron required.)

async function gotoSchedule(page) {
  await page.goto("/app/", { waitUntil: "load" });
  // Schedule lives in the Work hub (right rail). Card-first navigation (2026-07, no
  // tabs): the overview's Schedule card clicks through to the panel.
  const workBtn = page.locator(".pl-rail--right").getByRole("button", { name: "Work", exact: true });
  const cls = (await workBtn.getAttribute("class")) ?? "";
  if (!cls.includes("--active")) await workBtn.click();
  await page.getByTestId("work-card-schedule").click();
}

async function openScheduleModal(page) {
  await gotoSchedule(page);
  await page.getByTestId("schedule-new").click();
  await expect(page.getByTestId("schedule-modal")).toBeVisible();
}

test("modal opens on Once with a date field, time field, and inline calendar", async ({ page }) => {
  await openScheduleModal(page);
  // #2159: the bare datetime-local is now a date input + time input + an inline month grid.
  await expect(page.getByTestId("schedule-once-date")).toHaveAttribute("type", "date");
  await expect(page.getByTestId("schedule-once-time")).toHaveAttribute("type", "time");
  await expect(page.locator(".cal")).toBeVisible();
  // Clicking a day in the calendar fills the date field.
  await page.locator(".cal-day:not(.cal-out)").first().click();
  await expect(page.getByTestId("schedule-once-date")).not.toHaveValue("");
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
  // Range validation, not just a field count: minute 60 is five fields but never valid.
  await page.getByTestId("schedule-cron").fill("60 9 * * *");
  await expect(page.getByTestId("schedule-error")).toContainText("out of range");
  await page.getByTestId("schedule-prompt").fill("Run it");
  await expect(page.getByTestId("schedule-submit")).toBeDisabled();
});

test("submit is gated until prompt + schedule are set", async ({ page }) => {
  await openScheduleModal(page);
  await page.getByTestId("schedule-once-date").fill("2099-01-01");
  await page.getByTestId("schedule-once-time").fill("09:00");
  await expect(page.getByTestId("schedule-submit")).toBeDisabled(); // no prompt yet
  await page.getByTestId("schedule-prompt").fill("Run the daily brief");
  await expect(page.getByTestId("schedule-submit")).toBeEnabled();
});

test("a one-off in the past is flagged and blocks submit (#2159)", async ({ page }) => {
  await openScheduleModal(page);
  await page.getByTestId("schedule-once-date").fill("2020-01-01");
  await page.getByTestId("schedule-once-time").fill("09:00");
  await page.getByTestId("schedule-prompt").fill("this would never fire");
  // The preview banner shows a red error and submit stays disabled — the one-off that
  // silently never fires is caught before it's created.
  await expect(page.getByTestId("schedule-error")).toContainText("in the past");
  await expect(page.getByTestId("schedule-submit")).toBeDisabled();
});

test("a one-off with no time is refused, not quietly scheduled for 09:00 (#2159)", async ({ page }) => {
  await openScheduleModal(page);
  await page.getByTestId("schedule-once-date").fill("2099-01-01");
  await page.getByTestId("schedule-once-time").fill("23:59");
  await page.getByTestId("schedule-prompt").fill("run late");
  // A complete pair builds a schedule and arms submit. (The preview renders the time
  // through the browser's locale formatter, so assert on the built ISO instant, not on
  // "23:59" — 23:59 local is a different wall clock in UTC.)
  await expect(page.getByTestId("schedule-preview").locator(".preview-cron")).not.toHaveText("");
  await expect(page.getByTestId("schedule-submit")).toBeEnabled();

  // Now lose the time, the way the Windows report describes the field doing on blur.
  await page.getByTestId("schedule-once-time").fill("");

  // It used to become 09:00 here, silently, and submit stayed enabled — the dialog
  // reported success while scheduling the agent at a materially different time.
  await expect(page.getByTestId("schedule-preview").locator(".preview-cron")).toHaveCount(0);
  await expect(page.getByTestId("schedule-error")).toContainText("Pick a time");
  await expect(page.getByTestId("schedule-submit")).toBeDisabled();
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

test("the detail dialog edits via the same Once/Repeat/Cron builder, gated until changed", async ({ page }) => {
  await gotoSchedule(page);
  await page.getByTestId("schedule-row-job-1").click();
  await page.getByTestId("schedule-detail-edit").click();
  const detail = page.getByTestId("schedule-detail");
  // #2159 part 4: edit opens the SAME builder, pre-filled by parsing the stored schedule
  // ("0 9 * * *" → Repeat / daily). The live preview reflects it.
  const preview = detail.getByTestId("schedule-preview");
  await expect(preview).toContainText("every day at");
  await expect(preview.locator("code")).toContainText("0 9 * * *");
  await expect(page.getByTestId("schedule-detail-prompt")).toHaveValue(/Summarize overnight activity/);
  // Save is gated until a real change.
  await expect(page.getByTestId("schedule-detail-save")).toBeDisabled();
  // Change the frequency through the builder → the schedule changes → Save enables.
  await detail.locator("#schedule-freq").click();
  await page.getByRole("menuitemradio", { name: "Every weekday" }).click();
  await expect(preview.locator("code")).toContainText("* * 1-5");
  await expect(page.getByTestId("schedule-detail-save")).toBeEnabled();
});

test("editing a UTC job keeps it UTC and preserves an untouched schedule string (#2439 review)", async ({ page }) => {
  await gotoSchedule(page);
  // job-2 is stored WITHOUT a timezone (= UTC) and with a leading-zero cron hour.
  await page.getByTestId("schedule-row-job-2").click();
  await page.getByTestId("schedule-detail-edit").click();
  const detail = page.getByTestId("schedule-detail");
  // "0 09 * * *" parses to the daily preset; the builder shows its normalized cron, but
  // a cosmetic re-format is not an edit — Save must open disabled.
  await expect(detail.getByTestId("schedule-preview").locator("code")).toContainText("0 9 * * *");
  await expect(page.getByTestId("schedule-detail-save")).toBeDisabled();
  // A prompt-only save sends the STORED schedule string verbatim and no timezone — the
  // job was UTC and must not silently adopt the operator's local zone.
  await page.getByTestId("schedule-detail-prompt").fill("Rotate the cache, quietly.");
  const put = page.waitForRequest((r) => r.method() === "PUT" && r.url().includes("/api/scheduler/jobs/job-2"));
  await page.getByTestId("schedule-detail-save").click();
  const body = (await put).postDataJSON();
  expect(body.schedule).toBe("0 09 * * *");
  expect(body.timezone).toBeUndefined();
});

test("a job whose stored schedule can't validate still takes a prompt-only edit (#2439 review)", async ({ page }) => {
  await gotoSchedule(page);
  // job-3 is a one-off already in the past — the builder flags it, but since an untouched
  // schedule saves the STORED string, builder validity must not lock the prompt.
  await page.getByTestId("schedule-row-job-3").click();
  await page.getByTestId("schedule-detail-edit").click();
  await expect(page.getByTestId("schedule-error")).toContainText("in the past");
  await page.getByTestId("schedule-detail-prompt").fill("Compile it anyway.");
  const put = page.waitForRequest((r) => r.method() === "PUT" && r.url().includes("/api/scheduler/jobs/job-3"));
  await page.getByTestId("schedule-detail-save").click();
  expect((await put).postDataJSON().schedule).toBe("2026-05-01T09:00:00Z");
});

test("the row delete button confirms first — Cancel keeps the job", async ({ page }) => {
  await gotoSchedule(page);
  // The row's trash no longer deletes on one click; it summons a ConfirmDialog.
  await page.getByRole("button", { name: "Delete job" }).first().click();
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
  await page.getByRole("button", { name: "Delete job" }).first().click();
  const confirm = page.getByRole("dialog", { name: "Delete scheduled job?" });
  await expect(confirm).toBeVisible();
  await page.keyboard.press("Escape"); // click-outside / Esc cancels (no delete)
  await expect(confirm).toBeHidden();
  await expect(page.getByTestId("schedule-row-job-1")).toBeVisible();

  // Re-open and actually confirm → the dialog closes (the cancel mutation fires).
  await page.getByRole("button", { name: "Delete job" }).first().click();
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
