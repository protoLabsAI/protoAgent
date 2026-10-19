import { expect, test } from "@playwright/test";

// Settings ▸ Devices ▸ Pair an agent (ADR 0113) — the REMOTE's half of agent pairing: mint a
// typeable code, show where this agent is reachable, cancel it scoped to its kind, offer the
// shared reachability step when loopback-bound, and badge paired hubs as "Agent".
//
// The section is behind `settings.devices` (ADR 0068, OFF by default until the ADR 0113 D8
// desktop test), so every test opts in with the `?flag:` query override — the same shareable
// override a developer would use.

test.describe.configure({ mode: "serial" });

const scope = (testInfo) => `devices-spec-${testInfo.parallelIndex}`;

test.beforeEach(async ({ page }, testInfo) => {
  await page.setExtraHTTPHeaders({ "x-e2e-fleet": scope(testInfo) }); // devices live on the fleet scope
  await page.request.post("/api/__test__/fleet/reset", { headers: { "x-e2e-fleet": scope(testInfo) } });
});

async function openDevices(page) {
  await page.goto("/app/?flag:settings.devices=on", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  await page.getByTestId("app-drawer").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Devices", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Devices" })).toBeVisible();
}

test("the device list badges other agents' hubs apart from phones, and revoke still works", async ({ page }) => {
  await openDevices(page);
  const phone = page.locator(".subagent-row", { hasText: "Josh's iPhone" });
  const hub = page.locator(".subagent-row", { hasText: "studio-hub" });
  await expect(phone.getByText("Device", { exact: true })).toBeVisible();
  await expect(hub.getByText("Agent", { exact: true })).toBeVisible();

  await hub.getByRole("button", { name: "Remove studio-hub" }).click();
  await expect(page.getByText("Removed studio-hub")).toBeVisible();
  await expect(page.locator(".subagent-row", { hasText: "studio-hub" })).toHaveCount(0);
  await expect(phone).toBeVisible();
});

test("Pair an agent: big code, m:ss countdown, tailnet-first URLs, the hub hint; Cancel is scoped to agent", async ({ page }) => {
  const starts = [];
  const cancels = [];
  page.on("request", (r) => {
    if (r.url().endsWith("/api/pairing/start")) starts.push(r.postDataJSON());
    if (r.url().endsWith("/api/pairing/cancel")) cancels.push(r.postDataJSON());
  });
  await openDevices(page);
  await page.getByRole("button", { name: "Pair an agent" }).click();

  const card = page.getByRole("region", { name: "Pair another agent" });
  await expect(card.getByTestId("agent-pair-code")).toHaveText("7KQ2M-X9D4P");
  await expect(card).toContainText(/Expires in [45]:\d\d/);
  expect(starts).toEqual([{ kind: "agent" }]);
  // The mock lists LAN first — the console must still lead with the tailnet address.
  const urls = card.locator(".devices-agent-hosts li");
  await expect(urls.first()).toContainText("Tailnet");
  await expect(urls.first()).toContainText("http://100.64.0.5:7871");
  await expect(urls.nth(1)).toContainText("LAN");
  await expect(card).toContainText("Settings ▸ Agents ▸ Pair…");
  await expect(card).toContainText("protoagent fleet pair <url> <code>");

  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(card).toHaveCount(0);
  await expect.poll(() => cancels).toEqual([{ kind: "agent" }]);
});

