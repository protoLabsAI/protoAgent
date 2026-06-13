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

test("schedule is a right-rail panel that lists scheduled jobs", async ({ page }) => {
  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Schedule" })).toBeVisible();
  await expect(page.getByText("Summarize overnight activity")).toBeVisible();
});

test("goals tab in the right sidebar lists active goals", async ({ page }) => {
  // Goals moved out of Studio into the right sidebar (Notes / Beads / Goals).
  await page.getByRole("button", { name: "Goals", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Goals" })).toBeVisible();
  await expect(page.getByText("All tests pass")).toBeVisible();
});

test("beads tab in the right sidebar lists issues (query-backed)", async ({ page }) => {
  // Beads (TanStack Query / Suspense, ADR 0013) is the default-active right panel, and clicking an
  // already-open panel now toggles it closed — so switch to Goals first, then back to Beads.
  await page.locator(".pl-rail--right").getByRole("button", { name: "Goals", exact: true }).click();
  await page.locator(".pl-rail--right").getByRole("button", { name: "Beads", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Beads" })).toBeVisible();
  await expect(page.getByText("Wire the telemetry rollup")).toBeVisible();
});

test("workspace settings: identity lands, then tools and MCP sections", async ({ page }) => {
  // The agent makeup folded into Settings ▸ Workspace (ADR 0048 S-C).
  await page.locator(".pl-rail").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs--segmented").getByRole("button", { name: "Workspace", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Identity" })).toBeVisible(); // landing section
  await expect(page.getByTestId("identity-name")).toBeVisible();

  await page.locator(".pl-sidenav").getByRole("tab", { name: "Tools", exact: true }).click();
  await expect(page.getByText("web_search")).toBeVisible();

  await page.locator(".pl-sidenav").getByRole("tab", { name: "MCP", exact: true }).click();
  await expect(page.getByText("echo · stdio")).toBeVisible(); // MCP server
});

test("plugins section: Local / Market / Download tabs", async ({ page }) => {
  await page.locator(".pl-rail").getByRole("button", { name: "Plugins", exact: true }).click();

  // Local (default tab) — both status groups + the enable toggle.
  await expect(page.getByText("Demo Plugin", { exact: false })).toBeVisible();
  await expect(page.getByText("Zzz Disabled", { exact: false })).toBeVisible();
  await page.locator(".subagent-row", { hasText: "Zzz Disabled" })
    .getByRole("button", { name: "Enable" }).click();
  await expect(page.locator(".plugin-hint")).toContainText("Zzz Disabled enabled");

  // Market tab — discovery links.
  await page.locator(".pl-tabs").getByRole("tab", { name: "Market", exact: true }).click();
  await expect(page.getByRole("link", { name: /Browse the directory/ })).toBeVisible();

  // Download tab — install from a git URL.
  await page.locator(".pl-tabs").getByRole("tab", { name: "Download", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Install from a git URL" })).toBeVisible();
});

test("UI state persists across reload (ADR 0035 S1 — Zustand persist)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Move off the defaults: a left surface (Plugins) + a non-default right-panel tab (Goals — Beads
  // is the default, and clicking the default-active one would toggle it closed).
  await page.locator(".pl-rail").getByRole("button", { name: "Plugins", exact: true }).click();
  await page.locator(".pl-rail--right").getByRole("button", { name: "Goals", exact: true }).click();

  // Reload — the persisted store restores both, instead of snapping back to Chat/Notes.
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").getByRole("button", { name: "Plugins", exact: true })).toHaveClass(/active/);
  await expect(page.locator(".pl-rail--right").getByRole("button", { name: "Goals", exact: true })).toHaveClass(/active/);
});


test("right-click a rail surface opens a context menu that moves it (ADR 0036)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const rightRail = page.locator(".pl-rail--right");
  const leftRail = page.locator(".pl-rail:not(.pl-rail--right)");

  // Beads starts on the right rail.
  await expect(rightRail.getByRole("button", { name: "Beads", exact: true })).toBeVisible();

  // Right-click it → the context menu opens with the move item.
  await rightRail.getByRole("button", { name: "Beads", exact: true }).click({ button: "right" });
  const menu = page.locator(".pl-menu");
  await expect(menu).toBeVisible();
  await menu.getByText("Move to left rail").click();

  // Beads moved to the left rail (store-backed → persists, but checking the move is enough here).
  await expect(leftRail.getByRole("button", { name: "Beads", exact: true })).toBeVisible();
  await expect(rightRail.getByRole("button", { name: "Beads", exact: true })).toHaveCount(0);
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

test("mobile shell: bottom quick-bar + hamburger drawer (ADR 0035 S4)", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/app/", { waitUntil: "load" });

  // Below the breakpoint: the desktop rails are gone; a bottom quick-bar appears.
  await expect(page.locator(".pl-rail")).toHaveCount(0);
  const bar = page.locator(".pl-mobilenav");
  await expect(bar).toBeVisible();

  // A default quick-bar surface switches the single active surface.
  await bar.getByRole("button", { name: "Knowledge", exact: true }).click();

  // The hamburger opens a drawer with the full surface list.
  await bar.getByRole("button", { name: "All surfaces" }).click();
  const drawer = page.getByRole("dialog", { name: "Surfaces" });
  await expect(drawer).toBeVisible();
  await drawer.getByRole("button", { name: "Beads", exact: true }).click();
  await expect(drawer).toHaveCount(0); // picking closes it
});
