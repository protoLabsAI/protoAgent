import { expect, test } from "@playwright/test";

// Settings ▸ Workspace ▸ MCP: add/remove MCP servers inline (hot reload). The mock
// runtime status ships one server (echo); add/remove hit the mocked /api/mcp/servers.

test("MCP tab lists servers and adds one inline", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "MCP", exact: true }).click();

  await expect(page.getByRole("heading", { name: "MCP servers" })).toBeVisible();
  await expect(page.getByText("echo · stdio")).toBeVisible();

  // Add a stdio server inline.
  await page.getByRole("button", { name: /Add server/ }).click();
  await page.getByPlaceholder("name (e.g. echo)").fill("mathy");
  await page.getByPlaceholder("command (e.g. python)").fill("python");
  await page.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.locator(".plugin-hint")).toContainText("Connected mathy");
});

test("MCP tab imports servers from pasted JSON", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "MCP", exact: true }).click();

  await page.getByRole("button", { name: /Add server/ }).click();
  await page.getByRole("button", { name: "Paste JSON", exact: true }).click();
  await page.locator("textarea.mcp-json").fill(
    '{"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]}, "weather": {"url": "https://x/mcp"}}}',
  );
  await page.getByRole("button", { name: "Import", exact: true }).click();
  await expect(page.locator(".plugin-hint")).toContainText("Imported 2 servers: filesystem, weather");
});

test("MCP catalog quick-adds a common server that needs an input", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "MCP", exact: true }).click();

  await page.getByRole("button", { name: /Browse common servers/ }).click();
  // Scope to the dialog — the panel behind it has its own "Add server" trigger. The
  // catalog search field renders only in the dialog's browse view (a clean open signal).
  const dialog = page.getByRole("dialog", { name: "Add a common MCP server" });
  await expect(dialog.getByLabel("search MCP servers")).toBeVisible();

  // Filesystem needs a path → picking it opens a configure step before adding.
  await dialog.locator(".mcp-catalog-card", { hasText: "Filesystem" }).getByRole("button", { name: "Add" }).click();
  await dialog.getByLabel("Allowed directory").fill("/data");
  await dialog.getByRole("button", { name: "Add server", exact: true }).click();

  // A successful add closes the dialog, hints, and the server joins the list.
  await expect(page.getByLabel("search MCP servers")).toHaveCount(0);
  await expect(page.locator(".plugin-hint")).toContainText("Connected filesystem");
  await expect(page.getByText("filesystem · stdio")).toBeVisible();
});

test("MCP catalog adds a no-input server in one click", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "MCP", exact: true }).click();

  await page.getByRole("button", { name: /Browse common servers/ }).click();
  const dialog = page.getByRole("dialog", { name: "Add a common MCP server" });
  await expect(dialog.getByLabel("search MCP servers")).toBeVisible();

  // Memory needs no config → one click adds it and closes the dialog.
  await dialog.locator(".mcp-catalog-card", { hasText: "Memory" }).getByRole("button", { name: "Add" }).click();
  await expect(page.getByLabel("search MCP servers")).toHaveCount(0);
  await expect(page.locator(".plugin-hint")).toContainText("Connected memory");
  await expect(page.getByText("memory · stdio")).toBeVisible();
});
