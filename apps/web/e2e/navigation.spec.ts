import { expect, test } from "@playwright/test";

// Every workspace surface mounts and renders its mocked data. Guards against a
// surface crashing on an unexpected payload shape — the Runtime panel in
// particular reads the skills / MCP / plugins blocks.

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

// Grouped nav (heavy consolidation): click a rail group, then its in-surface sub-tab.
async function openSub(page, group: string, tab: string) {
  await page.getByRole("button", { name: group, exact: true }).click();
  await page.getByRole("button", { name: tab, exact: true }).click();
}

test("Studio lands directly on Workflows (Run tab removed — run is a chat gesture)", async ({ page }) => {
  // ADR 0020: execution moved to chat slash commands (/<subagent>, /<workflow>),
  // so Studio is just Workflows — no sub-nav, no Run tab.
  await page.getByRole("button", { name: "Studio", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Workflows" })).toBeVisible();
  // The old Run sub-tab is gone.
  await expect(page.locator(".pl-tabs").getByRole("tab", { name: "Run", exact: true })).toHaveCount(0);
});

// The Work hub (#4955) is one right-rail "Work" surface: an Overview plus a section per work
// source (Goals · Watches · Tasks · Schedules · …) in a SideNav. Per-section navigation and
// each section's data rendering are covered by the Work view unit tests (WorkView.test.tsx,
// WorkMount.test.tsx and the section tests); this file only asserts the surface itself mounts,
// persists, and moves between docks. (The former WorkPanel card-first navigation tests were
// retired with the WorkPanel surface they drove.)

test("settings dialog: Identity, then Tools and MCP sections", async ({ page }) => {
  // Settings is the consolidated dialog now (2026-06), opened from the utility-bar pill
  // (no longer a rail surface). Navigate the Agent group's Identity / Tools / MCP sections.
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Identity", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Identity" })).toBeVisible();
  await expect(page.getByTestId("identity-name")).toBeVisible();

  await page.locator(".pl-sidenav").getByRole("tab", { name: "Tools", exact: true }).click();
  await expect(page.getByText("web_search")).toBeVisible();

  await page.locator(".pl-sidenav").getByRole("tab", { name: "MCP", exact: true }).click();
  await expect(page.getByText("echo · stdio")).toBeVisible(); // MCP server
});

test("plugins section: Installed / Discover (config + advanced install folded in)", async ({ page }) => {
  // Plugins is a Settings dialog section now (2026-06), opened from the utility-bar pill.
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Plugins", exact: true }).click();

  // Two sections only now (ADR 0059 D4) — no separate "Install URL" tab.
  await expect(page.locator(".pl-tabs").getByRole("tab", { name: "Install URL", exact: true })).toHaveCount(0);

  // Installed (default tab) — one table now (loaded + disabled rows, status pills) with
  // the enable toggle in the row's action cell.
  await expect(page.getByText("Demo Plugin", { exact: false })).toBeVisible();
  await expect(page.getByText("Zzz Disabled", { exact: false })).toBeVisible();
  await page.locator(".plugin-table tbody tr", { hasText: "Zzz Disabled" })
    .getByRole("button", { name: "Enable" }).click();
  await expect(page.locator(".pl-toast", { hasText: "Zzz Disabled" })).toBeVisible();

  // Configure opens a per-plugin settings PANE now (#4488) — Settings ▸ Plugins ▸ <plugin>, not
  // an inline row expander and not a modal over Settings. The (icon-only) Configure button is
  // labelled "Configure <name>"; the pane is crumbed by the plugin's display name, with a back
  // link to the plugin list.
  const demoRow = page.locator(".plugin-table tbody tr", { hasText: "Demo Plugin" });
  await demoRow.getByRole("button", { name: "Configure" }).click();
  const configPane = page.locator(".plugin-settings-pane");
  await expect(configPane.locator(".plugin-settings-crumb-current")).toHaveText("Demo Plugin");
  await expect(configPane.locator('.setting-row[data-key="demo.greeting"]')).toBeVisible();
  await page.getByTestId("plugin-config-back").click(); // back to the plugin list for the later steps
  await expect(configPane).toHaveCount(0);

  // Install-from-URL is a dialog opened from the Installed toolbar (2026-06 consolidation).
  // The DS Dialog title is role="dialog" (not a heading); assert via the URL field, which
  // only renders while the dialog is open.
  await page.getByRole("button", { name: "Install from URL" }).click();
  const installDialog = page.getByRole("dialog", { name: "Install a plugin from a git URL" });
  await expect(installDialog.getByLabel("plugin git URL")).toBeVisible();
  // Close just this dialog via its scoped Close button before clicking the Discover tab —
  // Escape would also dismiss the Settings overlay (a sibling DS Dialog shares a
  // document-level Escape handler).
  await installDialog.getByRole("button", { name: "Close" }).click();
  await expect(page.getByLabel("plugin git URL")).toHaveCount(0);

  // Discover tab — the in-app official-plugin directory (ADR 0059): cards + search.
  await page.locator(".pl-tabs").getByRole("tab", { name: "Discover", exact: true }).click();
  await expect(page.getByLabel("Search plugins")).toBeVisible();
  const artifact = page.locator(".plugin-card", { hasText: "Artifact" });
  await expect(artifact.getByRole("button", { name: /Install/ })).toBeVisible();   // not installed → installable
  await expect(page.locator(".plugin-card", { hasText: "Discord" })).toContainText(/installed/);
});

