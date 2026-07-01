import { expect, test } from "@playwright/test";

// The Delegates panel (ADR 0025) is a built-in core surface with its own top-level
// Settings ▸ Workspace ▸ Delegates section (ADR 0048): it lists the configured delegates
// (GET /api/delegates), and an Add form with a type picker driven by GET
// /api/delegate-types. Mocked endpoints in e2e/mock-server.mjs.

async function openIntegrations(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Delegates", exact: true }).click();
}

test("lists configured delegates with type + secret badges", async ({ page }) => {
  await openIntegrations(page);
  // The panel title now renders in the shared SettingsSubPanel header (#1545) as a DS
  // PanelHeader heading (outside .delegates-section); the rows stay inside it.
  await expect(page.getByRole("heading", { name: "Delegates" })).toBeVisible();
  const panel = page.locator(".delegates-section");
  const row = panel.locator(".subagent-row", { hasText: "opus" });
  await expect(row).toBeVisible();
  await expect(row.getByText("openai", { exact: true })).toBeVisible(); // DS Badge (#832)
  await expect(row.getByText("secret set")).toBeVisible();
  // Health prober (PR4): the cached status surfaces as a DS StatusDot.
  await expect(row.locator(".pl-dot--success")).toBeVisible();
});

test("Add opens a dialog with a type picker and a schema-driven form", async ({ page }) => {
  await openIntegrations(page);
  await page.locator(".delegates-section").getByRole("button", { name: /Add delegate/ }).click();

  // The add/edit form is a dialog now (it used to render inline in the panel).
  const dialog = page.getByRole("dialog", { name: "Add a delegate" });
  await expect(dialog).toBeVisible();

  // Three type cards from /api/delegate-types (DS RadioCard).
  await expect(dialog.locator(".pl-radiocard")).toHaveCount(3);

  // Default type (a2a) renders its URL field; switching to acp renders Command.
  await expect(dialog.getByText("URL", { exact: false })).toBeVisible();
  await dialog.locator(".pl-radiocard", { hasText: "Coding agent" }).click();
  await expect(dialog.getByText("Command", { exact: false })).toBeVisible();
  await expect(dialog.getByText("Workdir", { exact: false })).toBeVisible();

  // The coding-agent preset picker (from the canonical /api/acp-agents catalog) fills
  // Command + Args when an agent is chosen.
  await expect(dialog.locator("#acp-preset")).toBeVisible();
  // DropdownSelect (#274): open the trigger, then pick the portaled menu item (rendered at
  // document.body, so it's page-scoped, not inside `dialog`).
  await dialog.locator("#acp-preset").click();
  await page.getByRole("menuitemradio", { name: "Claude Code" }).click();
  await expect(dialog.locator("#del-command")).toHaveValue("npx");
});
