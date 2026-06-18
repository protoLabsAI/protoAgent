import { expect, test } from "@playwright/test";

// The console opens a server→client SSE channel (GET /api/events, ADR 0003) for the
// app's lifetime. The dedicated topbar "live" indicator was removed with the status
// light; the goal.achieved toast below still exercises the channel end-to-end — the
// event only arrives if the SSE connected.

test("a goal.achieved bus event surfaces a toast", async ({ page }) => {
  // ADR 0039 — the mock pushes a one-shot goal.achieved; the console toasts it so a plain
  // operator notices a terminal goal without writing a plugin hook.
  await page.goto("/app/", { waitUntil: "load" });
  const toast = page.locator(".pl-toast", { hasText: "Goal achieved" });
  await expect(toast).toBeVisible();
  await expect(toast).toContainText("unit tests pass");
});
