import { expect, test } from "@playwright/test";

// The unified feed is ONE read-only utility-bar widget (#3029) that merged the former
// separate Activity and Inbox pills: a bottom-left pill whose unread badge tracks BOTH
// the `inbox.item` (pending inbound stimuli, ADR 0003) and `activity.message` (completed
// agent turns, ADR 0022) bus events. Click opens one dialog — pending inbox items on top,
// the completed provenance timeline below. Loads from GET /api/inbox + GET /api/activity.

test("one widget: badge from both events → pending inbox + completed activity", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Exactly one widget replaces the old two pills — no separate inbox widget survives.
  await expect(page.getByTestId("activity-widget")).toBeVisible();
  await expect(page.getByTestId("inbox-widget")).toHaveCount(0);

  // The unread badge bumps off BOTH bus events (the mock pushes `activity.message`
  // AND `inbox.item` while the dialog is closed).
  await expect(page.getByTestId("activity-badge")).toBeVisible();

  // Click the pill → the unified feed opens in a dialog (and the badge clears).
  await page.getByTestId("activity-widget").click();
  const feed = page.getByTestId("unified-feed");
  await expect(feed).toBeVisible();
  await expect(page.getByTestId("activity-badge")).toHaveCount(0);

  // --- Pending section (top): inbox items from GET /api/inbox with priority + source. ---
  const pending = page.getByTestId("feed-pending");
  await expect(pending.getByText("build failed on main")).toBeVisible();
  await expect(pending.getByText("new signup: acme.co")).toBeVisible();
  await expect(pending.locator(".inbox-pri-now")).toBeVisible();

  // Dismiss the first pending item → POST /api/inbox/1/deliver, and it leaves the list.
  const deliver = page.waitForRequest(
    (r) => r.url().includes("/api/inbox/1/deliver") && r.method() === "POST",
  );
  const firstItem = pending.locator(".inbox-item", { hasText: "build failed on main" });
  await firstItem.locator(".inbox-dismiss").click();
  await deliver;
  await expect(pending.getByText("build failed on main")).toHaveCount(0);
  await expect(pending.getByText("new signup: acme.co")).toBeVisible();

  // It stays gone across the live `inbox.item` refetch cycle (the dismissed-id set
  // survives the refetch even though GET /api/inbox still returns the row).
  await page.waitForTimeout(700);
  await expect(pending.getByText("build failed on main")).toHaveCount(0);

  // --- Completed section (below): the provenance timeline with markdown + badges. ---
  await expect(feed.getByText("3 PRs merged overnight, CI green.")).toBeVisible();
  await expect(feed.getByText("scheduled").first()).toBeVisible(); // origin badge
  await expect(feed.getByText("daily-brief")).toBeVisible(); // trigger label
  await expect(feed.getByText("Build failed on main — investigating.")).toBeVisible();

  // Each completed response is explicitly attributed to the stimulus it replies to (#1375).
  const sched = feed.locator(".activity-entry", { hasText: "3 PRs merged overnight, CI green." });
  await expect(sched.locator(".activity-stimulus")).toContainText("in response to");
  await expect(sched.locator(".activity-stimulus-text")).toContainText("Summarize overnight repo activity");

  // A pushed `activity.message` appends live while the dialog is open — carrying its stimulus.
  const live = feed.locator(".activity-entry", { hasText: "live activity ping" }).first();
  await expect(live).toBeVisible();
  await expect(live.locator(".activity-stimulus-text")).toContainText("Hourly heartbeat check");
  await expect(live).toHaveAttribute("data-origin", "scheduler");

  // A failed sister-agent turn keeps its partial result but also shows the terminal
  // cause, so it can't be mistaken for a successful handoff.
  const peer = feed.locator(".activity-entry", { hasText: "second target timed out" }).first();
  await expect(peer).toHaveAttribute("data-origin", "a2a");
  await expect(peer).toHaveAttribute("data-state", "failed");
  await expect(peer.getByText("sister-agent")).toBeVisible();
  await expect(peer.getByText("A2A turn failed:")).toBeVisible();

  // Live rows get distinct React keys / data ids (no same-millisecond collision).
  const liveId = await live.getAttribute("data-entry-id");
  const peerId = await peer.getAttribute("data-entry-id");
  expect(liveId).not.toBeNull();
  expect(peerId).not.toBeNull();
  expect(peerId).not.toBe(liveId);

  // Read-only — there is no reply composer.
  await expect(page.locator(".activity-composer")).toHaveCount(0);

  // The pushed task identity is retained in the reader subtitle, so a terminal
  // sister-agent entry can be correlated with the durable A2A task (open-in-reader).
  await peer.hover();
  await peer.getByRole("button", { name: "Open in reader" }).click();
  await expect(page.locator(".doc-viewer")).toContainText("task a2a-task-2644");
});

test("a completed activity entry opens in the shared full-screen document reader (ADR 0062)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("activity-widget").click();
  const feed = page.getByTestId("unified-feed");
  const entry = feed.locator(".activity-entry", { hasText: "3 PRs merged overnight, CI green." });
  await entry.hover();
  await entry.getByRole("button", { name: "Open in reader" }).click();

  // The full-screen document viewer opens (on top of the feed) with the entry's full content.
  const reader = page.locator(".doc-viewer");
  await expect(reader).toBeVisible();
  await expect(reader.getByText("3 PRs merged overnight, CI green.")).toBeVisible();
});
