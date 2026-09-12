import type { Page, Request } from "@playwright/test";

/**
 * Serve a patched copy of a mock endpoint's REAL body — snapshotted ONCE, up front.
 *
 * The obvious way to patch a live response, proxying inside the handler, is flaky:
 *
 *     await page.route("**\/api/runtime/status", async (route) => {
 *       const json = await (await route.fetch()).json(); // ← "Response has been disposed"
 *       ...
 *
 * `route.fetch()` stores the body in the BrowserContext's request context, and test teardown
 * disposes it — `BrowserContext.close()` calls `request.dispose()` first, which drops every stored
 * fetch body. The app fires requests right up to the end of a test (a poll, the blanket refetch
 * after the setup wizard's Finish), so one handler can have its `route.fetch()` resolve just
 * before teardown and its body read land just after: `apiResponse.json: Response has been
 * disposed`, failing a test whose assertions all passed. The gap is milliseconds, so it only opens
 * under load (setup-wizard.spec, 1 in 160 runs with several suites running at once).
 *
 * Snapshotting in the test body — awaited, long before teardown — leaves the handler holding
 * nothing teardown can dispose; `route.fulfill` itself is safe against a closing page. It is a
 * snapshot, so use this only where the endpoint's real body doesn't change during the test.
 *
 * `patch` gets a fresh deep copy per request: mutate it in place, or return a replacement.
 * `glob` defaults to `**<path>` (which also matches the slug-routed `/agents/<slug><path>`);
 * `headers` scope the snapshot — `page.request` does NOT inherit `page.setExtraHTTPHeaders`.
 * Only GETs are answered from the snapshot; any other method falls through to the next
 * handler (or the mock), exactly as if this route weren't there.
 */
export async function routeSnapshot<T = any>(
  page: Page,
  path: string,
  patch: (json: T, request: Request) => T | void,
  { glob = `**${path}`, headers }: { glob?: string; headers?: Record<string, string> } = {},
): Promise<void> {
  const snapshot = await page.request.get(path, { headers });
  const status = snapshot.status();
  const base = (await snapshot.json()) as T;
  await page.route(glob, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const json = structuredClone(base);
    const patched = patch(json, route.request());
    await route.fulfill({ status, json: patched === undefined ? json : patched });
  });
}
