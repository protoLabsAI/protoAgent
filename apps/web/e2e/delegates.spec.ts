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

test("env editor serializes rows, a per-row secret, and env_remove into the payload", async ({ page }) => {
  await openIntegrations(page);
  await page.locator(".delegates-section").getByRole("button", { name: /Add delegate/ }).click();
  const dialog = page.getByRole("dialog", { name: "Add a delegate" });
  await expect(dialog).toBeVisible();
  await dialog.getByPlaceholder("e.g. opus").fill("gateway");

  // The env editor (#2114) is present on every type (default a2a).
  await expect(dialog.getByText("Environment", { exact: true })).toBeVisible();

  // Add two rows.
  const addVar = dialog.getByRole("button", { name: "Add variable" });
  await addVar.click();
  await addVar.click();
  const rows = dialog.locator(".delegate-env-row");
  await expect(rows).toHaveCount(2);

  // Row 0: a plain var. Row 1: a var routed to secrets.yaml via the per-row toggle.
  await rows.nth(0).getByLabel("env name").fill("ANTHROPIC_BASE_URL");
  await rows.nth(0).getByLabel("env value").fill("https://gw/v1");
  await rows.nth(1).getByLabel("env name").fill("ANTHROPIC_AUTH_TOKEN");
  await rows.nth(1).getByLabel("env value").fill("sk-secret");
  // Toggle the row secret — its title flips from "Store as secret" to "Secret — …".
  await rows.nth(1).getByRole("button", { name: "Store as secret" }).click();
  await expect(rows.nth(1).getByRole("button", { name: /^Secret —/ })).toBeVisible();

  // env_remove is a comma/newline list editor.
  await dialog.getByPlaceholder("PROTOAGENT_, A2A_AUTH_TOKEN").fill("PROTOAGENT_, A2A_AUTH_TOKEN");

  const reqP = page.waitForRequest((r) => r.url().endsWith("/api/delegates") && r.method() === "POST");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  const body = (await reqP).postDataJSON();

  expect(body.env).toMatchObject({ ANTHROPIC_BASE_URL: "https://gw/v1", ANTHROPIC_AUTH_TOKEN: "sk-secret" });
  expect(body.env_secret).toContain("ANTHROPIC_AUTH_TOKEN");
  expect(body.env_secret).not.toContain("ANTHROPIC_BASE_URL");
  expect(body.env_remove).toEqual(["PROTOAGENT_", "A2A_AUTH_TOKEN"]);
});

test("editing an env-carrying delegate seeds masked secrets + env_remove", async ({ page }) => {
  await openIntegrations(page);
  const row = page.locator(".delegates-section .subagent-row", { hasText: "coder" });
  await row.getByRole("button", { name: "Edit" }).click();
  const dialog = page.getByRole("dialog", { name: "Edit coder" });
  await expect(dialog).toBeVisible();

  const rows = dialog.locator(".delegate-env-row");
  await expect(rows).toHaveCount(2);
  // Non-secret row shows its value; the secret row seeds set-but-masked (toggle on,
  // value never echoed to the client — it came back as "***").
  await expect(rows.nth(0).getByLabel("env name")).toHaveValue("ANTHROPIC_BASE_URL");
  await expect(rows.nth(0).getByLabel("env value")).toHaveValue("https://gw/v1");
  await expect(rows.nth(1).getByLabel("env name")).toHaveValue("ANTHROPIC_AUTH_TOKEN");
  await expect(rows.nth(1).getByRole("button", { name: /^Secret —/ })).toBeVisible();
  // env_remove prefilled as a comma-joined list.
  await expect(dialog.getByPlaceholder("PROTOAGENT_, A2A_AUTH_TOKEN")).toHaveValue("PROTOAGENT_, A2A_AUTH_TOKEN");
});
