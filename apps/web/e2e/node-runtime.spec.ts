import { expect, test } from "@playwright/test";

// Settings ▸ Workspace ▸ MCP: <NodeRuntimeCard> (ADR 0085). When no Node is on PATH the
// npx-based servers/coding agents can't launch, so the panel offers one-click
// provisioning. This drives the real component + react-query polling against a mocked
// backend: missing → click Install → progress → done (toast), the same states a real
// server walks through. Also proves the card stays HIDDEN when a Node already works.

const SHOTS = process.env.NODE_RUNTIME_SHOTS || "";

const missingNode = {
  source: null,
  version: null,
  bin_dir: null,
  managed: false,
  managed_version: null,
  system: false,
  supported: true,
  target_version: "v24.18.0",
};
const managedNode = {
  ...missingNode,
  source: "managed",
  version: "v24.18.0",
  managed: true,
  managed_version: "v24.18.0",
  bin_dir: "/home/u/.protoagent/runtime/node/current/bin",
};

async function openMcp(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "MCP", exact: true }).click();
  await expect(page.getByRole("heading", { name: "MCP servers" })).toBeVisible();
}

test("Node runtime: prompts to install, shows progress, then confirms", async ({ page }) => {
  let phase = "missing"; // missing → running → done

  await page.route("**/api/runtime/node", (route) => {
    const install =
      phase === "running"
        ? { state: "running", pct: 60, message: "downloading… 60%", error: null }
        : phase === "done"
          ? { state: "done", pct: 100, message: "installed", error: null }
          : { state: "idle", pct: 0, message: "", error: null };
    const node = phase === "done" ? managedNode : missingNode;
    return route.fulfill({ json: { node, install } });
  });
  await page.route("**/api/runtime/node/install**", (route) => {
    phase = "running";
    setTimeout(() => {
      phase = "done";
    }, 600);
    return route.fulfill({
      status: 202,
      json: { ok: true, node: missingNode, install: { state: "running", pct: 0, message: "starting…", error: null } },
    });
  });

  await openMcp(page);

  // 1. The call to action.
  const banner = page.locator(".shell-warning-banner", { hasText: "No Node runtime detected" });
  await expect(banner).toBeVisible();
  await expect(banner.getByText(/npx.*can't launch/)).toBeVisible();
  if (SHOTS) await banner.screenshot({ path: `${SHOTS}/node-runtime-prompt.png` });

  // 2. Install → progress.
  await page.getByRole("button", { name: /Install runtime/ }).click();
  await expect(page.locator(".shell-warning-banner", { hasText: "Installing Node runtime" })).toBeVisible();
  if (SHOTS) await page.locator(".shell-warning-banner").first().screenshot({ path: `${SHOTS}/node-runtime-installing.png` });

  // 3. Done → success toast + the banner self-hides (a usable Node now exists).
  await expect(page.locator(".pl-toast", { hasText: "Node runtime installed" })).toBeVisible();
  await expect(page.locator(".shell-warning-banner", { hasText: "No Node runtime detected" })).toHaveCount(0);
});

test("Node runtime: no banner when a Node is already available", async ({ page }) => {
  await page.route("**/api/runtime/node", (route) => route.fulfill({ json: { node: managedNode, install: { state: "idle", pct: 0, message: "", error: null } } }));
  await openMcp(page);
  await expect(page.locator(".shell-warning-banner")).toHaveCount(0);
});
