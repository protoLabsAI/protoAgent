import { expect, test } from "@playwright/test";
import { expectNoAxeViolations } from "./axe";

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

for (const colorScheme of ["light", "dark"] as const) {
  test(`Settings Devices has no axe accessibility violations in ${colorScheme} mode`, async ({ page }) => {
    await page.emulateMedia({ colorScheme });
    await page.route("**/api/theme", (route) => route.fulfill({
      json: { theme: { mode: colorScheme, overrides: {} } },
    }));
    await openDevices(page);
    await expect(page.locator("html")).toHaveAttribute("data-theme", colorScheme);
    await expect(page.getByRole("button", { name: "Remove studio-hub" })).toBeVisible();
    await expect(page.getByTestId("public-console-hint")).toBeVisible();
    await expectNoAxeViolations(page, { include: ".settings-overlay" });
  });
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

  // name:"Cancel" is EXACT: the default-open Work surface can render a work-row whose status badge
  // reads "Cancelled", which a substring (non-exact) match would also hit. The pairing Cancel
  // button sits beside the card, not inside it, so it can't be region-scoped.
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
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
  // name:"Cancel" is EXACT — see the agent-pairing test above (a "Cancelled" work-row from the
  // default-open Work surface would otherwise match this substring).
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
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

// ── #4336 S7.3: lifetime — weekly rotation and idle expiry (ADR 0121 D5) ─────────────────

test("a due phone rotates its token on boot; the next request carries the new one", async ({ page }) => {
  await page.addInitScript(() => {
    if (window.sessionStorage.getItem("seeded")) return;
    window.sessionStorage.setItem("seeded", "1");
    window.localStorage.setItem("protoagent.deviceToken", "old-device-token");
    window.localStorage.setItem("protoagent.deviceId", "dev-phone1");
  });
  const rotations = [];
  // Due only for the OLD token — once rotated, the server would say it isn't.
  await page.route("**/api/devices/self", (route) => {
    const due = route.request().headers()["authorization"] === "Bearer old-device-token";
    return route.fulfill({
      json: { device: { id: "dev-phone1", name: "Josh's iPhone", kind: "device", scope: "device", rotate_due: due, created_at: 1, last_seen_at: null } },
    });
  });
  await page.route("**/api/devices/self/rotate", (route) => {
    rotations.push(route.request().headers()["authorization"]);
    return route.fulfill({ json: { ok: true, token: "rotated-device-token", device: { id: "dev-phone1" } } });
  });
  await openDevices(page);
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem("protoagent.deviceToken"))).toBe("rotated-device-token");
  expect(rotations).toEqual(["Bearer old-device-token"]);

  // From here every request sends the rotated token — and it isn't rotated again.
  const bearers = new Set();
  page.on("request", (r) => {
    if (r.url().includes("/api/")) bearers.add(r.headers()["authorization"] || "");
  });
  await openDevices(page);
  await expect(page.locator(".subagent-row", { hasText: "Josh's iPhone" })).toBeVisible();
  expect([...bearers].filter(Boolean)).toEqual(["Bearer rotated-device-token"]);
  expect(rotations).toHaveLength(1);
});

test("a browser holding only the shared bearer never asks /api/devices/self", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem("protoagent.authToken", "e2e-operator-token"));
  const selfCalls = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/devices/self")) selfCalls.push(r.url());
  });
  await openDevices(page);
  await expect(page.locator(".subagent-row", { hasText: "Josh's iPhone" })).toBeVisible();
  expect(selfCalls).toEqual([]); // no device token here: nothing to rotate, nothing asked
});

