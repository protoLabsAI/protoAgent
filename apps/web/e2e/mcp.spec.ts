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
  await expect(page.locator(".pl-toast", { hasText: "Connected mathy" })).toBeVisible();
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
  await expect(page.locator(".pl-toast", { hasText: "Imported 2 servers: filesystem, weather" })).toBeVisible();
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
  await expect(page.locator(".pl-toast", { hasText: "Connected filesystem" })).toBeVisible();
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
  await expect(page.locator(".pl-toast", { hasText: "Connected memory" })).toBeVisible();
  await expect(page.getByText("memory · stdio")).toBeVisible();
});

// Box-commons sharing (ADR 0041): when the agent is layered, each server shows a
// commons/private tier badge and a share / unshare action. (Runs last — the seed
// replaces the MCP roster.)
test("MCP servers show tier badges and share / unshare", async ({ page }) => {
  await page.request.post("/api/__test__/mcp/layered");
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "MCP", exact: true }).click();

  const sharedRow = page.locator(".table-row", { hasText: "shared-fs" });
  const localRow = page.locator(".table-row", { hasText: "local-fs" });
  await expect(sharedRow.getByText("commons")).toBeVisible();
  await expect(localRow.getByText("private")).toBeVisible();

  // Share the private server → it joins the commons (badge flips).
  await page.getByRole("button", { name: "share local-fs" }).click();
  await expect(localRow.getByText("commons")).toBeVisible();

  // Unshare a commons server → confirm → it goes private again.
  await page.getByRole("button", { name: "unshare shared-fs" }).click();
  await page.getByRole("dialog", { name: "Unshare from the box commons?" })
    .getByRole("button", { name: "Unshare", exact: true }).click();
  await expect(sharedRow.getByText("private")).toBeVisible();
});
