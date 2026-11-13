import { expect, test } from "@playwright/test";

// Boot splash (bd-aawcj): switching agents is a full page load, so until the 2 MB bundle runs,
// #root would be an empty div on a dark background. index.html now paints a static splash on the
// first frame — the same layout/colours/spinner as App's BootGate — and a tiny inline script names
// the agent from the URL slug + a name the console cached in localStorage.
//
// These specs HOLD the app's JS bundle (the /app/assets/*.js chunks) behind a promise, prove the
// splash text is already on screen while nothing has executed, then release and confirm React's
// createRoot replaces the static splash. The hold is a gate, not a fixed wait — no waitForTimeout.

/** Route every app JS chunk through `gate`, so the module entry (and thus main.tsx) cannot run
 *  until the test resolves it. Returns the release fn. */
async function holdBundle(page: import("@playwright/test").Page): Promise<() => void> {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  await page.route(/\/assets\/[^?]*\.js/, async (route) => {
    await gate;
    await route.continue();
  });
  return release;
}

test("an agent path with a cached name shows 'Opening <name>…' before any script runs", async ({ page }) => {
  // The fleet switcher / boot gate cache the display name per slug (lib/agentNameCache.ts); the
  // inline splash script reads the same key raw. Seed it before the page's own scripts run.
  await page.addInitScript(() => {
    try {
      window.localStorage.setItem("protoagent.agentName:gizmo", "Gizmo Labs");
    } catch {
      /* storage blocked */
    }
  });

  const release = await holdBundle(page);
  await page.goto("/app/agent/gizmo/", { waitUntil: "commit" });

  // Visible while the bundle is still held — so no React has mounted yet.
  await expect(page.getByText("Opening Gizmo Labs…")).toBeVisible();

  // Let the bundle run: createRoot replaces the static splash with the live tree.
  release();
  await expect(page.locator(".pa-boot-splash")).toHaveCount(0);
});

test("an agent path with nothing cached shows 'Opening agent…' before any script runs", async ({ page }) => {
  const release = await holdBundle(page);
  await page.goto("/app/agent/nameless/", { waitUntil: "commit" });

  await expect(page.getByText("Opening agent…")).toBeVisible();

  release();
});