test("an expired phone shows Expired with remove-and-re-pair guidance; a limited one shows its expiry", async ({ page }) => {
  const now = Math.floor(Date.now() / 1000);
  await page.route("**/api/devices", (route) =>
    route.fulfill({
      json: {
        devices: [
          { id: "x1", name: "Drawer phone", kind: "device", scope: "device", created_at: now - 40 * 86400, last_seen_at: now - 31 * 86400, expired: true, expires_at: now - 86400 },
          { id: "x2", name: "Daily phone", kind: "device", scope: "device", created_at: now - 9000, last_seen_at: now - 3 * 3600, expired: false, expires_at: now - 3 * 3600 + 30 * 86400 },
          { id: "x3", name: "Old iPad", kind: "device", scope: "full", created_at: now - 9000, last_seen_at: now - 3 * 3600, expired: false, expires_at: null },
        ],
        caller: { scope: "full", device_id: null },
      },
    }),
  );
  await openDevices(page);
  const drawer = page.locator(".subagent-row", { hasText: "Drawer phone" });
  await expect(drawer.getByText("Expired", { exact: true })).toBeVisible();
  await expect(drawer).toContainText("remove it, then pair the phone again");
  await expect(drawer.getByRole("button", { name: "Remove Drawer phone" })).toBeVisible();
  const daily = page.locator(".subagent-row", { hasText: "Daily phone" });
  await expect(daily.getByText("Expired", { exact: true })).toHaveCount(0);
  await expect(daily.getByTestId("device-expiry-x2")).toHaveText(/expires in 30 days if unused/);
  await expect(page.getByTestId("device-expiry-x3")).toHaveCount(0); // full access never expires
});

// ── #4336 S7.5: the address set and "Move this phone to…" (ADR 0121 D1) ─────────────────

const TS_HTTPS = "https://box.tail1234.ts.net";
const PUBLIC_URL = "https://agent.example.com";

/** A paired phone whose home is this page's origin, with the agent's other addresses stored
 *  — and the boot /self call answering with the same set. */
async function asPairedPhone(page) {
  await page.addInitScript(
    ([tsHttps, publicUrl]) => {
      if (window.sessionStorage.getItem("seeded")) return;
      window.sessionStorage.setItem("seeded", "1");
      window.localStorage.setItem("protoagent.deviceToken", "e2e-device-token");
      window.localStorage.setItem("protoagent.deviceId", "dev-phone1");
      window.localStorage.setItem(
        "protoagent.deviceAddresses",
        JSON.stringify([
          { kind: "tailnet_https", origin: tsHttps, requirement: "tailscale" },
          { kind: "public", origin: publicUrl },
          { kind: "lan", origin: window.location.origin, requirement: "same_network" },
        ]),
      );
    },
    [TS_HTTPS, PUBLIC_URL],
  );
  await page.route("**/api/devices/self", (route) =>
    route.fulfill({
      json: {
        device: { id: "dev-phone1", name: "Josh's iPhone", kind: "device", scope: "device", rotate_due: false, created_at: 1, last_seen_at: null },
        addresses: [
          { kind: "tailnet_https", origin: TS_HTTPS, requirement: "tailscale" },
          { kind: "public", origin: PUBLIC_URL },
          { kind: "lan", origin: new URL(route.request().url()).origin, requirement: "same_network" },
        ],
      },
    }),
  );
}

