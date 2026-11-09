import { expect, test, type FrameLocator, type Page } from "@playwright/test";

// The host-free plugin VIEW harness (#4809, ADR 0034/0039) — the acceptance shape from the
// issue: cadview's freeze was "two mounts + a background subscribe", debuggable only against
// the live hub with hand-copied WKWebView localStorage. Here the SAME shape runs against the
// e2e mock backend with the fixture plugin's real view, under WebKit (the desktop engine) AND
// chromium. See apps/web/e2e/fixtures/preview-fixture and src/graph/plugins/preview.py.
//
// `x-e2e-preview` names the fixture → the mock splices its views into /api/runtime/status and
// serves its real page off disk. `x-e2e-bus-tap` taps this context's SSE stream (so a test can
// publish on demand) AND is the key the mock records page→bus publishes under (#4283 pattern).

const PID = "preview-fixture";
const MAIN = `plugin:${PID}:main`;
const SIDE = `plugin:${PID}:side`;
const TAP = `preview-${Math.random().toString(36).slice(2)}`;

test.use({ extraHTTPHeaders: { "x-e2e-preview": PID, "x-e2e-bus-tap": TAP } });

// The layout seed `plugin preview` writes into protoagent.ui (uiStore.ts persist key): mount
// `main` as the stage surface AND `side` in the right panel at once. `version: 14` matches the
// store (src/graph/plugins/preview.py `_UI_STORE_VERSION`), so the persist layer takes the blob
// verbatim instead of running it back through the migration chain.
const LAYOUT = {
  state: {
    surface: MAIN,
    rightPanel: SIDE,
    railOrder: { left: ["chat", MAIN], right: ["work", SIDE], bottom: [], hidden: [] },
  },
  version: 14,
};

async function bootPreview(page: Page): Promise<{ main: FrameLocator; side: FrameLocator }> {
  await page.addInitScript(
    (seed) => {
      try {
        localStorage.setItem("protoagent.ui", seed);
      } catch {
        /* storage blocked — the test will fail visibly on the missing mounts */
      }
    },
    JSON.stringify(LAYOUT),
  );
  await page.goto("/app/", { waitUntil: "load" });

  // Both views mount: one on the stage (left dock), one in the right panel — two real iframes.
  const frames = page.locator(".plugin-view-frame");
  await expect(frames).toHaveCount(2);
  const main = frames.nth(0).contentFrame();
  const side = frames.nth(1).contentFrame();
  // Each page ran its init handshake and its background subscribe.
  for (const f of [main, side]) {
    await expect(f.locator("body")).toHaveAttribute("data-bridge", /authed|anon/);
    await expect(f.locator("body")).toHaveAttribute("data-ready", "1");
  }
  return { main, side };
}

test("a tapped publish reaches BOTH background-subscribed mounts, and the page stays responsive", async ({
  page,
}) => {
  const { main, side } = await bootPreview(page);

  // The console's tapped SSE stream must be open before we publish (an untapped publish reaches
  // nobody). An empty-frames publish is a clean no-op that still reports the live stream count.
  await expect
    .poll(async () => {
      const res = await page.request.post("/api/__test__/bus", { data: { tap: TAP, frames: [] } });
      return (await res.json()).streams as number;
    })
    .toBeGreaterThan(0);

  // Publish one frame through the tap — it must land on BOTH mounts (the freeze shape: a double
  // mount with a background subscribe would reload mid-init and never record it).
  const res = await page.request.post("/api/__test__/bus", {
    data: { tap: TAP, frames: [{ topic: `${PID}.ping`, data: { id: "e1" } }] },
  });
  expect((await res.json()).streams).toBeGreaterThan(0);
  await expect(main.locator("body")).toHaveAttribute("data-frames", /\be1\b/);
  await expect(side.locator("body")).toHaveAttribute("data-frames", /\be1\b/);

  // Still responsive: a fresh UI click lands (a frozen / reload-looping page can't), and neither
  // mount churned (still exactly the two iframes — no remount).
  await main.locator("#tick").click();
  await expect(main.locator("body")).toHaveAttribute("data-tick", "ok");
  await expect(page.locator(".plugin-view-frame")).toHaveCount(2);
});

test("a page publish reaches the tap", async ({ page }) => {
  const { main } = await bootPreview(page);

  // The page posts `protoagent:publish`; the host namespaces it and POSTs /api/events/publish
  // with this context's tap header, which the mock records under the tap.
  await main.locator("#pub").click();
  await expect
    .poll(async () => {
      const res = await page.request.get(`/api/__test__/bus?tap=${TAP}`);
      const { received } = await res.json();
      return (received as Array<{ topic: string }>).map((r) => r.topic);
    })
    .toContain(`${PID}.ping`);
});
