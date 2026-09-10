import { expect, test } from "@playwright/test";

// Runtime-status `warnings` (#706 co-located instances etc.) render as a slim
// alert strip under the topbar; server-driven, so no warnings → no strip.

test("runtime warnings render as the shell alert strip", async ({ page }) => {
  await page.route("**/api/runtime/status", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    json.warnings = ["Another running instance shares this agent's data (~/.protoagent): roxy (pid 12345, port 7871)."];
    await route.fulfill({ json });
  });
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".shell-warning-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("Another running instance");
  await expect(banner).toHaveAttribute("role", "alert");
});

test("no warnings → no alert strip (the default fixture)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible(); // app booted
  await expect(page.locator(".shell-warning-banner")).toHaveCount(0);
});

// Structured setup gaps (graph/plugins/setup_gaps.py) ride the SAME `warnings[]` array
// as legacy strings, but render as actionable, dismissible banners. `boardy` is the
// enabled+loaded plugin in the e2e fixture, so its Configure dialog resolves cleanly.
const CODER_GAP = {
  plugin: "boardy",
  label: "Project Board",
  key: "coder",
  message: "No coder delegate is configured, so the board can't run features.",
  actions: [{ kind: "plugin_config", target: "boardy", label: "Configure Project Board" }],
};

async function routeWarnings(page: import("@playwright/test").Page, warnings: unknown[]) {
  await page.route("**/api/runtime/status", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    json.warnings = warnings;
    await route.fulfill({ json });
  });
}

test("a structured setup gap renders its Configure action and opens the plugin-config dialog", async ({ page }) => {
  await routeWarnings(page, [CODER_GAP]);
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toHaveAttribute("role", "alert");
  await expect(banner).toContainText("No coder delegate is configured");

  await banner.getByRole("button", { name: "Configure Project Board" }).click();
  // Opens the reporting plugin's existing Configure dialog (titled by the gap label).
  await expect(page.getByRole("dialog", { name: "Project Board" })).toBeVisible();
});

test("an unknown/malformed action renders no interactive control and doesn't break the strip", async ({ page }) => {
  await routeWarnings(page, [
    {
      plugin: "boardy",
      label: "Project Board",
      key: "repo",
      message: "The board can't reach its repository.",
      actions: [{ kind: "open_url", target: "https://evil.example/pwn" }],
    },
  ]);
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("can't reach its repository");
  // No CTA, and the plugin string never became a link.
  await expect(banner.getByRole("button", { name: /Configure|Open settings/ })).toHaveCount(0);
  await expect(banner.locator("a")).toHaveCount(0);
  await expect(page.locator(".pl-rail").first()).toBeVisible(); // strip + app still healthy
});

test("legacy string warnings and a structured gap coexist in the strip", async ({ page }) => {
  await routeWarnings(page, ["Another running instance shares this agent's data.", CODER_GAP]);
  await page.goto("/app/", { waitUntil: "load" });

  // The plain string still renders as a warning alert; the gap renders as its own banner.
  await expect(page.locator(".shell-warning-banner")).toHaveCount(2);
  await expect(page.locator(".setup-gap-banner")).toHaveCount(1);
  await expect(page.getByText("Another running instance shares this agent's data.")).toBeVisible();
});

test("a setup gap dismisses for the session only, and returns on a new session", async ({ page }) => {
  await routeWarnings(page, [CODER_GAP]);
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: /Dismiss/ }).click();
  await expect(banner).toHaveCount(0);

  // Same session (reload keeps sessionStorage) → the unchanged gap stays hidden even though
  // the server still reports it every poll.
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible();
  await expect(page.locator(".setup-gap-banner")).toHaveCount(0);

  // New session (sessionStorage cleared) → it returns; dismissal never touched the server.
  await page.evaluate(() => window.sessionStorage.clear());
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".setup-gap-banner")).toBeVisible();
});

test("a dismissed gap stays hidden across a transient empty runtime status in the same session", async ({ page }) => {
  // The status endpoint's `warnings` payload is mutable across reloads, so we can simulate a
  // transient/null runtime status (reload catching an unresolved poll) between two live polls.
  let currentWarnings: unknown[] = [CODER_GAP];
  await page.route("**/api/runtime/status", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    json.warnings = currentWarnings;
    await route.fulfill({ json });
  });
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: /Dismiss/ }).click();
  await expect(banner).toHaveCount(0);

  // Status blips to empty (a reload catching an unresolved status) — the strip clears, but the
  // session dismissal must NOT be pruned just because the live gap set is momentarily empty.
  currentWarnings = [];
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible();
  await expect(page.locator(".setup-gap-banner")).toHaveCount(0);

  // The unchanged gap returns on the next poll/reload → it stays hidden for the rest of the
  // session (the regression the review caught: it must NOT reappear).
  currentWarnings = [CODER_GAP];
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible();
  await expect(page.locator(".setup-gap-banner")).toHaveCount(0);
});