test("Move this phone to…: pick another address, confirm, and the console hands off to it", async ({ page }) => {
  await asPairedPhone(page);
  const handoffs = [];
  await page.route("**/api/devices/self/handoff", (route) => {
    handoffs.push([route.request().headers()["authorization"], route.request().postDataJSON()]);
    return route.fulfill({ json: { ok: true, code: "K7QM2-XPA4F", ttl: 60, origin: TS_HTTPS, url: `${TS_HTTPS}/app/#pair=K7QM2-XPA4F` } });
  });
  // The new home, stubbed: the test only needs to see the navigation land there.
  await page.route(`${TS_HTTPS}/**`, (route) => route.fulfill({ contentType: "text/html", body: "<title>new home</title>" }));
  await openDevices(page);

  const mine = page.locator(".subagent-row", { hasText: "Josh's iPhone" });
  await expect(page.locator(".subagent-row", { hasText: "studio-hub" }).getByRole("button", { name: "Move this phone to…" })).toHaveCount(0);
  await mine.getByRole("button", { name: "Move this phone to…" }).click();
  const picker = mine.getByRole("group", { name: "Move this phone" });
  // The other addresses only — never the one this phone is on — each with what it needs.
  await expect(picker.getByRole("button")).toHaveText(["Tailnet HTTPS · box.tail1234.ts.net", "Public URL · agent.example.com", "Cancel"]);
  await expect(picker).toContainText("needs the Tailscale app on this phone");
  await expect(picker).toContainText("works from any network");

  // Picking is not moving: a confirm step first, and Back returns to the list.
  await picker.getByRole("button", { name: "Tailnet HTTPS · box.tail1234.ts.net" }).click();
  await expect(picker.getByTestId("device-move-confirm")).toContainText("Move this phone to box.tail1234.ts.net?");
  expect(handoffs).toEqual([]);
  await picker.getByRole("button", { name: "Back" }).click();
  await picker.getByRole("button", { name: "Tailnet HTTPS · box.tail1234.ts.net" }).click();
  await picker.getByRole("button", { name: "Move", exact: true }).click();

  await page.waitForURL(`${TS_HTTPS}/app/#pair=K7QM2-XPA4F`);
  expect(handoffs).toEqual([["Bearer e2e-device-token", { origin: TS_HTTPS }]]);
});

test("a refused move stays put and says why", async ({ page }) => {
  await asPairedPhone(page);
  await page.route("**/api/devices/self/handoff", (route) =>
    route.fulfill({ status: 400, json: { ok: false, error: "origin_not_offered", detail: "that address isn't one this agent offers" } }),
  );
  await openDevices(page);
  const mine = page.locator(".subagent-row", { hasText: "Josh's iPhone" });
  await mine.getByRole("button", { name: "Move this phone to…" }).click();
  const picker = mine.getByRole("group", { name: "Move this phone" });
  await picker.getByRole("button", { name: "Public URL · agent.example.com" }).click();
  await picker.getByRole("button", { name: "Move", exact: true }).click();
  await expect(picker.getByRole("alert")).toHaveText("That address isn't one this agent offers.");
  await expect(page.getByRole("heading", { name: "Devices" })).toBeVisible();
});

test("a browser holding only the shared bearer is offered no move", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem("protoagent.authToken", "e2e-operator-token"));
  await openDevices(page);
  await expect(page.locator(".subagent-row", { hasText: "Josh's iPhone" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Move this phone to…" })).toHaveCount(0);
});

test("the offline notice lists the phone's other addresses when its home stops answering", async ({ page }) => {
  // The notice probes the phone's other addresses for an "answering" hint. Those are fixture
  // origins here; under an enforced console CSP the probe only reaches the agent's configured
  // public URLs (connect-src), so this runs without the policy (the server reports it by default).
  await page.setExtraHTTPHeaders({ "x-e2e-csp": "off" });
  await asPairedPhone(page);
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".app-shell-main")).toBeVisible();
  await expect(page.getByTestId("home-origin-notice")).toHaveCount(0);

  // The home goes away (the phone left the Wi-Fi it was paired on).
  await page.route("**/healthz", (route) => route.abort("internetdisconnected"));
  for (let i = 0; i < 2; i++) await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  const notice = page.getByTestId("home-origin-notice");
  await expect(notice).toBeVisible();
  await expect(notice.getByRole("link")).toHaveText(["Tailnet HTTPS · box.tail1234.ts.net", "Public URL · agent.example.com"]);
  await expect(notice.getByRole("link").first()).toHaveAttribute("href", `${TS_HTTPS}/app/`);
  await expect(notice).toContainText("needs the Tailscale app on this phone");
  await expect(notice).toContainText("Opening one means pairing this phone again there.");

  // Back in range: it clears.
  await page.unroute("**/healthz");
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect(notice).toHaveCount(0);
});

