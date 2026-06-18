import { expect, test } from "@playwright/test";

// Settings IA (2026-06-18 pass): WORKSPACE settings live in the rail "Settings"
// surface (no scope toggle); GLOBAL settings open from the header hamburger → app
// drawer → an overlay dialog. Each section renders GET /api/settings/schema groups
// and saves via POST /api/settings (auto-reload).

// The rail Settings surface — Workspace-only now.
async function openSettings(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Settings", exact: true }).click();
}

// Global settings open from the header drawer; `item` picks the drawer entry
// ("Global settings" lands on Overview, "Telemetry" deep-links that section).
async function openGlobal(page, item = "Global settings") {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  const drawer = page.getByTestId("app-drawer");
  await expect(drawer).toBeVisible();
  await drawer.getByRole("button", { name: item, exact: true }).click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
}

// Click a section in a sidenav (the rail's, or — scoped — the overlay's).
async function section(page, name, scope = ".pl-sidenav") {
  await page.locator(scope).getByRole("tab", { name, exact: true }).click();
}

// Field groups are collapsed by default — open every group so the fields are visible.
async function expandAllGroups(page) {
  await expect(page.locator(".pl-accordion__trigger").first()).toBeVisible();
  const triggers = page.locator(".pl-accordion__trigger");
  for (let i = 0; i < (await triggers.count()); i++) {
    const t = triggers.nth(i);
    if ((await t.getAttribute("aria-expanded")) !== "true") await t.click();
  }
}

test("Workspace settings live in the rail (no scope toggle)", async ({ page }) => {
  await openSettings(page);
  // No Global/Workspace segmented toggle anymore — the rail is Workspace directly.
  await expect(page.locator(".pl-tabs--segmented")).toHaveCount(0);
  expect(await page.locator(".pl-sidenav").locator("button").allTextContents()).toEqual([
    "Identity",
    "Model & Routing",
    "Tools",
    "MCP",
    "Subagents",
    "Skills",
    "Middleware",
    "Memory",
    "System",
    "Theme",
    "Plugins",
  ]);
  await section(page, "System");
  await expect(page.locator(".pl-accordion__title").first()).toBeVisible();
  expect(await page.locator(".pl-accordion__title").allTextContents()).toEqual(["Compaction", "Runtime"]);
});

test("Global settings open from the header drawer → overlay (Overview · Configuration · Fleet · Telemetry · Commons)", async ({ page }) => {
  await openGlobal(page);
  const sidenav = page.locator(".settings-overlay .pl-sidenav");
  expect(await sidenav.locator("button").allTextContents()).toEqual([
    "Overview",
    "Configuration",
    "Fleet",
    "Telemetry",
    "Commons",
  ]);
  // Fleet section shows the agents panel; Telemetry renders the dashboard.
  await sidenav.getByRole("tab", { name: "Fleet", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
  await sidenav.getByRole("tab", { name: "Telemetry", exact: true }).click();
  await expect(page.getByTestId("telemetry-surface")).toBeVisible();
});

test("the drawer's Telemetry item deep-links the Global Telemetry section", async ({ page }) => {
  await openGlobal(page, "Telemetry");
  await expect(page.getByTestId("telemetry-surface")).toBeVisible();
});

test("Workspace ▸ Model & Routing shows the agent's Model + Routing fields", async ({ page }) => {
  await openSettings(page);
  await section(page, "Model & Routing");
  await expect(page.locator(".pl-accordion__title").first()).toBeVisible();
  expect(await page.locator(".pl-accordion__title").allTextContents()).toEqual(["Model", "Routing"]);
  await expandAllGroups(page);
  await expect(page.locator('.setting-row[data-key="routing.aux_model"] input')).toHaveValue("protolabs/fast");
  await expect(page.locator('.setting-row[data-key="model.api_key"] input')).toHaveAttribute("placeholder", /set/);
});

test("editing an Agent setting enables save and round-trips", async ({ page }) => {
  await openSettings(page);
  await section(page, "Model & Routing");
  await expandAllGroups(page);
  const save = page.getByRole("button", { name: /Save & apply/ });
  await expect(save).toBeDisabled();
  await page.locator('.setting-row[data-key="routing.aux_model"] input').fill("protolabs/turbo");
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.locator(".settings-status")).toContainText("config saved");
});

test("a restart-flagged System field shows the restart banner", async ({ page }) => {
  await openSettings(page);
  await section(page, "System");
  await expect(page.locator(".settings-banner")).toHaveCount(0);
  await expandAllGroups(page);
  await page.locator('.setting-row[data-key="runtime.autostart_on_boot"] .pl-switch').click();
  await expect(page.locator(".settings-banner")).toContainText("restart");
});

// ADR 0047 hybrid layered settings — per-agent settings show every field with an
// inheritance badge; an overridden host-scoped field offers reset-to-inherited.
test("per-agent settings show ADR 0047 inheritance badges + reset", async ({ page }) => {
  await openSettings(page);
  await section(page, "Model & Routing");
  await expandAllGroups(page);
  await expect(page.locator('.setting-row[data-key="model.name"] .setting-inheritance')).toContainText(
    "inherited from Global",
  );
  await expect(page.locator('.setting-row[data-key="routing.aux_model"] .setting-inheritance')).toContainText(
    "inherited from default",
  );
  const temp = page.locator('.setting-row[data-key="model.temperature"]');
  await expect(temp.locator(".setting-inheritance")).toContainText("overridden here");
  await temp.getByRole("button", { name: /Reset to inherited/ }).click();
  await expect(page.locator(".settings-status")).toContainText("inherited");
  await expect(page.locator('.setting-row[data-key="routing.fallback_models"] .setting-inheritance')).toHaveCount(0);
});

test("Configuration (Global) edits the host-scoped subset and saves to the host layer", async ({ page }) => {
  await openGlobal(page);
  await section(page, "Configuration", ".settings-overlay .pl-sidenav");
  await expect(page.locator(".settings-banner").first()).toContainText("box-shared");
  await expandAllGroups(page);
  await expect(page.locator('.setting-row[data-key="model.name"]')).toBeVisible();
  await expect(page.locator('.setting-row[data-key="model.api_key"]')).toHaveCount(0);
  await expect(page.locator('.setting-row[data-key="routing.fallback_models"]')).toHaveCount(0);
  await page.locator('.setting-row[data-key="routing.aux_model"] input').fill("protolabs/host-fast");
  const save = page.getByRole("button", { name: /Save & apply/ }).first();
  await save.click();
  await expect(page.locator(".settings-status").first()).toContainText("(host)");
});
