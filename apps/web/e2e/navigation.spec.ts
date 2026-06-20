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

// The Work hub (2026-06) consolidates the former Beads / Goals / Schedule rail surfaces
// into one right-rail "Work" surface with Overview · Goals · Tasks · Schedule tabs.
// Work is the default-active right panel, so it's already open — clicking its rail icon
// when active would TOGGLE it closed, so only click when it's not the active surface.
// In the narrow right panel the DS responsive Tabs collapse the role="tab" strip into a
// native <select.pl-tabs__select>; pick the tab through it.
const TAB_VALUE: Record<string, string> = { Overview: "overview", Goals: "goals", Tasks: "tasks", Schedule: "schedule" };
async function openWorkTab(page, tab: string) {
  const workBtn = page.locator(".pl-rail--right").getByRole("button", { name: "Work", exact: true });
  const cls = (await workBtn.getAttribute("class")) ?? "";
  if (!cls.includes("--active")) await workBtn.click();
  await page.locator(".pl-tabs__select").first().selectOption(TAB_VALUE[tab]);
}

test("Work → Schedule tab lists scheduled jobs", async ({ page }) => {
  await openWorkTab(page, "Schedule");
  await expect(page.getByRole("heading", { name: "Schedule" })).toBeVisible();
  await expect(page.getByText("Summarize overnight activity")).toBeVisible();
});

test("Work → Goals tab lists active goals", async ({ page }) => {
  // Goals folded into the Work hub's Goals tab (still renders the GoalsPanel verbatim).
  await openWorkTab(page, "Goals");
  await expect(page.getByRole("heading", { name: "Goals" })).toBeVisible();
  await expect(page.getByText("All tests pass")).toBeVisible();
});

test("Work → Tasks tab lists beads issues (query-backed)", async ({ page }) => {
  // The tab is labeled "Tasks" but the panel content is still Beads (TanStack Query /
  // Suspense, ADR 0013) — folded into the Work hub.
  await openWorkTab(page, "Tasks");
  await expect(page.getByRole("heading", { name: "Beads" })).toBeVisible();
  await expect(page.getByText("Wire the telemetry rollup")).toBeVisible();
});

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

  // Installed (default tab) — both status groups + the enable toggle.
  await expect(page.getByText("Demo Plugin", { exact: false })).toBeVisible();
  await expect(page.getByText("Zzz Disabled", { exact: false })).toBeVisible();
  await page.locator(".subagent-row", { hasText: "Zzz Disabled" })
    .getByRole("button", { name: "Enable" }).click();
  await expect(page.locator(".plugin-hint")).toContainText("Zzz Disabled enabled");

  // Config folded in (ADR 0059, bd-23a.3) — Demo Plugin's row exposes Configure → fields inline.
  const demoRow = page.locator(".plugin-row-wrap", { hasText: "Demo Plugin" });
  await demoRow.getByRole("button", { name: "Configure" }).click();
  await expect(demoRow.locator('.plugin-row-config .setting-row[data-key="demo.greeting"]')).toBeVisible();

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
  // Its panel renders — the Work hub's Tabs strip is present in the bottom dock.
  await expect(page.locator(".pl-appshell__bottom .pl-tabs").getByRole("tab", { name: "Overview", exact: true })).toBeVisible();
});

test("mobile shell: bottom quick-bar + unified header drawer (ADR 0035 S4)", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/app/", { waitUntil: "load" });

  // Below the breakpoint: the desktop rails are gone; a bottom quick-bar appears.
  await expect(page.locator(".pl-rail")).toHaveCount(0);
  const bar = page.locator(".pl-mobilenav");
  await expect(bar).toBeVisible();

  // A default quick-bar surface switches the single active surface.
  await bar.getByRole("button", { name: "Knowledge", exact: true }).click();

  // The DS bottom-bar "More" is hidden (2026-06-18 IA pass) — the unified mobile
  // "more" is the header hamburger's app drawer, which on mobile also lists surfaces.
  await expect(bar.getByRole("button", { name: "All surfaces" })).toBeHidden();
  await page.getByTestId("header-menu").click();
  const drawer = page.getByTestId("app-drawer");
  await expect(drawer).toBeVisible();
  await drawer.getByRole("button", { name: "Work", exact: true }).click();
  await expect(drawer).toHaveCount(0); // picking a surface closes the drawer
});
