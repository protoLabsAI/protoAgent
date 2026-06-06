import { expect, test } from "@playwright/test";

// Plugin-contributed console surfaces (ADR 0026): an enabled plugin that declares
// a `views` entry (surfaced via /api/runtime/status) gets a dynamic rail icon
// whose panel is an iframe of the page the plugin serves. The mock runtime-status
// includes a "boardy" plugin with one view.

test("a plugin view adds a rail icon that opens its page in an iframe", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // The plugin's view label appears as a rail button (beyond the core surfaces).
  const railBtn = page.locator(".rail").getByRole("button", { name: "Board", exact: true });
  await expect(railBtn).toBeVisible();

  // Clicking it hosts the plugin page in a same-origin iframe at the declared path.
  await railBtn.click();
  const frame = page.locator(".plugin-view-frame");
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute("src", /\/plugins\/boardy\/board/);
  await expect(frame).toHaveAttribute("sandbox", /allow-scripts/);

  // Switching back to a core surface (Chat) hides the plugin view.
  await page.locator(".rail").getByRole("button", { name: "Chat", exact: true }).click();
  await expect(page.locator(".plugin-view-frame")).toHaveCount(0);
});
