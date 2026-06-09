import { expect, test } from "@playwright/test";

// The Activity provenance feed (ADR 0022): a timeline of agent-initiated turns,
// each tagged with what triggered it. Loads entries from GET /api/activity,
// shows an unread badge while off-surface, and appends pushed events live.

test("feed shows entries with provenance + live append", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Pushed activity messages arrive while we're on Chat → the rail badge shows.
  await expect(page.locator(".pl-rail__badge")).toBeVisible();

  await page.getByRole("button", { name: "Activity", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Activity" })).toBeVisible();

  const feed = page.getByTestId("activity-surface");
  // Entry text + its provenance badges (origin + trigger) render.
  await expect(feed.getByText("3 PRs merged overnight, CI green.")).toBeVisible();
  await expect(feed.getByText("scheduled").first()).toBeVisible(); // origin badge
  await expect(feed.getByText("daily-brief")).toBeVisible(); // trigger label
  await expect(feed.getByText("Build failed on main — investigating.")).toBeVisible();
  await expect(feed.getByText("inbox").first()).toBeVisible(); // inbox origin badge

  // A pushed event appends live while the surface is open.
  await expect(feed.getByText("live activity ping").first()).toBeVisible();
});

test("replying clears the composer (the answer returns as a feed entry)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Activity", exact: true }).click();

  const composer = page.locator(".activity-composer textarea");
  await composer.fill("ping from operator");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  // The feed shows agent outputs (not an echo of your reply); send clears the box.
  await expect(composer).toHaveValue("");
});