test("UI state persists across reload (ADR 0035 S1 — Zustand persist)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Move the left surface off its default (Chat) to Knowledge. Work is the default-active
  // right panel (don't click it — that would toggle the panel closed). Both the moved-left
  // surface and the open Work right panel should survive a reload via the persisted store.
  await page.locator(".pl-rail").getByRole("button", { name: "Knowledge", exact: true }).click();
  await expect(page.locator(".pl-rail--right").getByRole("button", { name: "Work", exact: true })).toHaveClass(/--active/);

  // Reload — the persisted store restores both, instead of snapping back to Chat.
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").getByRole("button", { name: "Knowledge", exact: true })).toHaveClass(/--active/);
  await expect(page.locator(".pl-rail--right").getByRole("button", { name: "Work", exact: true })).toHaveClass(/--active/);
});


test("right-click a rail surface opens a context menu that moves it (ADR 0036)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const rightRail = page.locator(".pl-rail--right");
  const leftRail = page.locator(".pl-rail:not(.pl-rail--right)");

  // Work starts on the right rail.
  await expect(rightRail.getByRole("button", { name: "Work", exact: true })).toBeVisible();

  // Right-click it → the context menu opens with the move item.
  await rightRail.getByRole("button", { name: "Work", exact: true }).click({ button: "right" });
  const menu = page.locator(".pl-menu");
  await expect(menu).toBeVisible();
  await menu.getByText("Move to left rail").click();

  // Work moved to the left rail (store-backed → persists, but checking the move is enough here).
  await expect(leftRail.getByRole("button", { name: "Work", exact: true })).toBeVisible();
  await expect(rightRail.getByRole("button", { name: "Work", exact: true })).toHaveCount(0);
});

test("Chat is movable too — right-click → move to the right rail (ADR 0036)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const leftRail = page.locator(".pl-rail:not(.pl-rail--right)");
  const rightRail = page.locator(".pl-rail--right");

  await expect(leftRail.getByRole("button", { name: "Chat", exact: true })).toBeVisible();
  await leftRail.getByRole("button", { name: "Chat", exact: true }).click({ button: "right" });
  await page.locator(".pl-menu").getByText("Move to right rail").click();

  // Chat now lives on the right rail (no longer pinned left).
  await expect(rightRail.getByRole("button", { name: "Chat", exact: true })).toBeVisible();
  await expect(leftRail.getByRole("button", { name: "Chat", exact: true })).toHaveCount(0);
});

test("right-click → Move to bottom dock docks the surface at the bottom", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const rightRail = page.locator(".pl-rail--right");
  const bottomRail = page.locator(".pl-rail--bottom");
  // The bottom rail exists as an (empty) drop target, but nothing is docked there by default.
  await expect(bottomRail.getByRole("button")).toHaveCount(0);

  // Move Work (right rail) → bottom dock.
  await rightRail.getByRole("button", { name: "Work", exact: true }).click({ button: "right" });
  await page.locator(".pl-menu").getByText("Move to bottom dock").click();

  // Work now lives in the (icon-only) bottom rail, off the right rail, and its panel opens.
  await expect(bottomRail.getByRole("button", { name: "Work", exact: true })).toBeVisible();
  await expect(rightRail.getByRole("button", { name: "Work", exact: true })).toHaveCount(0);
  // Its panel renders — the Work view's two-pane shell is present in the bottom dock.
  await expect(page.locator(".pl-appshell__bottom .work-view__panes")).toBeVisible();
});

test("right-click a core surface → Hide removes it; Chat offers no Hide (ADR 0035/0036)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const leftRail = page.locator(".pl-rail:not(.pl-rail--right)");

  // Knowledge can be hidden off the rail (enabled-but-not-shown — no plugin to disable here).
  await expect(leftRail.getByRole("button", { name: "Knowledge", exact: true })).toBeVisible();
  await leftRail.getByRole("button", { name: "Knowledge", exact: true }).click({ button: "right" });
  await page.locator(".pl-menu").getByText("Hide", { exact: true }).click();
  await expect(leftRail.getByRole("button", { name: "Knowledge", exact: true })).toHaveCount(0);

  // Chat mounts unconditionally on its dock, so it must NOT offer Hide (a hidden chat would
  // render with no rail icon). Its menu still has the move actions.
  await leftRail.getByRole("button", { name: "Chat", exact: true }).click({ button: "right" });
  const menu = page.locator(".pl-menu");
  await expect(menu).toBeVisible();
  await expect(menu.getByText("Move to right rail")).toBeVisible();
  await expect(menu.getByText("Hide", { exact: true })).toHaveCount(0);
});
