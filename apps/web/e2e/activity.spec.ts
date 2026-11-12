import { expect, test } from "@playwright/test";

// S4c: the Feed pill is the activity feed's own home — it opens the full ActivitySurface
// (pending triage + completed provenance timeline) in its own dialog. Work has no Activity
// section any more, so none of this routes through the Work modal.
test("Feed pill opens its own dialog with pending triage and live completed turns", async ({ page }) => {
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
        // The scheduler prepends its wake header; the feed must not show it (#5094).
        stimulus: "[Autonomous wake — scheduled run. Orient from <working_state>, then:]\n\nHourly persisted heartbeat check.",
      }] : [],
    } });
  });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByTestId("activity-badge")).toBeVisible();
  // The dialog body mounts only while open — a closed pill reads no history.
  expect(historyReads).toBe(0);
  await page.getByTestId("activity-widget").click();
  const dialog = page.getByRole("dialog", { name: /Feed/ });
  await expect(dialog).toBeVisible();
  // No Work modal involved: opening the feed never opens Work.
  await expect(page.locator('.work-view[data-frame="modal"]')).toHaveCount(0);
  await expect(page.getByTestId("activity-badge")).toHaveCount(0);
  const feed = dialog.getByTestId("unified-feed");
  await expect(feed).toBeVisible();
  // The dialog keeps its own fixed-height feed box (#5094's dialog mode).
  await expect(feed).toHaveClass(/util-dialog-fill/);

  // PENDING triage (ADR 0003): the blocked priority-now page is conspicuous, with context.
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

  // COMPLETED timeline (ADR 0022): live bus frames append with their provenance...
  const completed = dialog.getByTestId("feed-completed");
  const live = completed.locator(".activity-entry", { hasText: "live activity ping" }).first();
  await expect(live).toBeVisible();
  await expect(live.locator(".activity-origin")).toContainText("scheduled");
  // The mock bus publishes the scheduler's real stimulus, wake header included; the
  // row shows only the job's own words (#5094).
  await expect(live.locator(".activity-stimulus-text")).toHaveText("Hourly heartbeat check.");
  // ...and the header reload refetches the persisted history into the same timeline.
  expect(historyReads).toBeGreaterThan(0);
  includeNewTurn = true;
  await dialog.getByRole("button", { name: "Refresh" }).click();
  const persisted = completed.locator(".activity-entry", { hasText: "Live completed response" }).first();
  await expect(persisted).toBeVisible();
  // Persisted scheduled turns strip the wake header too — in the row and its hover text —
  // so the internal "[Autonomous wake — …]" framing never reaches the operator (#5094).
  await expect(persisted.locator(".activity-stimulus-text")).toHaveText("Hourly persisted heartbeat check.");
  await expect(persisted.locator(".activity-stimulus")).toHaveAttribute("title", "Hourly persisted heartbeat check.");
  await expect(dialog.getByText(/Autonomous wake/)).toHaveCount(0);
  // The feed stays read the whole time it is open.
  await expect(page.getByTestId("activity-badge")).toHaveCount(0);

  // Leaving the dialog resumes unread tracking on the live event bus.
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("activity-badge")).toBeVisible();
});

test("Decisions pill opens the Work Decisions section", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("decisions-widget").click();
  const work = page.locator('.work-view[data-frame="modal"]');
  await expect(work).toBeVisible();
  await expect(work.getByRole("button", { name: /Work sections/ })).toContainText("Decisions");
});
