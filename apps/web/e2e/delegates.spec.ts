import { expect, test } from "@playwright/test";

// The Delegates panel (ADR 0025) is a built-in core surface with its own top-level
// Settings ▸ Workspace ▸ Delegates section (ADR 0048): it lists the configured delegates
// (GET /api/delegates), and an Add form with a type picker driven by GET
// /api/delegate-types. Mocked endpoints in e2e/mock-server.mjs.

async function openIntegrations(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Delegates", exact: true }).click();
}

test("lists configured delegates with type + secret badges", async ({ page }) => {
  await openIntegrations(page);
  const panel = page.locator(".delegates-section");
  await expect(panel.getByText("Delegates", { exact: true })).toBeVisible();
  const row = panel.locator(".subagent-row", { hasText: "opus" });
  await expect(row).toBeVisible();
  await expect(row.getByText("openai", { exact: true })).toBeVisible(); // DS Badge (#832)
  await expect(row.getByText("secret set")).toBeVisible();
  // Health prober (PR4): the cached status surfaces as a DS StatusDot.
  await expect(row.locator(".pl-dot--success")).toBeVisible();
});

test("Add opens a type picker and a schema-driven form", async ({ page }) => {
  await openIntegrations(page);
  const panel = page.locator(".delegates-section");
  await panel.getByRole("button", { name: /Add delegate/ }).click();

  // Three type cards from /api/delegate-types (DS RadioCard).
  const tiles = panel.locator(".pl-radiocard");
  await expect(tiles).toHaveCount(3);

  // Default type (a2a) renders its URL field; switching to acp renders Command.
  await expect(panel.getByText("URL", { exact: false })).toBeVisible();
  await panel.locator(".pl-radiocard", { hasText: "Coding agent" }).click();
  await expect(panel.getByText("Command", { exact: false })).toBeVisible();
  await expect(panel.getByText("Workdir", { exact: false })).toBeVisible();

  // The coding-agent preset picker (from the canonical /api/acp-agents catalog) fills
  // Command + Args when an agent is chosen.
  await expect(panel.locator("#acp-preset")).toBeVisible();
  // DropdownSelect (#274): open the trigger, then pick the portaled menu item (rendered at
  // document.body, so it's page-scoped, not inside `panel`).
  await panel.locator("#acp-preset").click();
  await page.getByRole("menuitemradio", { name: "Claude Code" }).click();
  await expect(panel.locator("#del-command")).toHaveValue("npx");
});