test("the origin a phone moved away from points at its new home once the move completed", async ({ page }) => {
  await page.addInitScript((tsHttps) => {
    if (window.sessionStorage.getItem("seeded")) return;
    window.sessionStorage.setItem("seeded", "1");
    window.localStorage.setItem("protoagent.deviceToken", "e2e-device-token");
    window.localStorage.setItem("protoagent.deviceId", "dev-phone1");
    window.localStorage.setItem("protoagent.deviceMovedTo", tsHttps);
  }, TS_HTTPS);
  await page.route("**/api/devices/self", (route) =>
    route.fulfill({
      json: { device: { id: "dev-phone1", name: "Josh's iPhone", kind: "device", scope: "device", rotate_due: false, handed_off: true, created_at: 1, last_seen_at: null } },
    }),
  );
  await page.goto("/app/", { waitUntil: "load" });
  const gate = page.getByRole("dialog", { name: "Authentication required" });
  await expect(gate.getByTestId("auth-moved")).toContainText("This phone moved to box.tail1234.ts.net");
  await expect(gate.getByRole("link", { name: "box.tail1234.ts.net" })).toHaveAttribute("href", `${TS_HTTPS}/app/`);
  expect(await page.evaluate(() => window.localStorage.getItem("protoagent.deviceToken"))).toBeNull();
});

test("past the grace, the old origin's refused token is dropped and the gate points at the new home", async ({ page }) => {
  await page.addInitScript((tsHttps) => {
    if (window.sessionStorage.getItem("seeded")) return;
    window.sessionStorage.setItem("seeded", "1");
    window.localStorage.setItem("protoagent.deviceToken", "e2e-device-token");
    window.localStorage.setItem("protoagent.deviceId", "dev-phone1");
    window.localStorage.setItem("protoagent.deviceMovedTo", tsHttps);
  }, TS_HTTPS);
  await page.route("**/api/devices/self", (route) => route.fulfill({ status: 401, json: { detail: "unauthorized" } }));
  await page.goto("/app/", { waitUntil: "load" });
  const gate = page.getByRole("dialog", { name: "Authentication required" });
  await expect(gate.getByTestId("auth-moved")).toContainText("This phone moved to box.tail1234.ts.net");
  expect(await page.evaluate(() => window.localStorage.getItem("protoagent.deviceToken"))).toBeNull();
});

// ── Notification key (ADR 0135, #5539) ────────────────────────────────────────────────────

test("a full-access console sees the notification key and rotates it behind a confirm", async ({ page }) => {
  await mockScopedDevices(page, { caller: { scope: "full", device_id: null } });
  const retires = Math.floor(Date.now() / 1000) + 20 * 86400;
  let keys = [
    { version: 1, public_key: "BOLDPUBLICKEY", created_at: 1, active: false, retire_after: retires },
    { version: 2, public_key: "BNEWPUBLICKEY", created_at: 2, active: true, retire_after: null },
  ];
  let posts = 0;
  await page.route("**/api/push/keys", (route) => route.fulfill({
    json: { keys, rotation_window_seconds: 2592000, bindings_by_version: { 1: 2, 2: 1 } },
  }));
  await page.route("**/api/push/keys/rotate", (route) => {
    posts += 1;
    keys = [{ ...keys[1], active: false, retire_after: retires }, { version: 3, public_key: "BTHIRD", created_at: 3, active: true, retire_after: null }];
    return route.fulfill({ json: keys[1] });
  });
  await openDevices(page);
  const card = page.getByTestId("notification-key-card");
  await expect(card).toContainText("Key version 2 · active");
  await expect(card).toContainText(/Version 1 retires on \d{1,2} \w+\. 2 devices still use it\./);
  await expect(card).not.toContainText("PUBLICKEY");

  const open = card.getByRole("button", { name: "Rotate key…" });
  await open.click();
  const confirm = card.getByRole("group", { name: "Confirm key rotation" });
  await expect(confirm).toContainText("Every device must renew within 30 days.");
  await expect(confirm.getByRole("button", { name: "Cancel" })).toBeFocused();
  await expectNoAxeViolations(page, { include: '[data-testid="notification-key-card"]' });

  // Escape cancels the rotation only: Settings stays open and focus returns to the opener.
  await page.keyboard.press("Escape");
  await expect(confirm).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Devices" })).toBeVisible();
  await expect(open).toBeFocused();
  expect(posts).toBe(0);

  await open.click();
  await confirm.getByRole("button", { name: "Rotate key", exact: true }).click();
  await expect(card).toContainText("Key version 3 · active");
  await expect(confirm).toHaveCount(0);
  expect(posts).toBe(1);
});