test("Add a device cancels with kind device (it can't drop a pending agent code)", async ({ page }) => {
  const cancels = [];
  page.on("request", (r) => {
    if (r.url().endsWith("/api/pairing/cancel")) cancels.push(r.postDataJSON());
  });
  await openDevices(page);
  await page.getByRole("button", { name: "Add a device" }).click();
  await expect(page.getByRole("region", { name: "Pair a new device" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect.poll(() => cancels).toEqual([{ kind: "device" }]);
});

test("Add a device shows the short code beside the QR, the URL to open, and the countdown", async ({ page }) => {
  await openDevices(page);
  await page.getByRole("button", { name: "Add a device" }).click();
  const card = page.getByRole("region", { name: "Pair a new device" });
  // ONE code (ADR 0087 D3 amendment): what the QR carries is also what a phone types.
  await expect(card.getByTestId("device-pair-code")).toHaveText("3HV8T-QW6RN");
  await expect(card.getByTestId("device-pair-url")).toHaveText("http://100.64.0.5:7871/app/");
  await expect(card).toContainText("Have a pairing code?");
  await expect(card).toContainText(/Expires in [45]:\d\d/);
});

test("an expired agent code offers New code", async ({ page }) => {
  // Pin the mock's clock 10 minutes in the past, so the code it mints is already expired
  // by this page's real clock — the countdown's expiry path, without waiting 5 minutes.
  await page.setExtraHTTPHeaders({
    "x-e2e-fleet": scope(test.info()),
    "x-e2e-now": String(Date.now() - 10 * 60 * 1000),
  });
  await openDevices(page);
  await page.getByRole("button", { name: "Pair an agent" }).click();
  const notice = page.getByRole("region", { name: "Pairing code expired" });
  await expect(notice).toContainText("agent code expired");
  await expect(notice.getByRole("button", { name: "New code" })).toBeVisible();
});

test("loopback-bound: Pair an agent shows the SAME reachability step as the phone flow", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-fleet": scope(test.info()), "x-e2e-pairing": "loopback" });
  await openDevices(page);
  await page.getByRole("button", { name: "Pair an agent" }).click();
  const step = page.getByRole("region", { name: "Make this agent reachable" });
  await expect(step).toContainText("another agent can't reach it");
  await expect(step.getByRole("button", { name: "Tailnet · 100.64.0.5" })).toBeVisible();
  await expect(step.getByRole("button", { name: "Wi-Fi · 192.168.5.20" })).toBeVisible();
});

// ── Device identity (#4336 S3) ────────────────────────────────────────────────────────────

test("rename a device inline; an empty name is refused in place", async ({ page }) => {
  await openDevices(page);
  await page.getByRole("button", { name: "Rename Josh's iPhone" }).click();
  const input = page.getByRole("textbox", { name: "New name for Josh's iPhone" });
  await expect(input).toHaveValue("Josh's iPhone");

  await input.fill("   ");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Give it a name.");

  const patches = [];
  page.on("request", (r) => {
    if (r.method() === "PATCH" && r.url().includes("/api/devices/")) patches.push([r.url(), r.postDataJSON()]);
  });
  await input.fill("Kitchen iPad");
  await input.press("Enter");
  await expect(page.locator(".subagent-row", { hasText: "Kitchen iPad" })).toBeVisible();
  await expect(page.locator(".subagent-row", { hasText: "Josh's iPhone" })).toHaveCount(0);
  expect(patches).toEqual([[expect.stringMatching(/\/api\/devices\/dev-phone1$/), { name: "Kitchen iPad" }]]);
});

test("Escape cancels a rename without closing Settings", async ({ page }) => {
  await openDevices(page);
  await page.getByRole("button", { name: "Rename studio-hub" }).click();
  const input = page.getByRole("textbox", { name: "New name for studio-hub" });
  await input.fill("something else");
  await input.press("Escape");
  await expect(input).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Devices" })).toBeVisible();
  await expect(page.locator(".subagent-row", { hasText: "studio-hub" })).toBeVisible();
});

test("this browser's own row is marked; removing it asks first, then signs you out", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("protoagent.deviceToken", "e2e-device-token");
    window.localStorage.setItem("protoagent.deviceId", "dev-phone1");
  });
  await openDevices(page);
  const mine = page.locator(".subagent-row", { hasText: "Josh's iPhone" });
  const hub = page.locator(".subagent-row", { hasText: "studio-hub" });
  await expect(mine.getByText("This device", { exact: true })).toBeVisible();
  await expect(hub.getByText("This device", { exact: true })).toHaveCount(0);

  const deletes = [];
  page.on("request", (r) => {
    if (r.method() === "DELETE") deletes.push(r.url());
  });
  await mine.getByRole("button", { name: "Remove Josh's iPhone" }).click();
  const confirm = mine.getByRole("group", { name: "Remove this device" });
  await expect(confirm).toContainText("signs you out here");
  // Asking is not doing: nothing was sent yet, and Keep backs out.
  expect(deletes).toEqual([]);
  await confirm.getByRole("button", { name: "Keep" }).click();
  await expect(confirm).toHaveCount(0);

  await mine.getByRole("button", { name: "Remove Josh's iPhone" }).click();
  await mine.getByRole("button", { name: "Remove and sign out" }).click();
  await expect(page.getByRole("dialog", { name: "Authentication required" })).toBeVisible();
  expect(deletes).toEqual([expect.stringMatching(/\/api\/devices\/dev-phone1$/)]);
  expect(await page.evaluate(() => window.localStorage.getItem("protoagent.deviceToken"))).toBeNull();
  expect(await page.evaluate(() => window.localStorage.getItem("protoagent.deviceId"))).toBeNull();
});

