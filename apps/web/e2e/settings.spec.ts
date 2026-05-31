import { expect, test } from "@playwright/test";

// The Settings surface renders GET /api/settings/schema generically, saves
// changed fields via POST /api/settings (auto-reload), and flags fields that
// need a process restart.

async function openSettings(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "System", exact: true }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
}

test("renders schema groups and current values", async ({ page }) => {
  await openSettings(page);
  // Grouped sections from the schema (scope to the group titles — some names
  // like "Runtime" also appear in the nav rail).
  // allTextContents (raw DOM text) — innerText would be uppercased by CSS.
  const titles = await page.locator(".settings-group-title").allTextContents();
  expect(titles).toEqual(["Model", "Routing", "Compaction", "Runtime"]);
  // A string field shows its current value.
  const aux = page.locator('.setting-row[data-key="routing.aux_model"] input');
  await expect(aux).toHaveValue("protolabs/fast");
  // The restart-flagged field shows the badge.
  const autostart = page.locator('.setting-row[data-key="runtime.autostart_on_boot"]');
  await expect(autostart.locator(".setting-restart")).toBeVisible();
  // Secret is never echoed — empty with a "set" placeholder.
  const key = page.locator('.setting-row[data-key="model.api_key"] input');
  await expect(key).toHaveValue("");
  await expect(key).toHaveAttribute("placeholder", /set/);
});

test("editing enables save and round-trips", async ({ page }) => {
  await openSettings(page);
  const save = page.getByRole("button", { name: /Save & apply/ });
  await expect(save).toBeDisabled(); // nothing dirty yet

  const aux = page.locator('.setting-row[data-key="routing.aux_model"] input');
  await aux.fill("protolabs/turbo");
  await expect(save).toBeEnabled();
  await save.click();
  // Server (mock) reports saved + reloaded.
  await expect(page.locator(".settings-status")).toContainText("config saved");
});

test("toggling a restart-flagged field shows the restart banner", async ({ page }) => {
  await openSettings(page);
  await expect(page.locator(".settings-banner")).toHaveCount(0);
  await page.locator('.setting-row[data-key="runtime.autostart_on_boot"] input[type="checkbox"]').check();
  await expect(page.locator(".settings-banner")).toContainText("restart");
});