test("a limited console gets no notification key card", async ({ page }) => {
  await mockScopedDevices(page, { caller: { scope: "device", device_id: "p-new" } });
  await openDevices(page);
  await expect(page.locator(".subagent-row", { hasText: "New Pixel" })).toBeVisible();
  await expect(page.getByTestId("notification-key-card")).toHaveCount(0);
});

// ── Notifications on this device, and each row's push line (ADR 0135 W2e, #5539) ─────────
//
// Headless Chromium has no push service and no permission prompt, so the browser half
// (permission, the stored subscription) is stubbed in the page and the agent half
// (`/api/devices`, `/api/devices/self/push`) with `page.route`. The prompt itself is a
// manual check on a real device.

const PUSH_KEY = "AQID"; // base64url of bytes 1,2,3: the key the fake subscription was made with

type BrowserPush = { permission?: "default" | "granted" | "denied"; subscribed?: boolean; supported?: boolean };

/** Stub what the browser knows: its permission and whether it holds a subscription. */
async function stubBrowserPush(page, { permission = "default", subscribed = false, supported = true }: BrowserPush = {}) {
  await page.addInitScript(
    ([permission, subscribed, supported, key]) => {
      if (!supported) {
        delete (window as { PushManager?: unknown }).PushManager;
        return;
      }
      Object.defineProperty(Notification, "permission", { configurable: true, get: () => permission });
      const bytes = Uint8Array.from(atob(key as string), (c) => c.charCodeAt(0));
      const subscription = subscribed
        ? {
            endpoint: "https://web.push.apple.com/e2e",
            expirationTime: null,
            options: { applicationServerKey: bytes.buffer, userVisibleOnly: true },
            toJSON: () => ({ endpoint: "https://web.push.apple.com/e2e", keys: { p256dh: "e2e", auth: "e2e" } }),
            unsubscribe: async () => true,
          }
        : null;
      const pushManager = { getSubscription: async () => subscription, subscribe: async () => subscription };
      // The card reads the registration; boot's silent renew goes through the real worker.
      ServiceWorkerContainer.prototype.getRegistration = async () => ({ pushManager }) as unknown as ServiceWorkerRegistration;
      PushManager.prototype.getSubscription = pushManager.getSubscription as PushManager["getSubscription"];
      PushManager.prototype.subscribe = pushManager.subscribe as PushManager["subscribe"];
    },
    [permission, subscribed, supported, PUSH_KEY] as const,
  );
}

/** This browser as the paired phone `dev-phone1`, whose row carries `push`. */
async function asPushDevice(page, { push = null, renew = null }: { push?: object | null; renew?: string | null } = {}) {
  await page.addInitScript(() => {
    window.localStorage.setItem("protoagent.deviceToken", "e2e-device-token");
    window.localStorage.setItem("protoagent.deviceId", "dev-phone1");
  });
  const now = Math.floor(Date.now() / 1000);
  await page.route("**/api/devices", (route) =>
    route.fulfill({
      json: {
        devices: [
          { id: "dev-phone1", name: "Josh's iPhone", kind: "device", scope: "device", created_at: now - 9000, last_seen_at: now - 60, push, push_renew: renew },
        ],
        caller: { scope: "device", device_id: "dev-phone1" },
      },
    }),
  );
}

