import { expect, test } from "@playwright/test";

// Settings IA (ADR 0048): scope is the primary axis — TWO homes, each with its own
// section sub-nav. 🖥 Host / App (box-shared) and 🧩 Workspace (the focused agent).
// Each section renders GET /api/settings/schema groups for its category and saves via
// POST /api/settings (auto-reload). The Agent rail surface still hosts the agent
// makeup until the S-C collapse.

async function openSettings(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
}

// Click a home (the segmented scope toggle in the SideNav header, role=button) or a
// section (the DS SideNav rail, role=tab). Names are unique across both.
async function tab(page, name) {
  const home = page.locator(".pl-tabs--segmented").getByRole("button", { name, exact: true });
  if (await home.count()) {
    await home.click();
    return;
  }
  await page.locator(".pl-sidenav").getByRole("tab", { name, exact: true }).click();
}

// Field groups are collapsed by default (the operator expands as needed) — open
// every group so the fields under them are visible/interactable in a test. Waits
// for the suspense load, then toggles only the still-collapsed triggers.
async function expandAllGroups(page) {
  await expect(page.locator(".pl-accordion__trigger").first()).toBeVisible();
  const triggers = page.locator(".pl-accordion__trigger");
  for (let i = 0; i < (await triggers.count()); i++) {
    const t = triggers.nth(i);
    if ((await t.getAttribute("aria-expanded")) !== "true") await t.click();
  }
}

test("Settings is a two-home shell (Host / App · Workspace)", async ({ page }) => {
  await openSettings(page);
  // The two scope homes (ADR 0048) — the segmented toggle pinned atop the SideNav.
  expect(await page.locator(".pl-tabs--segmented").locator("button").allTextContents()).toEqual([
    "Host / App",
    "Workspace",
  ]);
  // The Host / App home's sections (the default home) — DS SideNav rail.
  expect(await page.locator(".pl-sidenav").locator("button").allTextContents()).toEqual([
    "Overview",
    "Host config",
    "Fleet",
    "Telemetry",
    "Commons",
  ]);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible(); // default section
  // The Workspace home → the focused agent's makeup + settings (ADR 0048 fold).
  await tab(page, "Workspace");
  expect(await page.locator(".pl-sidenav").locator("button").allTextContents()).toEqual([
    "Identity",
    "Settings",
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
  await tab(page, "System");
  // Field groups are collapsible accordions (DS 0.29); titles render in the trigger.
  await expect(page.locator(".pl-accordion__title").first()).toBeVisible(); // wait for the suspense load
  expect(await page.locator(".pl-accordion__title").allTextContents()).toEqual(["Compaction", "Runtime"]);
});

test("Workspace ▸ Settings shows the agent's Model + Routing fields", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs--segmented").getByRole("button", { name: "Workspace", exact: true }).click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Settings", exact: true }).click();
  // Model + Routing render here (the agent makeup folded into Workspace, ADR 0048).
  await expect(page.locator(".pl-accordion__title").first()).toBeVisible(); // wait for the suspense load
  expect(await page.locator(".pl-accordion__title").allTextContents()).toEqual(["Model", "Routing"]);
  await expandAllGroups(page); // groups are collapsed by default — open to reach the fields
  const aux = page.locator('.setting-row[data-key="routing.aux_model"] input');
  await expect(aux).toHaveValue("protolabs/fast");
  const key = page.locator('.setting-row[data-key="model.api_key"] input');
  await expect(key).toHaveAttribute("placeholder", /set/);
});

test("editing an Agent setting enables save and round-trips", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs--segmented").getByRole("button", { name: "Workspace", exact: true }).click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Settings", exact: true }).click();
  await expandAllGroups(page); // groups are collapsed by default — open to reach the fields
  const save = page.getByRole("button", { name: /Save & apply/ });
  await expect(save).toBeDisabled();
  await page.locator('.setting-row[data-key="routing.aux_model"] input').fill("protolabs/turbo");
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.locator(".settings-status")).toContainText("config saved");
});

test("a restart-flagged System field shows the restart banner", async ({ page }) => {
  await openSettings(page);
  await tab(page, "Workspace");
  await tab(page, "System");
  await expect(page.locator(".settings-banner")).toHaveCount(0);
  await expandAllGroups(page); // groups are collapsed by default — open to reach the fields
  await page.locator('.setting-row[data-key="runtime.autostart_on_boot"] input[type="checkbox"]').check();
  await expect(page.locator(".settings-banner")).toContainText("restart");
});

// ADR 0047 hybrid layered settings — per-agent Settings shows every field with an
// inheritance badge; an overridden host-scoped field offers reset-to-inherited.
test("per-agent settings show ADR 0047 inheritance badges + reset", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs--segmented").getByRole("button", { name: "Workspace", exact: true }).click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Settings", exact: true }).click();
  await expandAllGroups(page); // groups are collapsed by default — open to reach the fields
  // model.name inherits from the host layer.
  await expect(
    page.locator('.setting-row[data-key="model.name"] .setting-inheritance'),
  ).toContainText("inherited from Host");
  // routing.aux_model inherits the App default.
  await expect(
    page.locator('.setting-row[data-key="routing.aux_model"] .setting-inheritance'),
  ).toContainText("inherited from default");
  // model.temperature is host-scoped but overridden here → reset affordance.
  const temp = page.locator('.setting-row[data-key="model.temperature"]');
  await expect(temp.locator(".setting-inheritance")).toContainText("overridden here");
  await temp.getByRole("button", { name: /Reset to inherited/ }).click();
  await expect(page.locator(".settings-status")).toContainText("inherited");
  // routing.fallback_models is a plain agent setting — no badge.
  await expect(
    page.locator('.setting-row[data-key="routing.fallback_models"] .setting-inheritance'),
  ).toHaveCount(0);
});

test("Host config edits the host-scoped subset and saves to the host layer", async ({ page }) => {
  await openSettings(page);
  await tab(page, "Host config"); // Host / App is the default home
  await expect(page.locator(".settings-banner").first()).toContainText("box-shared");
  await expandAllGroups(page); // groups are collapsed by default — open to reach the fields
  // Only host-scoped fields appear — model.name (host) is here; the agent-scoped
  // model.api_key + routing.fallback_models are NOT.
  await expect(page.locator('.setting-row[data-key="model.name"]')).toBeVisible();
  await expect(page.locator('.setting-row[data-key="model.api_key"]')).toHaveCount(0);
  await expect(page.locator('.setting-row[data-key="routing.fallback_models"]')).toHaveCount(0);
  // A save writes to the host layer (the mock echoes the layer).
  await page.locator('.setting-row[data-key="routing.aux_model"] input').fill("protolabs/host-fast");
  const save = page.getByRole("button", { name: /Save & apply/ }).first();
  await save.click();
  await expect(page.locator(".settings-status").first()).toContainText("(host)");
});
