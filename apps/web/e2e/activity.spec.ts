import { expect, test } from "@playwright/test";

// The merged Feed (docs/design/feed.md, slice 4): the pill opens ONE newest-first list in
// its own dialog — pending inbox items as quiet queued rows, live and persisted turns as
// ran/failed rows under day headings. Nothing in the Feed dismisses, delivers or alerts
// (bd-rbk7e retries undelivered pages server-side), and the scheduler's internal wake
// header never reaches the operator (#5094).
test("Feed pill opens one newest-first merged list with queued, live and persisted rows", async ({ page }) => {
  let includeNewTurn = false;
  let historyReads = 0;
  const delivers: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/deliver")) delivers.push(r.url());
  });
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
  // The dialog keeps the shared util body height (#5165).
  await expect(feed).toHaveClass(/util-dialog-fill/);

  // QUEUED: both pending items render as calm rows — the formerly "blocked" priority-now
  // page included. No pill, no dismiss control, no alert, no "undelivered page" flag.
  const queued = feed.locator('.feed-row[data-kind="queued"]');
  await expect(queued).toHaveCount(2);
  await expect(queued.filter({ hasText: "build failed on main" }))
    .toContainText("Queued — runs when protoAgent is free");
  await expect(queued.filter({ hasText: "new signup: acme.co" })).toHaveCount(1);
  await expect(feed.locator(".feed-row button")).toHaveCount(0); // rows are the only controls
  await expect(feed.locator(".pl-alert")).toHaveCount(0);
  await expect(feed.getByText("undelivered page")).toHaveCount(0);

  // LIVE: bus frames land as rows, newest first under a Today heading; the failed turn
  // carries exactly one Failed badge.
  const live = feed.locator(".feed-row", { hasText: "live activity ping" }).first();
  await expect(live).toBeVisible();
  await expect(live).toContainText("in response to “Hourly heartbeat check.”");
  const failed = feed.locator('.feed-row[data-kind="failed"]').first();
  await expect(failed.locator(".pl-badge--error")).toHaveText("Failed");
  await expect(feed.locator("h3.feed-day-heading").first()).toHaveText("Today");

  // ...and the header reload refetches the persisted history into the same list.
  expect(historyReads).toBeGreaterThan(0);
  includeNewTurn = true;
  await dialog.getByRole("button", { name: "Refresh" }).click();
  const persisted = feed.locator(".feed-row", { hasText: "Live completed response" }).first();
  await expect(persisted).toBeVisible();
  // Persisted scheduled turns strip the wake header too — the internal
  // "[Autonomous wake — …]" framing never reaches the operator (#5094).
  await expect(persisted).toContainText("in response to “Hourly persisted heartbeat check.”");
  await expect(dialog.getByText(/Autonomous wake/)).toHaveCount(0);

  // Nothing in the Feed delivered or dismissed anything, and it stays read while open.
  expect(delivers).toEqual([]);
  await expect(page.getByTestId("activity-badge")).toHaveCount(0);

  // Leaving the dialog resumes unread tracking on the live event bus.
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("activity-badge")).toBeVisible();
});

test("Decisions pill opens the Work Needs you group", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("decisions-widget").click();
  const work = page.locator('.work-view[data-frame="modal"]');
  await expect(work).toBeVisible();
  await expect(work.getByRole("button", { name: /Work sections/ })).toContainText("Needs you");
});
