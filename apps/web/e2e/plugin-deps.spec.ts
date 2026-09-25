import { expect, test, type Page } from "@playwright/test";

import { routeSnapshot } from "./routeSnapshot";

// A plugin's missing Python packages, installed from where the operator already is:
//  • the loader's deps-gap banner carries an "Install dependencies" button (setup-gap action
//    `install_deps`) that POSTs the SAME /api/plugins/install-deps as the Plugins row;
//  • an install whose response reports `deps_needed` asks once ("install them now?"), lists
//    the exact specs + the plugin's source, and installs on confirm through that same route.
// The gap record is the shape graph/plugins/loader.py reports (pinned in tests/test_plugins.py).

const DEPS_GAP = {
  plugin: "boardy",
  label: "Project Board",
  key: "deps-missing",
  message:
    "can't run until its Python packages are installed: leftpad. Install them from Settings ▸ Plugins or with `protoagent plugin install-deps boardy`.",
  actions: [
    { kind: "install_deps", target: "boardy", label: "Install dependencies" },
    { kind: "global_settings", target: "plugins", label: "Open Plugins" },
  ],
};

/** Serve runtime status with the deps gap while `gapOpen()` is true. */
async function routeGap(page: Page, gapOpen: () => boolean) {
  await routeSnapshot(page, "/api/runtime/status", (base: Record<string, unknown>) => {
    const gaps = gapOpen() ? [DEPS_GAP] : [];
    return { ...base, setup_gaps: gaps, warnings: gaps.map((g) => `${g.label}: ${g.message}`) };
  });
}

test("the deps banner installs the packages in place and clears on success", async ({ page }) => {
  let open = true;
  const posted: unknown[] = [];
  await routeGap(page, () => open);
  await page.route("**/api/plugins/install-deps", async (route) => {
    posted.push(route.request().postDataJSON());
    await new Promise((r) => setTimeout(r, 300)); // long enough to see the in-flight state
    open = false; // the server recomputed the gap: packages landed
    await route.fulfill({ json: { ok: true, installed: ["leftpad>=1"], refresh: "plugin" } });
  });
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toContainText("can't run until its Python packages are installed: leftpad");
  const install = banner.getByTestId("setup-gap-install-deps");
  await expect(install).toHaveText("Install dependencies");
  await expect(banner.getByRole("button", { name: "Open Plugins" })).toBeVisible();

  await install.click();
  await expect(install).toContainText("Installing…");
  await expect(page.locator(".pl-toast", { hasText: "Dependencies installed" })).toBeVisible();
  expect(posted).toEqual([{ id: "boardy" }]); // THIS gap's plugin, via the existing route
  await expect(page.locator(".setup-gap-banner")).toHaveCount(0);
});

test("a failed banner install shows pip's error and keeps the banner + its action", async ({ page }) => {
  await routeGap(page, () => true);
  await page.route("**/api/plugins/install-deps", (route) =>
    route.fulfill({
      status: 400,
      json: { detail: "pip install failed: ERROR: No matching distribution found for leftpad>=1" },
    }),
  );
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await banner.getByTestId("setup-gap-install-deps").click();
  const toast = page.locator(".pl-toast", { hasText: "Dependencies didn't install" });
  await expect(toast).toBeVisible();
  await expect(toast).toContainText("No matching distribution found for leftpad>=1");
  await expect(banner.getByTestId("setup-gap-install-deps")).toHaveText("Install dependencies");
});

const DEPS_NEEDED = [
  {
    id: "artifact-plugin",
    name: "Artifact",
    source: "https://github.com/protoLabsAI/artifact-plugin",
    target: "this server's Python environment",
    deps: [
      { name: "leftpad", spec: "leftpad>=1; sys_platform == 'darwin'", optional: false },
      { name: "fancy", spec: "fancy>=2", optional: true },
    ],
  },
];

/** Answer POST /api/plugins/install as a clean install that still needs packages here. */
async function routeInstallNeedingDeps(page: Page) {
  await page.route("**/api/plugins/install", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await route.fulfill({
      json: {
        installed: {
          id: "artifact-plugin", name: "Artifact", version: "0.1.0", description: "", resolved_sha: "a".repeat(40),
          source_url: "https://github.com/protoLabsAI/artifact-plugin", requires_pip: [], capabilities: {},
          contributes: { views: [], secrets: [] },
        },
        enabled: ["artifact-plugin"],
        reloaded: true,
        restart_recommended: false,
        enable_error: null,
        load_errors: {},
        deps_needed: DEPS_NEEDED,
      },
    });
  });
}

