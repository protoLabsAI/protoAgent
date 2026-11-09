import { expect, test, type Page } from "@playwright/test";

import { routeSnapshot } from "./routeSnapshot";

// The runtime banner dispatches an operator click and clears after the status refresh.
const PYTHON_GAP = {
  plugin: "boardy",
  label: "Project Board",
  key: "runtime-missing",
  message:
    "needs Python 3.12. Install its runtime to continue.",
  actions: [
    { kind: "install_python", target: "boardy", label: "Install Python runtime" },
    { kind: "global_settings", target: "plugins", label: "Open Plugins" },
  ],
};

/** Serve runtime status with the runtime gap while `gapOpen()` is true. */
async function routeGap(page: Page, gapOpen: () => boolean) {
  await routeSnapshot(page, "/api/runtime/status", (base: Record<string, unknown>) => {
    const gaps = gapOpen() ? [PYTHON_GAP] : [];
    return { ...base, setup_gaps: gaps, warnings: gaps.map((g) => `${g.label}: ${g.message}`) };
  });
}

test("the runtime banner provisions Python in place and clears on success", async ({ page }) => {
  let open = true;
  const posted: unknown[] = [];
  await routeGap(page, () => open);
  await page.route("**/api/plugins/install-python", async (route) => {
    posted.push(route.request().postDataJSON());
    await new Promise((r) => setTimeout(r, 300)); // long enough to see the in-flight state
    open = false; // the server recomputed the gap: runtime provisioned
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toContainText("needs Python 3.12");
  const install = banner.getByTestId("setup-gap-install-python");
  await expect(install).toHaveText("Install Python runtime");
  await expect(banner.getByRole("button", { name: "Open Plugins" })).toBeVisible();

  await install.click();
  await expect(install).toContainText("Installing…");
  await expect(page.locator(".pl-toast", { hasText: "Python runtime installed" })).toBeVisible();
  expect(posted).toEqual([{ id: "boardy" }]); // THIS gap's plugin, via the existing route
  await expect(page.locator(".setup-gap-banner")).toHaveCount(0);
});

test("a failed banner install shows uv's error and keeps the banner + its action", async ({ page }) => {
  await routeGap(page, () => true);
  await page.route("**/api/plugins/install-python", (route) =>
    route.fulfill({
      status: 400,
      json: { detail: "uv venv failed: Python download failed" },
    }),
  );
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await banner.getByTestId("setup-gap-install-python").click();
  const toast = page.locator(".pl-toast", { hasText: "Python runtime didn't install" });
  await expect(toast).toBeVisible();
  await expect(toast).toContainText("Python download failed");
  await expect(banner.getByTestId("setup-gap-install-python")).toHaveText("Install Python runtime");
});

