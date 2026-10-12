import { expect, test, type Page } from "@playwright/test";

// Frame-rendered plugin component (ADR 0118 D5 console, S12b): a plugin contributes a
// component-v1 kind the console has NO built-in renderer for. The console resolves it through the
// live catalog (GET /api/components) to the plugin's FRAME page and hosts that page inline in the
// transcript (FrameComponentHost) — so the component renders WITH NO CONSOLE REBUILD. This drives
// the real stack end to end: a component-v1 part in the turn ("FRAMEKIND", e2e/fixtures.mjs) →
// ChatComponent's resolution order → FrameComponentHost's sandboxed iframe → the plugin-served
// frame page. Both the catalog and the frame page are route-mocked, so the console binary is
// unchanged — which is exactly the property under test.

// The catalog the console resolves component kinds through. `pl-demo-widget` is a frame kind (it
// carries a `frame_url`); the frame-less/core rows prove the console picks by name, not position.
const CATALOG = [
  { name: "pl-demo-widget", plugin: "demo", frame_url: "/plugins/demo/widget" },
  { name: "table", plugin: null, frame_url: null },
];

// The plugin-served page the resolved component renders in. A visible marker proves the frame's
// own markup is live; the height post is what the real plugin-kit sends so the host sizes to it.
const FRAME_HTML =
  "<!doctype html><html><body style='margin:0'>" +
  "<div id='frame-marker'>FRAME COMPONENT OK</div>" +
  "<script>parent.postMessage({ type: 'protoComponent:height', height: 120 }, '*');</script>" +
  "</body></html>";

async function withFrameComponent(page: Page) {
  await page.route("**/api/components", (route) => route.fulfill({ json: CATALOG }));
  await page.route("**/plugins/demo/widget*", (route) =>
    route.fulfill({ contentType: "text/html", body: FRAME_HTML }),
  );
}

test.beforeEach(async ({ page }) => {
  await withFrameComponent(page);
});

test("a plugin's frame component renders inline with no console rebuild", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("FRAMEKIND demo");
  await composer.press("Enter");

  // The turn settles; the component-v1 part resolves to a frame host in the transcript.
  await expect(page.locator(".pl-message--assistant").last()).toContainText("Here's the demo widget.");
  const host = page.getByTestId("frame-component-host");
  await expect(host).toBeVisible();
  // Bring the host into view so its near-viewport lazy mount fires (then the iframe exists).
  await host.scrollIntoViewIfNeeded();

  // It hosts the plugin's frame page (the catalog's frame_url) in a bearer-free, scripts-only
  // sandbox — deliberately NO allow-same-origin.
  const frame = host.locator("iframe");
  await expect(frame).toHaveAttribute("src", /\/plugins\/demo\/widget/);
  await expect(frame).toHaveAttribute("sandbox", "allow-scripts");

  // The plugin's OWN markup is live inside the frame — rendered without rebuilding the console.
  await expect(
    page.frameLocator('[data-testid="frame-component-host"] iframe').locator("#frame-marker"),
  ).toContainText("FRAME COMPONENT OK");
});