/** Answer `/api/devices/self/push` (any method) with this device's status on the agent. */
async function stubSelfPush(page, { bound = false, renew = null }: { bound?: boolean; renew?: string | null } = {}) {
  const now = Math.floor(Date.now() / 1000);
  const binding = bound ? { vapid_version: 2, generation: 1, created_at: now - 9 * 86400, renewed_at: now - 2 * 86400 - 60 } : null;
  await page.route("**/api/devices/self/push", (route) =>
    route.fulfill({ json: { public_key: PUSH_KEY, version: 2, binding, renew } }),
  );
}

const APPLE_BINDING = {
  vapid_version: 2, generation: 1, created_at: 1, renewed_at: 1, provider: "apple",
  last_attempt_at: null, last_accepted_at: null, last_error: null, last_error_at: null,
};

type PushCase = {
  name: string;
  state: string;
  arrange: (page) => Promise<void>;
  check: (card, page) => Promise<void>;
};

const PUSH_CARD_STATES: PushCase[] = [
  {
    name: "off",
    state: "off",
    arrange: async (page) => {
      await stubBrowserPush(page);
      await asPushDevice(page);
      await stubSelfPush(page);
    },
    check: async (card) => {
      await expect(card.getByTestId("push-state")).toContainText("Off on this device");
      await expect(card.getByTestId("push-state")).toContainText("Get alerts on this device even when the console is closed.");
      await expect(card.getByRole("button", { name: "Turn on notifications" })).toBeEnabled();
    },
  },
  {
    name: "on, with its push service",
    state: "on",
    arrange: async (page) => {
      await stubBrowserPush(page, { permission: "granted", subscribed: true });
      await asPushDevice(page, { push: APPLE_BINDING });
      await stubSelfPush(page, { bound: true });
    },
    check: async (card) => {
      await expect(card.getByTestId("push-state").getByText("On", { exact: true })).toBeVisible();
      await expect(card.getByTestId("push-state")).toContainText("Push service: Apple");
      await expect(card.getByTestId("push-state")).toContainText("Renewed 2 days ago");
      await expect(card.getByRole("button", { name: "Turn off on this device" })).toBeEnabled();
      await expect(card.getByRole("button", { name: "Turn on notifications" })).toHaveCount(0);
    },
  },
  ...(
    [
      ["vapid_key", "This agent changed its notification key."],
      ["handoff", "This phone moved to a new address, which needs its own notifications."],
      ["idle", "This device went unused for too long."],
    ] as const
  ).map(([reason, sentence]): PushCase => ({
    name: `renew (${reason})`,
    state: "renew",
    arrange: async (page) => {
      await stubBrowserPush(page, { permission: "granted" });
      await asPushDevice(page, { renew: reason });
      await stubSelfPush(page, { renew: reason });
    },
    check: async (card) => {
      await expect(card.getByTestId("push-state")).toContainText("Renew on this device");
      await expect(card.getByTestId("push-state")).toContainText(sentence);
      await expect(card.getByRole("button", { name: "Renew notifications" })).toBeEnabled();
    },
  })),
  {
    name: "blocked",
    state: "denied",
    arrange: async (page) => {
      await stubBrowserPush(page, { permission: "denied" });
      await asPushDevice(page);
      await stubSelfPush(page);
    },
    check: async (card) => {
      await expect(card.getByTestId("push-state")).toContainText("Blocked");
      await expect(card.getByTestId("push-state")).toContainText("Notifications are blocked in this browser's settings.");
      await expect(card.getByTestId("push-state")).toContainText("then allow notifications for this site.");
      // Only the browser can undo a block: no button pretends otherwise.
      await expect(card.getByRole("button")).toHaveCount(0);
    },
  },
  {
    name: "not supported",
    state: "unsupported",
    arrange: async (page) => {
      await stubBrowserPush(page, { supported: false });
      await asPushDevice(page);
      await stubSelfPush(page);
    },
    check: async (card) => {
      await expect(card.getByTestId("push-state")).toContainText("Not supported");
      await expect(card.getByTestId("push-state")).toContainText("This browser can't get notifications.");
      await expect(card.getByRole("button")).toHaveCount(0);
    },
  },
  {
    name: "shared sign-in",
    state: "shared",
    // No device token in this browser: there is no device to ask about.
    arrange: async (page) => {
      await stubBrowserPush(page);
      await stubSelfPush(page);
    },
    check: async (card, page) => {
      await expect(card.getByTestId("push-state")).toContainText("Shared sign-in");
      await expect(card.getByTestId("push-state")).toContainText("Pair this browser to get notifications.");
      await expect(card.getByRole("button", { name: "Turn on notifications" })).toHaveCount(0);
      await card.getByRole("button", { name: "Pair this device" }).click();
      await expect(page.getByRole("region", { name: "Pair a new device" })).toBeVisible();
    },
  },
  {
    name: "can't check",
    state: "trouble",
    arrange: async (page) => {
      await stubBrowserPush(page);
      await asPushDevice(page);
      await page.route("**/api/devices/self/push", (route) => route.fulfill({ status: 500, json: { error: "boom" } }));
    },
    check: async (card) => {
      await expect(card.getByTestId("push-state")).toHaveText("Couldn't check notifications on this device.");
      // A failed read is never shown as "off": no Turn on, only Try again.
      await expect(card.getByRole("button", { name: "Turn on notifications" })).toHaveCount(0);
      await expect(card.getByRole("button", { name: "Try again" })).toBeEnabled();
    },
  },
];

