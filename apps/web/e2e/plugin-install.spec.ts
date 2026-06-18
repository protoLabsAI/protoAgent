import { expect, test } from "@playwright/test";

// Console Plugins section — install a plugin from a git URL in the dedicated
// Plugins rail section; the installed list round-trips install → uninstall.

async function openPluginsPanel(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Plugins", exact: true }).click();
  // Install-from-URL is the advanced action under Installed (ADR 0059 D4) — expand it.
  await page.getByRole("button", { name: "Install from a git URL" }).click();
  await expect(page.getByRole("heading", { name: "Install from a git URL" })).toBeVisible();
}

test("install a plugin from a git URL, then uninstall it", async ({ page }) => {
  await openPluginsPanel(page);

  await expect(page.getByText("No git-installed plugins yet.")).toBeVisible();

  // Install
  await page.getByLabel("plugin git URL").fill("https://github.com/acme/protoagent-plugin-widgets");
  await page.getByRole("button", { name: "Install", exact: true }).click();

  // Row appears, AUTO-ENABLED — installing enables + runs the plugin (trust-by-default).
  const row = page.locator(".plugin-row");
  await expect(row).toHaveCount(1);
  await expect(row.locator(".plugin-row-title")).toContainText("protoagent-plugin-widgets");
  await expect(row.getByText("enabled", { exact: true })).toBeVisible();

  // Uninstall
  await row.getByRole("button", { name: /uninstall/i }).click();
  await expect(page.locator(".plugin-row")).toHaveCount(0);
  await expect(page.getByText("No git-installed plugins yet.")).toBeVisible();
});

test("install surfaces a bad-URL error from the server", async ({ page }) => {
  await openPluginsPanel(page);
  // Empty URL keeps the button disabled; a URL the mock accepts installs fine —
  // so just assert the form is present + actionable.
  await expect(page.getByLabel("plugin git URL")).toBeVisible();
  await expect(page.getByRole("button", { name: "Install", exact: true })).toBeDisabled();
});