async function openDiscover(page: Page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Plugins", exact: true }).click();
  await page.locator(".pl-tabs").getByRole("tab", { name: "Discover", exact: true }).click();
}

test("Discover install asks once for the missing packages and installs them on confirm", async ({ page }) => {
  await routeInstallNeedingDeps(page);
  const posted: unknown[] = [];
  await page.route("**/api/plugins/install-deps", async (route) => {
    posted.push(route.request().postDataJSON());
    await route.fulfill({ json: { ok: true, installed: ["leftpad>=1; sys_platform == 'darwin'", "fancy>=2"], refresh: "plugin" } });
  });
  await openDiscover(page);
  await page.locator(".plugin-card", { hasText: "Artifact" }).getByRole("button", { name: "Install", exact: true }).click();

  const dialog = page.getByRole("dialog", { name: "Install Python packages for Artifact?" });
  await expect(dialog).toBeVisible();
  // The exact specs pip will run, the optional tier, and the source + target being consented to.
  await expect(dialog.getByRole("list", { name: "required packages" })).toHaveText("leftpad>=1; sys_platform == 'darwin'");
  await expect(dialog.getByRole("list", { name: "optional packages" })).toHaveText("fancy>=2");
  await expect(dialog).toContainText("https://github.com/protoLabsAI/artifact-plugin");
  await expect(dialog).toContainText("this server's Python environment");
  expect(posted).toEqual([]); // nothing installs before the click

  await dialog.getByRole("button", { name: "Install packages" }).click();
  await expect(dialog).toContainText("Dependencies installed");
  expect(posted).toEqual([{ id: "artifact-plugin" }]);
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(dialog).toHaveCount(0);
});

test("Discover install: a failed package install shows pip's error with Retry", async ({ page }) => {
  await routeInstallNeedingDeps(page);
  await page.route("**/api/plugins/install-deps", (route) =>
    route.fulfill({ status: 400, json: { detail: "pip install failed: ERROR: No matching distribution found for leftpad>=1" } }),
  );
  await openDiscover(page);
  await page.locator(".plugin-card", { hasText: "Artifact" }).getByRole("button", { name: "Install", exact: true }).click();

  const dialog = page.getByRole("dialog", { name: "Install Python packages for Artifact?" });
  await dialog.getByRole("button", { name: "Install packages" }).click();
  await expect(dialog).toContainText("Dependencies didn't install");
  await expect(dialog).toContainText("No matching distribution found for leftpad>=1");
  await expect(dialog.getByRole("button", { name: "Retry" })).toBeVisible();
});

test("git-URL install hands over to the packages dialog; Not now installs nothing", async ({ page }) => {
  await routeInstallNeedingDeps(page);
  let called = false;
  await page.route("**/api/plugins/install-deps", (route) => {
    called = true;
    return route.fulfill({ json: { ok: true, installed: [] } });
  });
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Plugins", exact: true }).click();
  await page.getByRole("button", { name: "Install from URL" }).click();
  await page.getByLabel("plugin git URL").fill("https://github.com/protoLabsAI/artifact-plugin");
  await page.getByRole("button", { name: "Install", exact: true }).click();

  const dialog = page.getByRole("dialog", { name: "Install Python packages for Artifact?" });
  await expect(dialog).toBeVisible();
  await expect(page.getByLabel("plugin git URL")).toHaveCount(0); // one dialog at a time
  await dialog.getByRole("button", { name: "Not now" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByLabel("plugin git URL")).toHaveCount(0); // the whole flow closed
  expect(called).toBe(false);
});

test("an older backend without deps_needed still gets the declared-packages note (clean names)", async ({ page }) => {
  await page.route("**/api/plugins/install", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await route.fulfill({
      json: {
        installed: {
          id: "legacy", name: "Legacy", version: "0.1.0", description: "", resolved_sha: "b".repeat(40),
          source_url: "https://github.com/acme/legacy", requires_pip: ["leftpad>=1; sys_platform == 'win32'"],
          capabilities: {}, contributes: { views: [], secrets: [] },
        },
        enabled: ["legacy"], reloaded: true, restart_recommended: false, enable_error: null, load_errors: {},
      },
    });
  });
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Plugins", exact: true }).click();
  await page.getByRole("button", { name: "Install from URL" }).click();
  await page.getByLabel("plugin git URL").fill("https://github.com/acme/legacy");
  await page.getByRole("button", { name: "Install", exact: true }).click();
  await expect(page.locator(".plugin-install-status")).toHaveText(
    "Installed Legacy — declares Python packages: leftpad (Install deps on its row).",
  );
});
