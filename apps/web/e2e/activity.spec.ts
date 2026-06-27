import { expect, test } from "@playwright/test";

// Activity is a read-only utility-bar widget (2026-06 IA pass): a bottom-left pill with an
// unread badge that opens the provenance feed (ADR 0022) in a dialog. The feed loads from
// GET /api/activity and appends pushed `activity.message` events live while the dialog is open.

test("widget badge → dialog feed with provenance + live append", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Pushed activity messages arrive while the dialog is closed → the widget's unread badge shows.
  await expect(page.getByTestId("activity-badge")).toBeVisible();

  // Click the widget pill → the read-only feed opens in a dialog (and the badge clears).
  await page.getByTestId("activity-widget").click();
  const feed = page.getByTestId("activity-surface");
  await expect(feed).toBeVisible();
  await expect(page.getByTestId("activity-badge")).toHaveCount(0);

  // Entry text + its provenance badges (origin + trigger) render.
  await expect(feed.getByText("3 PRs merged overnight, CI green.")).toBeVisible();
  await expect(feed.getByText("scheduled").first()).toBeVisible(); // origin badge
  await expect(feed.getByText("daily-brief")).toBeVisible(); // trigger label
  await expect(feed.getByText("Build failed on main — investigating.")).toBeVisible();
  await expect(feed.getByText("inbox").first()).toBeVisible(); // inbox origin badge

  // A pushed event appends live while the dialog is open.
  await expect(feed.getByText("live activity ping").first()).toBeVisible();

  // Read-only since the IA pass — there is no reply composer.
  await expect(page.locator(".activity-composer")).toHaveCount(0);
});

test("an Activity entry opens in the shared full-screen document reader (ADR 0062)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("activity-widget").click();
  const feed = page.getByTestId("activity-surface");
  const entry = feed.locator(".activity-entry", { hasText: "3 PRs merged overnight, CI green." });
  await entry.hover();
  await entry.getByRole("button", { name: "Open in reader" }).click();

  // The full-screen document viewer opens (on top of the feed) with the entry's full content.
  const reader = page.locator(".doc-viewer");
  await expect(reader).toBeVisible();
  await expect(reader.getByText("3 PRs merged overnight, CI green.")).toBeVisible();
});