test("removing this device keeps you signed in when an operator token is also kept here", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("protoagent.authToken", "e2e-operator-token");
    window.localStorage.setItem("protoagent.deviceToken", "e2e-device-token");
    window.localStorage.setItem("protoagent.deviceId", "dev-phone1");
  });
  await openDevices(page);
  const mine = page.locator(".subagent-row", { hasText: "Josh's iPhone" });
  await mine.getByRole("button", { name: "Remove Josh's iPhone" }).click();
  await mine.getByRole("button", { name: "Remove and sign out" }).click();
  await expect(page.getByText("Still connected with this browser's operator token.")).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Authentication required" })).toHaveCount(0);
  await expect(page.locator(".subagent-row", { hasText: "Josh's iPhone" })).toHaveCount(0);
  expect(await page.evaluate(() => window.localStorage.getItem("protoagent.authToken"))).toBe("e2e-operator-token");
});

test("the active badge covers the 5-minute last-seen throttle", async ({ page }) => {
  const now = Math.floor(Date.now() / 1000);
  await page.route("**/api/devices", (route) =>
    route.fulfill({
      json: {
        devices: [
          // Stamped 4 minutes ago: the server won't re-stamp until 5 — this IS in use.
          { id: "a1", name: "Busy phone", kind: "device", created_at: now - 9000, last_seen_at: now - 240 },
          { id: "a2", name: "Old tablet", kind: "device", created_at: now - 9000, last_seen_at: now - 3 * 3600, active: false },
        ],
      },
    }),
  );
  await openDevices(page);
  const busy = page.locator(".subagent-row", { hasText: "Busy phone" });
  await expect(busy.getByText("active", { exact: true })).toBeVisible();
  await expect(busy).toContainText("in use in the last few minutes");
  const old = page.locator(".subagent-row", { hasText: "Old tablet" });
  await expect(old.getByText("active", { exact: true })).toHaveCount(0);
  await expect(old).toContainText("last seen 3h ago");
});

test("a #pair= link confirms on the phone and stores the token under its own key", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem("protoagent.authToken", "operator-token-on-this-browser"));
  await page.route("**/api/pairing/claim", (route) =>
    route.fulfill({ json: { ok: true, token: "fresh-device-token", device: { id: "dev-new", name: "Josh's iPhone", kind: "device" } } }),
  );
  await page.goto("/app/#pair=3HV8T-QW6RN", { waitUntil: "load" });
  await expect(page.getByText("Paired as Josh's iPhone")).toBeVisible();
  expect(new URL(page.url()).hash).toBe("");
  expect(await page.evaluate(() => window.localStorage.getItem("protoagent.deviceToken"))).toBe("fresh-device-token");
  // The operator token this browser already held is left exactly where it was.
  expect(await page.evaluate(() => window.localStorage.getItem("protoagent.authToken"))).toBe("operator-token-on-this-browser");
});

