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
    "Agents",
    "Theme",
    "Telemetry",
    "Plugins",
    "System",
    "Host defaults", // ADR 0047 — the host-scoped subset, edited at the host layer
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

// ADR 0047 hybrid layered settings — per-agent Settings shows every field with an
// inheritance badge; an overridden host-scoped field offers reset-to-inherited.
test("per-agent settings show ADR 0047 inheritance badges + reset", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Agent", exact: true }).click();
  await page.locator(".pl-tabs").getByRole("tab", { name: "Settings", exact: true }).click();
  await expect(page.locator(".settings-group-title").first()).toBeVisible();
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

test("Host defaults view edits the host-scoped subset and saves to the host layer", async ({ page }) => {
  await openSettings(page);
  await tab(page, "Host defaults");
  await expect(page.locator(".settings-banner").first()).toContainText("box-shared");
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
