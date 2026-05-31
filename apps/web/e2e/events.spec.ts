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