test("a phone paired before the key split keeps working and is migrated on boot", async ({ page }) => {
  // Pre-S3 storage: the device token sat in the OPERATOR key, beside its id. The init script
  // runs before every navigation, so seed it once only.
  await page.addInitScript(() => {
    if (window.sessionStorage.getItem("seeded")) return;
    window.sessionStorage.setItem("seeded", "1");
    window.localStorage.setItem("protoagent.authToken", "legacy-device-token");
    window.localStorage.setItem("protoagent.deviceId", "dev-phone1");
  });
  const bearers = new Set();
  page.on("request", (r) => {
    if (r.url().includes("/api/")) bearers.add(r.headers()["authorization"] || "");
  });
  await openDevices(page);
  expect(await page.evaluate(() => window.localStorage.getItem("protoagent.deviceToken"))).toBe("legacy-device-token");
  expect(await page.evaluate(() => window.localStorage.getItem("protoagent.authToken"))).toBeNull();
  // Same credential on the wire before and after — nobody was signed out.
  expect([...bearers].filter(Boolean)).toEqual(["Bearer legacy-device-token"]);
  await expect(page.locator(".subagent-row", { hasText: "Josh's iPhone" }).getByText("This device", { exact: true })).toBeVisible();
});

// ── #4336 S4: reach from anywhere (Tailscale HTTPS), the picked interface, restart ──────────

test("loopback-bound on a tailnet: HTTPS via Tailscale is offered first and pairs without a bind change", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-fleet": scope(test.info()), "x-e2e-pairing": "tailscale" });
  const writes = [];
  page.on("request", (r) => {
    if (r.url().endsWith("/api/settings")) writes.push(r.postDataJSON());
    if (r.url().endsWith("/api/pairing/tailscale/serve")) writes.push({ serve: r.method() });
  });
  await openDevices(page);
  await page.getByRole("button", { name: "Add a device" }).click();
  const step = page.getByRole("region", { name: "Make this agent reachable" });
  const option = step.getByTestId("reach-tailscale");
  await expect(option).toContainText("https://box.tail1234.ts.net");
  await expect(option).toContainText("needs the Tailscale app, signed in to the same tailnet");
  await option.getByRole("button", { name: "Use HTTPS via Tailscale" }).click();

  const card = page.getByRole("region", { name: "Pair a new device" });
  await expect(card.getByTestId("device-pair-url")).toHaveText("https://box.tail1234.ts.net/app/");
  await expect(card.getByTestId("device-pair-requirement")).toContainText("over HTTPS");
  await expect(card.getByTestId("device-pair-requirement")).toContainText("Tailscale app");
  // No bind was written and nothing needs a restart: serve keeps the agent on localhost.
  expect(writes).toEqual([{ serve: "POST" }]);
  await expect(page.getByRole("region", { name: "Restart required" })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "Turn off Tailscale HTTPS" })).toBeVisible();
});

test("picking an address binds THAT address, then Restart now reconnects and reopens the dialog", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-fleet": scope(test.info()), "x-e2e-pairing": "loopback" });
  const binds = [];
  page.on("request", (r) => {
    if (r.url().endsWith("/api/settings")) binds.push(r.postDataJSON());
  });
  let restarted = false;
  await page.route("**/api/restart", (route) => {
    restarted = true;
    return route.fulfill({ status: 202, contentType: "application/json", body: '{"ok":true,"restarting":true}' });
  });
  let probes = 0;
  await page.route("**/healthz", (route) => {
    probes += 1;
    // Down for the first probe, then back — the console waits for both.
    return probes === 1 ? route.abort() : route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
  });
  await openDevices(page);
  await page.getByRole("button", { name: "Add a device" }).click();
  const step = page.getByRole("region", { name: "Make this agent reachable" });
  await expect(step).toContainText("on localhost so this app keeps");
  await expect(step).toContainText("Tailscale app signed in to the same tailnet");
  await step.getByRole("button", { name: "Tailnet · 100.64.0.5" }).click();
  await expect.poll(() => binds).toEqual([{ updates: { "network.bind": "100.64.0.5" }, layer: "host" }]);

  const notice = page.getByRole("region", { name: "Restart required" });
  await notice.getByRole("button", { name: "Restart now" }).click();
  await expect(page.getByText("Restarted", { exact: true })).toBeVisible({ timeout: 15000 });
  expect(restarted).toBe(true);
  await expect(notice).toHaveCount(0);
  // The dialog reopened (still loopback in this mock, so the reach step is back).
  await expect(page.getByRole("region", { name: "Make this agent reachable" })).toBeVisible();
});

