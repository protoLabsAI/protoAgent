import { expect, test } from "@playwright/test";

test("Work Activity preserves pending triage and live completed turns after clearing the badge", async ({ page }) => {
  let includeNewTurn = false;
  let historyReads = 0;
  await page.route("**/api/activity", async (route) => {
    historyReads++;
    await route.fulfill({ json: {
      context_id: "activity", messages: [],
      entries: includeNewTurn ? [{
        id: 100, created_at: new Date().toISOString(), origin: "scheduler",
        trigger: "heartbeat", priority: "", state: "completed", task_id: "",
        text: "Live completed response",
        // The scheduler prepends its wake header; the row title must not show it (#5094).
        stimulus: "[Autonomous wake — scheduled run. Orient from <working_state>, then:]\n\nHourly heartbeat check.",
      }] : [],
    } });
  });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByTestId("activity-badge")).toBeVisible();
  expect(historyReads).toBe(0);
  await page.getByTestId("activity-widget").click();
  const work = page.locator('.work-view[data-frame="modal"]');
  await expect(work).toBeVisible();
  await expect(work.getByRole("button", { name: /Work sections/ })).toContainText("Activity");
  await expect(page.getByTestId("activity-badge")).toHaveCount(0);
  const feed = work.getByTestId("unified-feed");
  await expect(feed).toBeVisible();
  const pending = feed.getByTestId("feed-pending");
  // In Work the pending cards sit in the list's flow (#5094): no dialog-height box, no
  // inner scroller, and the Work list is the only scroller, with no horizontal overflow.
  await expect(pending).toHaveCSS("overflow-y", "visible");
  await expect(feed).not.toHaveClass(/util-dialog-fill/);
  const workList = work.locator(".work-view__list");
  await expect.poll(() => workList.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  const blockedPage = pending.getByTestId("inbox-now-page");
  await expect(blockedPage).toContainText("build failed on main");
  await expect(blockedPage).toContainText("undelivered page");
  await expect(blockedPage.locator(".inbox-source")).toHaveText("ci");
  await expect(pending.getByText("new signup: acme.co")).toBeVisible();

  const delivered = page.waitForRequest(
    (r) => new URL(r.url()).pathname === "/api/inbox/1/deliver" && r.method() === "POST",
  );
  await blockedPage.getByTitle("Deliver this page (mark delivered) and dismiss").click();
  await delivered;
  await expect(blockedPage).toHaveCount(0);
  // The mock deliberately keeps returning the dismissed row. Live inbox refreshes
  // must preserve the explicit dismissal while leaving the other queued item visible.
  for (let i = 0; i < 2; i++) {
    await page.waitForResponse((r) => new URL(r.url()).pathname === "/api/inbox" && r.request().method() === "GET");
  }
  await expect(pending.getByText("build failed on main")).toHaveCount(0);
  await expect(pending.getByText("new signup: acme.co")).toBeVisible();

  // The server persists turns before publishing activity.message. The mock bus
  // continues publishing while Work is open; its next event must refresh this row.
  await expect(work.getByText("No activity yet")).toBeVisible();
  includeNewTurn = true;
  const live = work.locator(".work-row", { hasText: "Hourly heartbeat check." });
  await expect(live).toBeVisible();
  await expect(live.locator(".work-row__title")).toHaveText("Hourly heartbeat check.");
  await expect(page.getByTestId("activity-badge")).toHaveCount(0);
  await live.click();
  await expect(work.locator(".work-view__detail")).toContainText("Live completed response");
  await expect(work.locator(".work-view__detail")).toContainText("Scheduled");

  // Escape dismisses the selected inspector before closing Work. Activity stays
  // read while its list is still open, and focus returns to the live row.
  await page.keyboard.press("Escape");
  await expect(work).toBeVisible();
  await expect(work.locator(".work-view__detail")).toHaveCount(0);
  await expect(live).toBeFocused();
  await expect(live).toHaveAttribute("aria-current", "false");
  await expect(page.getByTestId("activity-badge")).toHaveCount(0);

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
  await expect(work.getByRole("button", { name: /Work sections/ })).toContainText("Decisions");
});
