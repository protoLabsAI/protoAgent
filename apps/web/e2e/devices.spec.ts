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