for (const width of [1280, 390]) {
  for (const { name, state, arrange, check } of PUSH_CARD_STATES) {
    test(`PushCard at ${width}px: ${name}`, async ({ page }) => {
      await arrange(page);
      await openDevices(page);
      // Settings is opened at desktop width, then narrowed: the card must hold at both.
      await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
      const card = page.getByTestId("push-card");
      await card.scrollIntoViewIfNeeded();
      await expect(card.getByRole("heading", { name: "Notifications on this device" })).toBeVisible();
      await expect(card).toHaveAttribute("data-state", state);
      await expectNoAxeViolations(page, { include: '[data-testid="push-card"]' });
      const overflow = await card.evaluate((el) => el.scrollWidth - el.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
      await check(card, page);
    });
  }
}

test("a 403 not_a_device answer also reads as a shared sign-in, not as a failure", async ({ page }) => {
  await stubBrowserPush(page);
  await asPushDevice(page);
  await page.route("**/api/devices/self/push", (route) => route.fulfill({ status: 403, json: { error: "not_a_device" } }));
  await openDevices(page);
  const card = page.getByTestId("push-card");
  await expect(card).toHaveAttribute("data-state", "shared");
  await expect(card.getByTestId("push-state")).toContainText("Pair this browser to get notifications.");
});

test("Try again re-reads a failed check and lands on what is true now", async ({ page }) => {
  await stubBrowserPush(page);
  await asPushDevice(page);
  let fail = true;
  await page.route("**/api/devices/self/push", (route) =>
    fail
      ? route.fulfill({ status: 500, json: { error: "boom" } })
      : route.fulfill({ json: { public_key: PUSH_KEY, version: 2, binding: null, renew: null } }),
  );
  await openDevices(page);
  const card = page.getByTestId("push-card");
  await expect(card).toHaveAttribute("data-state", "trouble");
  fail = false;
  await card.getByRole("button", { name: "Try again" }).click();
  await expect(card).toHaveAttribute("data-state", "off");
  await expect(card.getByTestId("push-status")).toHaveText("Notifications are off for this device.");
});

test("What leaves this machine opens to the four disclosure lines", async ({ page }) => {
  await stubBrowserPush(page);
  await asPushDevice(page);
  await stubSelfPush(page);
  await openDevices(page);
  const card = page.getByTestId("push-card");
  const lines = card.locator(".devices-push-disclosure p");
  await expect(lines.first()).toBeHidden();
  await card.getByText("What leaves this machine", { exact: true }).click();
  await expect(lines).toHaveText([
    "Alerts go straight from this machine to your browser's push service: Apple, Google or Mozilla. No protoAgent server sits in between, and you don't need a chat app.",
    'The alert says only "protoAgent needs your attention" and "Open the console to view a decision" (or "an update"). It never includes the question, chat text, job output, names or file paths.',
    "The push service can see when alerts are sent, how big they are and this machine's public IP address.",
    "Opening an alert asks you to sign in to the console. Nothing can be approved from the lock screen.",
  ]);
  await expect(lines.first()).toBeVisible();
  await expectNoAxeViolations(page, { include: '[data-testid="push-card"]' });
});

test("each device row says where its push stands: off, on, accepted, didn't accept, not reported yet", async ({ page }) => {
  const now = Math.floor(Date.now() / 1000);
  const row = (id: string, name: string, extra: object) =>
    ({ id, name, kind: "device", scope: "device", created_at: now - 9000, last_seen_at: null, ...extra });
  const binding = (provider: string, extra: object = {}) => ({ ...APPLE_BINDING, provider, ...extra });
  await page.route("**/api/devices", (route) =>
    route.fulfill({
      json: {
        devices: [
          row("r-off", "Off phone", { push: null }),
          row("r-on", "On phone", { push: binding("apple") }),
          row("r-ok", "Accepted phone", { push: binding("google", { last_attempt_at: now - 120, last_accepted_at: now - 119 }) }),
          row("r-err", "Refused phone", {
            push: binding("mozilla", { last_attempt_at: now - 120, last_error_at: now - 119, last_error: "timeout" }),
          }),
          row("r-code", "Odd phone", {
            push: binding("apple", { last_attempt_at: now - 120, last_error_at: now - 119, last_error: "http_418" }),
          }),
          row("r-renew", "Moved phone", { push: null, push_renew: "handoff" }),
          row("r-old", "Unreported phone", {}), // a server from before push status: no `push` key at all
          { id: "h-1", name: "studio-hub", kind: "agent", scope: "full", created_at: now - 9000, last_seen_at: null },
        ],
        caller: { scope: "full", device_id: null },
      },
    }),
  );
  await openDevices(page);
  const line = (name: string) => page.locator(".subagent-row", { hasText: name }).locator(".devices-push-line");
  await expect(line("Off phone")).toHaveText("Push off");
  await expect(line("On phone")).toHaveText("Push on · Apple");
  await expect(line("Accepted phone")).toHaveText(/^Push on · Google accepted the last alert at \d/);
  await expect(line("Refused phone")).toContainText("Push on · Mozilla didn't accept the last alert");
  await expect(line("Refused phone")).toContainText("The push service took too long to answer.");
  // An error the console has no sentence for still shows its code, never nothing.
  await expect(line("Odd phone")).toContainText("Error code: http_418");
  await expect(line("Moved phone")).toContainText("Renew needed");
  await expect(line("Moved phone")).toContainText("This phone moved to a new address, which needs its own notifications.");
  await expect(line("Unreported phone")).toHaveText("Not reported yet");
  // Another agent's hub is not a browser: no push line at all.
  await expect(line("studio-hub")).toHaveCount(0);
  await expect(page.getByText("Accepted means the push service took the alert. It doesn't mean you saw it.")).toBeVisible();
  await expectNoAxeViolations(page, { include: ".devices-list" });
});