test("a --host launch can't apply a new bind: the panel says what to run instead of offering Restart", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-fleet": scope(test.info()), "x-e2e-pairing": "loopback", "x-e2e-restart": "pinned" });
  await openDevices(page);
  await page.getByRole("button", { name: "Add a device" }).click();
  await page.getByRole("region", { name: "Make this agent reachable" }).getByRole("button", { name: "Wi-Fi · 192.168.5.20" }).click();
  const notice = page.getByRole("region", { name: "Restart required" });
  await expect(notice.getByTestId("restart-hint")).toContainText("--host 127.0.0.1");
  await expect(notice.getByRole("button", { name: "Restart now" })).toHaveCount(0);
});

test("the device QR says a tailnet address needs Tailscale on the phone", async ({ page }) => {
  await openDevices(page);
  await page.getByRole("button", { name: "Add a device" }).click();
  await expect(page.getByRole("region", { name: "Pair a new device" }).getByTestId("device-pair-requirement")).toContainText(
    "the phone needs the Tailscale app, signed in to the same tailnet",
  );
});

// ── #4336 S7.2: device scope (ADR 0121 D4) ────────────────────────────────────────────────

/** Mock the devices list + PATCH statefully, as the given caller. Returns the PATCH bodies. */
async function mockScopedDevices(page, { caller, refusePatch = false }) {
  const now = Math.floor(Date.now() / 1000);
  const rows = [
    { id: "p-old", name: "Old iPhone", kind: "device", scope: "full", created_at: now - 9000, last_seen_at: null },
    { id: "p-new", name: "New Pixel", kind: "device", scope: "device", created_at: now - 900, last_seen_at: null },
    { id: "h-1", name: "studio-hub", kind: "agent", scope: "full", created_at: now - 9000, last_seen_at: null },
  ];
  const patches = [];
  await page.route("**/api/devices", (route) => route.fulfill({ json: { devices: rows, caller } }));
  await page.route("**/api/devices/*", async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    const body = route.request().postDataJSON();
    patches.push(body);
    if (refusePatch) {
      return route.fulfill({
        status: 403,
        json: { error: "device_scope", detail: { code: "device_scope", reason: "This device can't change pairing or credentials — use a full-access device." } },
      });
    }
    const id = route.request().url().split("/").pop();
    const row = rows.find((r) => r.id === id);
    if (body.scope) row.scope = body.scope;
    return route.fulfill({ json: { ok: true, device: row } });
  });
  return patches;
}

test("Full access and Limited badges; Limit and Promote change a phone's scope", async ({ page }) => {
  const patches = await mockScopedDevices(page, { caller: { scope: "full", device_id: null } });
  await openDevices(page);
  const old = page.locator(".subagent-row", { hasText: "Old iPhone" });
  const fresh = page.locator(".subagent-row", { hasText: "New Pixel" });
  const hub = page.locator(".subagent-row", { hasText: "studio-hub" });
  await expect(old.getByText("Full access", { exact: true })).toBeVisible();
  await expect(fresh.getByText("Limited", { exact: true })).toBeVisible();
  // An agent is always full access and can't be limited — no badge, no action.
  await expect(hub.getByText("Full access", { exact: true })).toHaveCount(0);
  await expect(hub.getByRole("button", { name: /^(Limit|Promote) / })).toHaveCount(0);

  await old.getByRole("button", { name: "Limit Old iPhone" }).click();
  await expect(page.getByText("Old iPhone is limited")).toBeVisible();
  await expect(old.getByText("Limited", { exact: true })).toBeVisible();

  await fresh.getByRole("button", { name: "Promote New Pixel" }).click();
  await expect(page.getByText("New Pixel has full access")).toBeVisible();
  await expect(fresh.getByText("Full access", { exact: true })).toBeVisible();
  expect(patches).toEqual([{ scope: "device" }, { scope: "full" }]);
});

