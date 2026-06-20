import { expect, test } from "@playwright/test";

// Console Plugins manager (Settings ▸ Plugins, 2026-06 consolidation) — install a plugin
// from a git URL via the dialog; uninstall it from its row in the Installed list.

async function openInstallDialog(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Plugins", exact: true }).click();
  // Install-from-URL is a dialog opened from the Installed toolbar. The DS Dialog title is
  // role="dialog" (its accessible name), not a heading — assert the dialog via its URL field,
  // which only renders while the dialog is open (InstallPluginDialog returns null when closed).
  await page.getByRole("button", { name: "Install from URL" }).click();
  await expect(page.getByLabel("plugin git URL")).toBeVisible();
}

test("install a plugin from a git URL, then uninstall it from its row", async ({ page }) => {
  await openInstallDialog(page);

  // Install — a clean install closes the dialog; the new (auto-enabled) plugin joins the
  // Installed list.
  await page.getByLabel("plugin git URL").fill("https://github.com/acme/protoagent-plugin-widgets");
  await page.getByRole("button", { name: "Install", exact: true }).click();
  await expect(page.getByLabel("plugin git URL")).toHaveCount(0);

  const row = page.locator(".plugin-row-wrap", { hasText: "protoagent-plugin-widgets" });
  await expect(row).toBeVisible();

  // Uninstall from the row — a window.confirm guards it; accept and the row disappears.
  page.once("dialog", (d) => d.accept());
  await row.getByRole("button", { name: /uninstall/i }).click();
  await expect(page.locator(".plugin-row-wrap", { hasText: "protoagent-plugin-widgets" })).toHaveCount(0);
});

test("the install dialog's form guards an empty URL", async ({ page }) => {
  await openInstallDialog(page);
  await expect(page.getByLabel("plugin git URL")).toBeVisible();
  await expect(page.getByRole("button", { name: "Install", exact: true })).toBeDisabled();
});
