import { expect, test } from "@playwright/test";

// The console opens a server→client SSE channel (GET /api/events, ADR 0003) for
// the app's lifetime. The topbar "live" indicator reflects the connection.

test("live indicator turns connected when the event stream opens", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const dot = page.getByTestId("live-indicator");
  await expect(dot).toBeVisible();
  // EventSource onopen flips it live shortly after the response headers arrive.
  await expect(dot).toHaveAttribute("data-live", "true");
});

test("a goal.achieved bus event surfaces a toast", async ({ page }) => {
  // ADR 0039 — the mock pushes a one-shot goal.achieved; the console toasts it so a plain
  // operator notices a terminal goal without writing a plugin hook.
  await page.goto("/app/", { waitUntil: "load" });
  const toast = page.locator(".pl-toast", { hasText: "Goal achieved" });
  await expect(toast).toBeVisible();
  await expect(toast).toContainText("unit tests pass");
});
