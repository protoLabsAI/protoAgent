import { expect, test } from "@playwright/test";

// The Delegates panel (ADR 0025) lives under Settings → Plugins: it lists the
// configured delegates (GET /api/delegates), and an Add form with a type picker
// driven by GET /api/delegate-types. Mocked endpoints in e2e/mock-server.mjs.

async function openIntegrations(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs").getByRole("tab", { name: "Plugins", exact: true }).click();
}

test("lists configured delegates with type + secret badges", async ({ page }) => {
  await openIntegrations(page);
  const panel = page.locator(".delegates-section");
  await expect(panel.getByText("Delegates", { exact: true })).toBeVisible();
  const row = panel.locator(".subagent-row", { hasText: "opus" });
  await expect(row).toBeVisible();
  await expect(row.getByText("openai", { exact: true })).toBeVisible(); // DS Badge (#832)
  await expect(row.getByText("secret set")).toBeVisible();
  // Health prober (PR4): the cached status surfaces as a dot.
  await expect(row.locator(".delegate-health.ok")).toBeVisible();
});

test("Add opens a type picker and a schema-driven form", async ({ page }) => {
  await openIntegrations(page);
  const panel = page.locator(".delegates-section");
  await panel.getByRole("button", { name: /Add delegate/ }).click();

  // Three type tiles from /api/delegate-types.
  const tiles = panel.locator(".delegate-type-tile");
  await expect(tiles).toHaveCount(3);

  // Default type (a2a) renders its URL field; switching to acp renders Command.
  await expect(panel.getByText("URL", { exact: false })).toBeVisible();
  await panel.locator(".delegate-type-tile", { hasText: "Coding agent" }).click();
  await expect(panel.getByText("Command", { exact: false })).toBeVisible();
  await expect(panel.getByText("Workdir", { exact: false })).toBeVisible();
});
