import { expect, test } from "@playwright/test";

// The Activity surface (ADR 0003): the durable thread where agent-initiated
// turns land. Loads history from GET /api/activity, shows an unread badge while
// off-surface, and appends pushed `activity.message` events live.

test("unread badge counts pushed messages; surface shows history + live append", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Pushed activity messages arrive while we're on Chat → the rail badge shows.
  await expect(page.getByTestId("activity-badge")).toBeVisible();

  await page.getByRole("button", { name: "Activity", exact: true }).click();
  // Activity opens on its Thread sub-tab by default.
  await expect(page.getByRole("heading", { name: "Activity" })).toBeVisible();

  // History from GET /api/activity renders.
  await expect(page.getByText("morning standup")).toBeVisible();
  await expect(page.getByText("3 PRs merged overnight, CI green.")).toBeVisible();

  // A pushed event appends live while the surface is open.
  await expect(page.getByText("live activity ping").first()).toBeVisible();
});

test("replying optimistically appends the operator's message", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Activity", exact: true }).click();

  await page.locator(".activity-composer textarea").fill("ping from operator");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByText("ping from operator")).toBeVisible();
});
