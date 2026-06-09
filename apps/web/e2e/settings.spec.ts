import { expect, test } from "@playwright/test";

// Settings are decentralized: Agent settings live in the Agent view, Memory in the
// Knowledge view, and the central Settings surface keeps only the cross-cutting tabs
// (Overview · Telemetry · Plugins · System). Each renders GET /api/settings/schema
// groups for its category and saves via POST /api/settings (auto-reload).

async function openSettings(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
}

async function tab(page, name) {
  await page.locator(".pl-tabs").getByRole("tab", { name, exact: true }).click();
}

test("central Settings is just the cross-cutting tabs", async ({ page }) => {
  await openSettings(page);
  expect(await page.locator(".pl-tabs button").allTextContents()).toEqual([
    "Overview",
    "Telemetry",
    "Plugins",
    "System",
  ]);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible(); // default
  // System holds the runtime/perf knobs (Compaction + Runtime here).
  await tab(page, "System");
  await expect(page.locator(".settings-group-title").first()).toBeVisible(); // wait for the suspense load
  expect(await page.locator(".settings-group-title").allTextContents()).toEqual(["Compaction", "Runtime"]);
});

test("Agent settings live in the Agent view's Settings tab", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Agent", exact: true }).click();
  await page.locator(".pl-tabs").getByRole("tab", { name: "Settings", exact: true }).click();
  // Model + Routing render here, not in the central Settings surface.
  await expect(page.locator(".settings-group-title").first()).toBeVisible(); // wait for the suspense load
  expect(await page.locator(".settings-group-title").allTextContents()).toEqual(["Model", "Routing"]);
  const aux = page.locator('.setting-row[data-key="routing.aux_model"] input');
  await expect(aux).toHaveValue("protolabs/fast");
  const key = page.locator('.setting-row[data-key="model.api_key"] input');
  await expect(key).toHaveAttribute("placeholder", /set/);
});

test("editing an Agent setting enables save and round-trips", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Agent", exact: true }).click();
  await page.locator(".pl-tabs").getByRole("tab", { name: "Settings", exact: true }).click();
  const save = page.getByRole("button", { name: /Save & apply/ });
  await expect(save).toBeDisabled();
  await page.locator('.setting-row[data-key="routing.aux_model"] input').fill("protolabs/turbo");
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.locator(".settings-status")).toContainText("config saved");
});

test("a restart-flagged System field shows the restart banner", async ({ page }) => {
  await openSettings(page);
  await tab(page, "System");
  await expect(page.locator(".settings-banner")).toHaveCount(0);
  await page.locator('.setting-row[data-key="runtime.autostart_on_boot"] input[type="checkbox"]').check();
  await expect(page.locator(".settings-banner")).toContainText("restart");
});
