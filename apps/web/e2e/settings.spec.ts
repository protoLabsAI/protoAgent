import { expect, test } from "@playwright/test";

// The Settings surface renders GET /api/settings/schema generically, grouped
// into a category sub-nav (ADR 0020), saves changed fields via POST /api/settings
// (auto-reload), and flags fields that need a process restart.

async function openSettings(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "System", exact: true }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
}

async function category(page, name) {
  await page.locator(".settings-subnav").getByRole("button", { name, exact: true }).click();
}

test("groups are organized into a category sub-nav; Agent leads", async ({ page }) => {
  await openSettings(page);
  // Category sub-nav from the mock schema: Agent · Behavior · System.
  expect(await page.locator(".settings-subnav button").allTextContents()).toEqual([
    "Agent",
    "Behavior",
    "System",
  ]);
  // Agent is the default — only its sections show (Model + Routing), not System's.
  expect(await page.locator(".settings-group-title").allTextContents()).toEqual(["Model", "Routing"]);
  const aux = page.locator('.setting-row[data-key="routing.aux_model"] input');
  await expect(aux).toHaveValue("protolabs/fast");
  // Secret is never echoed — empty with a "set" placeholder.
  const key = page.locator('.setting-row[data-key="model.api_key"] input');
  await expect(key).toHaveValue("");
  await expect(key).toHaveAttribute("placeholder", /set/);
});

test("switching category reveals its sections + restart badge", async ({ page }) => {
  await openSettings(page);
  await category(page, "System");
  expect(await page.locator(".settings-group-title").allTextContents()).toEqual(["Runtime"]);
  const autostart = page.locator('.setting-row[data-key="runtime.autostart_on_boot"]');
  await expect(autostart.locator(".setting-restart")).toBeVisible();
});

test("editing enables save and round-trips", async ({ page }) => {
  await openSettings(page);
  const save = page.getByRole("button", { name: /Save & apply/ });
  await expect(save).toBeDisabled(); // nothing dirty yet

  const aux = page.locator('.setting-row[data-key="routing.aux_model"] input'); // Agent (default)
  await aux.fill("protolabs/turbo");
  await expect(save).toBeEnabled();
  await save.click();
  // Server (mock) reports saved + reloaded.
  await expect(page.locator(".settings-status")).toContainText("config saved");
});

test("toggling a restart-flagged field shows the restart banner", async ({ page }) => {
  await openSettings(page);
  await category(page, "System");
  await expect(page.locator(".settings-banner")).toHaveCount(0);
  await page.locator('.setting-row[data-key="runtime.autostart_on_boot"] input[type="checkbox"]').check();
  await expect(page.locator(".settings-banner")).toContainText("restart");
});
