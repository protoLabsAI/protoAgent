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

  // #1643 — the fresh install is configurable IMMEDIATELY (no page refresh): install
  // invalidates the settings schema, so the row grows a Configure button and the
  // dialog opens with the plugin's fields, not empty.
  await row.getByRole("button", { name: "Configure protoagent-plugin-widgets" }).click();
  const config = page.getByRole("dialog", { name: "protoagent-plugin-widgets" });
  await expect(config.locator('.setting-row[data-key="protoagent-plugin-widgets.greeting"]')).toBeVisible();
  await config.locator(".pl-dialog__close").click();

  // Uninstall from the row — a DS ConfirmDialog guards it; confirm and the row disappears.
  await row.getByRole("button", { name: /uninstall/i }).click();
  const confirm = page.getByRole("dialog", { name: "Uninstall plugin?" });
  await expect(confirm).toBeVisible();
  await confirm.getByRole("button", { name: "Uninstall", exact: true }).click();
  await expect(page.locator(".plugin-row-wrap", { hasText: "protoagent-plugin-widgets" })).toHaveCount(0);
});

test("the install dialog's form guards an empty URL", async ({ page }) => {
  await openInstallDialog(page);
  await expect(page.getByLabel("plugin git URL")).toBeVisible();
  await expect(page.getByRole("button", { name: "Install", exact: true })).toBeDisabled();
});

test("Discover install → Configure dialog hydrates without a page refresh (#1643)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Plugins", exact: true }).click();
  // Land on Installed first so the settings schema is fetched + cached WITHOUT the new
  // plugin's group — the bug's precondition (the schema query has a 5-min staleTime, so
  // without the install-side invalidation the stale cache serves the Configure dialog).
  await expect(page.locator(".plugin-row-wrap", { hasText: "Demo Plugin" })).toBeVisible();

  // Install from the Discover directory (this path used to skip the schema refetch).
  await page.locator(".pl-tabs").getByRole("tab", { name: "Discover", exact: true }).click();
  const card = page.locator(".plugin-card", { hasText: "Artifact" });
  await card.getByRole("button", { name: "Install", exact: true }).click();
  await expect(page.locator(".pl-toast", { hasText: "Plugin installed" })).toBeVisible();

  // Back on Installed: the new row offers Configure NOW — no page refresh — and the
  // dialog carries the plugin's fields (the schema was refetched after install).
  await page.locator(".pl-tabs").getByRole("tab", { name: "Installed", exact: true }).click();
  const row = page.locator(".plugin-row-wrap", { hasText: "artifact-plugin" });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Configure artifact-plugin" }).click();
  const config = page.getByRole("dialog", { name: "artifact-plugin" });
  await expect(config.locator('.setting-row[data-key="artifact-plugin.greeting"]')).toBeVisible();
  await config.locator(".pl-dialog__close").click();

  // Clean up the shared mock state: uninstall the plugin again.
  await row.getByRole("button", { name: /uninstall/i }).click();
  const confirm = page.getByRole("dialog", { name: "Uninstall plugin?" });
  await confirm.getByRole("button", { name: "Uninstall", exact: true }).click();
  await expect(page.locator(".plugin-row-wrap", { hasText: "artifact-plugin" })).toHaveCount(0);
});