test("a limited console can't Promote, Limit, pair or manage other devices — only itself", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("protoagent.deviceToken", "e2e-device-token");
    window.localStorage.setItem("protoagent.deviceId", "p-new");
  });
  await mockScopedDevices(page, { caller: { scope: "device", device_id: "p-new" } });
  await openDevices(page);
  await expect(page.getByTestId("devices-limited-note")).toContainText("This device has limited access.");
  await expect(page.getByRole("button", { name: "Add a device" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Pair an agent" })).toBeDisabled();
  await expect(page.getByRole("button", { name: /^(Limit|Promote) / })).toHaveCount(0);

  const old = page.locator(".subagent-row", { hasText: "Old iPhone" });
  await expect(old.getByRole("button", { name: "Remove Old iPhone" })).toHaveCount(0);
  await expect(old.getByRole("button", { name: "Rename Old iPhone" })).toHaveCount(0);
  const mine = page.locator(".subagent-row", { hasText: "New Pixel" });
  await expect(mine.getByText("This device", { exact: true })).toBeVisible();
  await expect(mine.getByRole("button", { name: "Rename New Pixel" })).toBeVisible();
  await expect(mine.getByRole("button", { name: "Remove New Pixel" })).toBeVisible();
});

test("a device-scope refusal from the server reads as a sentence, not a code", async ({ page }) => {
  // The panel believed it was full access (a stale answer); the server is the authority.
  await mockScopedDevices(page, { caller: { scope: "full", device_id: null }, refusePatch: true });
  await openDevices(page);
  await page.locator(".subagent-row", { hasText: "New Pixel" }).getByRole("button", { name: "Promote New Pixel" }).click();
  await expect(page.getByText("This device can't change pairing or credentials — use a full-access device.")).toBeVisible();
  await expect(page.getByText("device_scope")).toHaveCount(0);
});

// ── ADR 0121 S7.4: a phone without Tailscale, through your own public URL ──────────────────

test("no public URL: the panel says how to set one up, and nothing warns", async ({ page }) => {
  await openDevices(page);
  const hint = page.getByTestId("public-console-hint");
  await expect(hint).toContainText("network.public_console_url");
  await expect(hint.getByRole("link", { name: "Set up a public URL" })).toHaveAttribute(
    "href",
    /guides\/phone-access#without-tailscale$/,
  );
  await expect(page.getByTestId("public-console-warning")).toHaveCount(0);
});

test("public URL set: a persistent warning, and 'Phone without Tailscale' points the QR and URL at it", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-fleet": scope(test.info()), "x-e2e-pairing": "public" });
  await openDevices(page);
  // Persistent: shown as soon as the panel opens, before any pairing starts.
  const warning = page.getByTestId("public-console-warning");
  await expect(warning).toContainText(
    "Anyone on the internet can reach this console's sign-in. Put Cloudflare Access (or another identity-aware proxy) in front of it.",
  );
  await expect(warning).toContainText("https://agent.example.com");
  await expect(warning.getByRole("link", { name: "How to put Access in front of it" })).toHaveAttribute(
    "href",
    /guides\/exposing-protoagent#console-hostname$/,
  );
  await expect(page.getByTestId("public-console-hint")).toHaveCount(0);

  await page.getByRole("button", { name: "Add a device" }).click();
  const card = page.getByRole("region", { name: "Pair a new device" });
  // Preselected (the best path when there's no tailnet HTTPS).
  await expect(card.getByTestId("device-pair-url")).toHaveText("https://agent.example.com/app/");
  await expect(card.getByTestId("device-pair-requirement")).toContainText("no Tailscale needed");
  // Switch to the tailnet address and back: the option drives the typed-code URL.
  await card.getByRole("button", { name: "Tailnet · 100.64.0.5" }).click();
  await expect(card.getByTestId("device-pair-url")).toHaveText("http://100.64.0.5:7871/app/");
  await card.getByRole("button", { name: "Phone without Tailscale · agent.example.com" }).click();
  await expect(card.getByTestId("device-pair-url")).toHaveText("https://agent.example.com/app/");
  // The warning stays while the code is live.
  await expect(warning).toBeVisible();
});
