import { expect, test } from "@playwright/test";

// Desktop self-auth (#2055, #5408) against the real bearer gate of the mock (`?gated=1`, see
// auth-gated-views.spec.ts). The stored token goes bad under a running console (the shell rotated
// its secret): the first 401 makes the console ask the desktop shell for its token, which it
// stores. Requests already in flight with the old token come back 401 AFTER that, and used to
// put the sign-in prompt back up over a console that was signed in again. The prompt must stay
// down and the panels must recover with the shell's token.

const TOKEN = "e2e-operator-token";

test("a desktop self-auth mid-session keeps the prompt down; stale 401s don't re-open it", async ({ page }) => {
  // A fake desktop shell: `auth_token` hands over the real token; every other command is a no-op.
  await page.addInitScript((token) => {
    (window as unknown as { __TAURI__: unknown }).__TAURI__ = {
      core: { invoke: async (cmd: string) => (cmd === "auth_token" ? token : null) },
      event: { listen: async () => () => {} },
    };
    if (!sessionStorage.getItem("e2e-booted")) {
      sessionStorage.setItem("e2e-booted", "1");
      localStorage.setItem("protoagent.authToken", token); // signed in from the start
    }
  }, TOKEN);
  await page.goto("/app/?gated=1", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible({ timeout: 15_000 });

  // Every request sent with the old token, until it has come back.
  const inFlightStale = new Set<object>();
  page.on("request", (q) => {
    if (q.headers()["authorization"] === "Bearer revoked") inFlightStale.add(q);
  });
  page.on("requestfinished", (q) => inFlightStale.delete(q));
  page.on("requestfailed", (q) => inFlightStale.delete(q));

  // The token goes bad. The next reads 401 with it; the first one asks the shell for its token.
  const stale = page.waitForResponse((r) => r.status() === 401 && r.request().headers()["authorization"] === "Bearer revoked");
  await page.evaluate(() => localStorage.setItem("protoagent.authToken", "revoked"));
  await stale;
  // The shell's token is stored and every query refetches with it: Decisions comes back good.
  await page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/decisions" && r.ok() && r.request().headers()["authorization"] === `Bearer ${TOKEN}`,
    { timeout: 20_000 },
  );
  expect(await page.evaluate(() => localStorage.getItem("protoagent.authToken"))).toBe(TOKEN);
  // Every request still in flight with the old token has come back (as a 401) by now…
  await expect.poll(() => inFlightStale.size, { timeout: 10_000 }).toBe(0);
  // …and none of those stale 401s put the prompt back up.
  await expect(page.getByRole("dialog", { name: "Authentication required" })).toBeHidden();
});
