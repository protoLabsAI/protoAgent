import { expect, test } from "@playwright/test";

// Real bearer gate, real auth flow (#2886). auth-gate.spec.ts simulates a gated
// deployment via Playwright route interception — which can inject Authorization
// on EVERY request, including <script src> module loads, something no real
// browser can do. That shape can't catch the #2884 class of bug: a view
// subresource that isn't exempted and isn't covered by the auth handshake 401s
// in production while the interception spec stays green. Here the MOCK SERVER
// itself is gated (`?gated=1` arms a per-context cookie; mock-server.mjs mirrors
// a2a_impl/auth.py's default-deny + public allowlist), the page injects NO
// extraHTTPHeaders, and the console must get through entirely on its own:
// anonymous SPA chrome → auth dialog → bearer on every fetch → the sse-token
// handshake for the EventSource.

const TOKEN = "e2e-operator-token";

test.describe("console vs a genuinely bearer-gated backend", () => {
  // Every 401 the browser receives (by pathname), and how many /app chrome
  // responses were served anonymously. Reset per test; tests within a worker run
  // sequentially, so module-level state is safe under fullyParallel.
  let unauthorized: string[] = [];
  let anonymousChrome = 0;

  test.beforeEach(async ({ page }) => {
    unauthorized = [];
    anonymousChrome = 0;
    page.on("response", (response) => {
      const { pathname } = new URL(response.url());
      if (response.status() === 401) unauthorized.push(pathname);
      if (pathname.startsWith("/app") && response.status() === 200) anonymousChrome += 1;
    });
    // Boot gated: the ?gated=1 document response sets the gate cookie, so every
    // subsequent request this context makes — module loads, fetches, EventSource
    // — hits the bearer gate for real.
    await page.goto("/app/?gated=1", { waitUntil: "load" });
  });

  test("boots anonymously to the auth dialog; the real token flow reaches chat with zero post-auth 401s", async ({ page }) => {
    // The SPA chrome (HTML + JS modules + CSS) was served WITHOUT auth — enough
    // of the app booted through the gate to render the dialog at all.
    const dialog = page.getByRole("dialog", { name: "Authentication required" });
    await expect(dialog).toBeVisible({ timeout: 10_000 });
    expect(anonymousChrome).toBeGreaterThan(0);

    // The gate is real: the boot probe 401'd at the SERVER (not an intercept).
    expect(unauthorized.some((p) => p.startsWith("/api/"))).toBe(true);

    // The real console auth flow — paste the token into the dialog.
    await dialog.getByLabel("Operator token").fill(TOKEN);
    await dialog.getByRole("button", { name: "Connect", exact: true }).click();
    await expect(dialog).not.toBeVisible();

    // Recovery in place: every query refetches with the bearer and chat renders.
    const composer = page.getByPlaceholder(/Message protoAgent/i);
    await expect(composer).toBeVisible({ timeout: 15_000 });

    // The SSE handshake completed through the gate: /api/sse-token (bearer) →
    // /api/events?token=. The goal.achieved toast only arrives on a CONNECTED
    // stream, so a tokenless-EventSource regression fails here, not silently.
    await expect(page.locator(".pl-toast", { hasText: "Goal achieved" })).toBeVisible({ timeout: 20_000 });

    // From here on EVERY request must ride the handshake. Snapshot the denials
    // seen so far (pre-auth 401s are expected — they tripped the dialog), then
    // drive a full authed chat turn (/a2a stream + turn-end reconcile) and
    // require zero NEW 401s: any API call or subresource outside the auth
    // handshake — the #2884 class — fails this assertion.
    const preAuthDenials = unauthorized.length;
    await composer.fill("calc 19 * 23");
    await composer.press("Enter");
    await expect(page.locator(".pl-message--user")).toHaveText(/calc 19 \* 23/);
    await expect(page.locator(".pl-message--assistant").last()).toContainText("19 × 23 = 437", { timeout: 15_000 });
    expect(unauthorized.slice(preAuthDenials)).toEqual([]);

    // And no pre-auth denial was ever for page chrome: the gate must only have
    // rejected API/consumer routes, never the public SPA assets.
    for (const p of unauthorized) {
      expect(p.startsWith("/api/") || p === "/a2a").toBe(true);
    }
  });

  test("plugin view page chrome is public_paths-exempt; the view gets the bearer via the handshake", async ({ page }) => {
    // Direct probes through the context's cookie jar (gated) with NO credential:
    // the plugin view PAGE is anonymous chrome (manifest public_paths →
    // set_public_prefixes), while a sibling API route stays 401 — the exact
    // split a2a_impl/auth.py enforces for view subresources.
    expect((await page.request.get("/plugins/boardy/board")).status()).toBe(200);
    expect((await page.request.get("/api/runtime/status")).status()).toBe(401);
    expect((await page.request.get("/app/")).status()).toBe(200);

    // Authenticate through the real dialog.
    const dialog = page.getByRole("dialog", { name: "Authentication required" });
    await expect(dialog).toBeVisible({ timeout: 10_000 });
    await dialog.getByLabel("Operator token").fill(TOKEN);
    await dialog.getByRole("button", { name: "Connect", exact: true }).click();
    await expect(dialog).not.toBeVisible();

    // Open the plugin view: the iframe's page loads (public chrome — a plain
    // navigation that cannot carry a bearer) and the console hands it the
    // operator token via the protoagent:init postMessage handshake — the only
    // way a view's data calls can authenticate against the gated backend.
    await page.locator(".pl-rail").getByRole("button", { name: "Board", exact: true }).click();
    const body = page.frameLocator(".plugin-view-frame").locator("body");
    await expect(body).toHaveAttribute("data-bridge", "authed", { timeout: 15_000 });
  });
});
