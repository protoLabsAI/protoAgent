import { expect, test } from "@playwright/test";

// Settings ▸ Workspace ▸ MCP: add/remove MCP servers inline (hot reload). The mock
// runtime status ships one server (echo); add/remove hit the mocked /api/mcp/servers.

test("MCP tab lists servers and adds one inline", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-rail").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs--segmented").getByRole("button", { name: "Workspace", exact: true }).click();
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
  await page.locator(".pl-rail").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs--segmented").getByRole("button", { name: "Workspace", exact: true }).click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "MCP", exact: true }).click();

  await page.getByRole("button", { name: /Add server/ }).click();
  await page.getByRole("button", { name: "Paste JSON", exact: true }).click();
  await page.locator("textarea.mcp-json").fill(
    '{"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]}, "weather": {"url": "https://x/mcp"}}}',
  );
  await page.getByRole("button", { name: "Import", exact: true }).click();
  await expect(page.locator(".plugin-hint")).toContainText("Imported 2 servers: filesystem, weather");
});
