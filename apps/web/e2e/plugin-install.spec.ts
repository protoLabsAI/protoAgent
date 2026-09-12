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

  const row = page.locator(".plugin-table tbody tr", { hasText: "protoagent-plugin-widgets" });
  await expect(row).toBeVisible();

  // #2248 — the row says what the plugin DOES, not just its name. The description comes
  // from the manifest (the inventory side of the join); the runtime status has none.
  await expect(row.locator(".plugin-row-desc")).toHaveText("installed via console");

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
  await expect(page.locator(".plugin-table tbody tr", { hasText: "protoagent-plugin-widgets" })).toHaveCount(0);
});

test("the install dialog's form guards an empty URL", async ({ page }) => {
  await openInstallDialog(page);
  await expect(page.getByLabel("plugin git URL")).toBeVisible();
  await expect(page.getByRole("button", { name: "Install", exact: true })).toBeDisabled();
});

test("Discover cards show what a plugin adds and link its docs, like the website (#2910)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Plugins", exact: true }).click();
  await page.locator(".pl-tabs").getByRole("tab", { name: "Discover", exact: true }).click();

  const artifact = page.locator(".plugin-card", { hasText: "Artifact" });
  await expect(artifact.locator(".plugin-card-adds")).toHaveText(/tool\s*view/);
  await expect(artifact.getByRole("link", { name: "docs" })).toHaveAttribute(
    "href",
    "https://agent.protolabs.studio/docs/guides/plugins",
  );

  // A catalog entry without the fields (a fork's own catalog) renders neither.
  const discord = page.locator(".plugin-card", { hasText: "Discord" });
  await expect(discord).toBeVisible();
  await expect(discord.locator(".plugin-card-adds")).toHaveCount(0);
  await expect(discord.getByRole("link", { name: "docs" })).toHaveCount(0);

  // Search reaches the chips too, as the website's does.
  await page.getByRole("searchbox", { name: "Search plugins" }).fill("view");
  await expect(page.locator(".plugin-card")).toHaveCount(1);
  await expect(page.locator(".plugin-card", { hasText: "Artifact" })).toBeVisible();
});

test("Discover never offers Install for a bundled plugin, and shows its real on/off state", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Plugins", exact: true }).click();
  await page.locator(".pl-tabs").getByRole("tab", { name: "Discover", exact: true }).click();
  // By the card's title, exactly: "cowork" also appears in Execute Code's reason line.
  const card = (name: string) => page.locator(".plugin-card").filter({ has: page.getByText(name, { exact: true }) });
  const install = (name: string) => card(name).getByRole("button", { name: "Install", exact: true });

  // Ships in core and on.
  await expect(card("Cowork").locator(".plugin-card-foot")).toContainText("bundled · on");
  await expect(install("Cowork")).toHaveCount(0);
  // On only because cowork enables it, and the card says so.
  await expect(card("Execute Code").locator(".plugin-card-foot")).toContainText("bundled · on");
  await expect(card("Execute Code").locator(".plugin-card-why")).toHaveText("on because cowork enables it");
  await expect(install("Execute Code")).toHaveCount(0);
  // Ships in core, off: still no Install. It's turned on from the Installed tab.
  await expect(card("Telegram").locator(".plugin-card-foot")).toContainText("bundled · off");
  await expect(card("Telegram").locator(".plugin-card-why")).toHaveCount(0);
  await expect(install("Telegram")).toHaveCount(0);
  // A git-installable plugin still offers Install; an installed one says it's on.
  await expect(install("Artifact")).toBeVisible();
  await expect(card("Discord").locator(".plugin-card-foot")).toContainText("installed · on");
});

test("Discover install → Configure dialog hydrates without a page refresh (#1643)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Plugins", exact: true }).click();
  // Land on Installed first so the settings schema is fetched + cached WITHOUT the new
  // plugin's group — the bug's precondition (the schema query has a 5-min staleTime, so
  // without the install-side invalidation the stale cache serves the Configure dialog).
  await expect(page.locator(".plugin-table tbody tr", { hasText: "Demo Plugin" })).toBeVisible();

  // Install from the Discover directory (this path used to skip the schema refetch).
  await page.locator(".pl-tabs").getByRole("tab", { name: "Discover", exact: true }).click();
  const card = page.locator(".plugin-card", { hasText: "Artifact" });
  await card.getByRole("button", { name: "Install", exact: true }).click();
  await expect(page.locator(".pl-toast", { hasText: "Plugin installed" })).toBeVisible();

  // Back on Installed: the new row offers Configure NOW — no page refresh — and the
  // dialog carries the plugin's fields (the schema was refetched after install).
  await page.locator(".pl-tabs").getByRole("tab", { name: "Installed", exact: true }).click();
  const row = page.locator(".plugin-table tbody tr", { hasText: "artifact-plugin" });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Configure artifact-plugin" }).click();
  const config = page.getByRole("dialog", { name: "artifact-plugin" });
  await expect(config.locator('.setting-row[data-key="artifact-plugin.greeting"]')).toBeVisible();
  await config.locator(".pl-dialog__close").click();

  // Clean up the shared mock state: uninstall the plugin again.
  await row.getByRole("button", { name: /uninstall/i }).click();
  const confirm = page.getByRole("dialog", { name: "Uninstall plugin?" });
  await confirm.getByRole("button", { name: "Uninstall", exact: true }).click();
  await expect(page.locator(".plugin-table tbody tr", { hasText: "artifact-plugin" })).toHaveCount(0);
});
