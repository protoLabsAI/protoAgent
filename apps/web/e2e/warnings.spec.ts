import { expect, test } from "@playwright/test";

// Runtime-status `warnings` (#706 co-located instances etc.) render as a slim
// alert strip under the topbar; server-driven, so no warnings → no strip.

test("runtime warnings render as the shell alert strip", async ({ page }) => {
  await page.route("**/api/runtime/status", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    json.warnings = ["Another running instance shares this agent's data (~/.protoagent): roxy (pid 12345, port 7871)."];
    await route.fulfill({ json });
  });
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".shell-warning-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("Another running instance");
  await expect(banner).toHaveAttribute("role", "alert");
});

test("no warnings → no alert strip (the default fixture)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible(); // app booted
  await expect(page.locator(".shell-warning-banner")).toHaveCount(0);
});
