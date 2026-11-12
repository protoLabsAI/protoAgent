import { expect, test } from "@playwright/test";

// History left Work with the four-group consolidation (work-consolidation.md v2, ADR 0129 Q3):
// the Feed pill opens the widget's OWN dialog, where pending pages are triaged and completed
// turns are read. Work itself shows an undelivered page only as a Needs you "Delivery" row.
test("the Feed dialog preserves pending triage and live completed turns after clearing the badge", async ({ page }) => {
  let historyReads = 0;
  await page.route("**/api/activity", async (route) => {
    historyReads++;
    await route.fulfill({ json: { context_id: "activity", messages: [], entries: [] } });
  });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByTestId("activity-badge")).toBeVisible();
  expect(historyReads).toBe(0);
  await page.getByTestId("activity-widget").click();
  const dialog = page.getByRole("dialog", { name: /^Feed/ });
  await expect(dialog).toBeVisible();
  await expect(page.getByTestId("activity-badge")).toHaveCount(0);
  const feed = dialog.getByTestId("unified-feed");
  await expect(feed).toBeVisible();
  const pending = feed.getByTestId("feed-pending");
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

  // The mock bus keeps publishing while the feed is open: its next `activity.message`
  // live-appends into the timeline without re-raising the badge.
  const live = feed.locator(".activity-entry", { hasText: "live activity ping" }).first();
  await expect(live).toBeVisible();
  await expect(live).toContainText("Hourly heartbeat check.");
  await expect(page.getByTestId("activity-badge")).toHaveCount(0);

  // Leaving the dialog resumes unread tracking on the live event bus.
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("activity-badge")).toBeVisible();
});

test("the Feed pill never opens the Work modal", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("activity-widget").click();
  await expect(page.getByRole("dialog", { name: /^Feed/ })).toBeVisible();
  await expect(page.locator('.work-view[data-frame="modal"]')).toHaveCount(0);
});

test("Decisions pill opens the Work Needs you group", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("decisions-widget").click();
  const work = page.locator('.work-view[data-frame="modal"]');
  await expect(work).toBeVisible();
  await expect(work.getByRole("button", { name: /Work sections/ })).toContainText("Needs you");
});
