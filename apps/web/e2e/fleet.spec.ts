import { expect, test } from "@playwright/test";

// Fleet manager + archetype picker (Settings → Agents, ADR 0042). Drives the live
// control-plane endpoints (mocked): list, create from an archetype, stop. The mock
// FLEET is shared module state, so run serially + assert by presence (not exact counts).

test.describe.configure({ mode: "serial" });

// This spec MUTATES the mock fleet (create / stop / rename / add-remote). Claim a
// private fleet scope (the mock keys state on x-e2e-fleet) and reset it to baseline
// before every test — including serial-group retries — so a write can never leak
// into the next test, a retry, or another spec. The scope is keyed on the parallel
// worker so even concurrent runners (repeat-each, if mode:serial is ever lifted)
// stay isolated from each other.
test.beforeEach(async ({ page }, testInfo) => {
  const scope = `fleet-spec-${testInfo.parallelIndex}`;
  await page.setExtraHTTPHeaders({ "x-e2e-fleet": scope }); // app fetches carry it
  await page.request.post("/api/__test__/fleet/reset", { headers: { "x-e2e-fleet": scope } });
});

async function openFleet(page) {
  // Fleet lives in the Box group of the consolidated settings dialog (host console), opened
  // from the header hamburger → app drawer → Settings (folded in from the old Box rail surface).
  await page.getByTestId("header-menu").click();
  await page.getByTestId("app-drawer").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Fleet", exact: true }).click();
}

async function openAgents(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await openFleet(page);
}

// Fleet lives in the consolidated settings dialog (a modal) now, so its backdrop intercepts
// the topbar switcher — close it before interacting with the top bar.
async function closeOverlay(page) {
  await page.locator(".settings-overlay .pl-dialog__close").click();
  await expect(page.locator(".settings-overlay")).toHaveCount(0);
}

test("Agents tab lists the host (this instance) + peers, host active by default", async ({ page }) => {
  await openAgents(page);
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
  // The host self-registers — it's always present + marked "this instance", and focused
  // (active) when no peer is — so the panel is never "0 agents".
  await expect(page.getByText("this instance").first()).toBeVisible(); // DS Badge (#832)
  await expect(page.locator(".fleet-row.active .fleet-name")).toContainText("main");
  await expect(page.locator(".fleet-row", { hasText: "ava" })).toBeVisible();
  await expect(page.locator(".fleet-row", { hasText: "roxy" })).toBeVisible();
  // The host row has no stop/remove (can't act on itself); peers do.
  await expect(page.locator(".fleet-row", { hasText: "main" }).getByRole("button")).toHaveCount(0);
});

test("New agent → archetype picker → create returns to the list", async ({ page }) => {
  await openAgents(page);
  await page.getByRole("button", { name: "New agent" }).click();
  await expect(page.getByRole("heading", { name: "New agent" })).toBeVisible();
  await expect(page.locator(".pl-radiocard")).toHaveCount(2); // DS RadioCard, from GET /api/archetypes
  await page.locator(".pl-radiocard", { hasText: "Project Manager" }).click();
  await page.getByLabel("Agent name").fill("newbot");
  await page.getByRole("button", { name: /Create/ }).click();
  await expect(page.locator(".fleet-row", { hasText: "newbot" })).toBeVisible();
});

test("stop a running agent flips its status dot", async ({ page }) => {
  await openAgents(page);
  const ava = page.locator(".fleet-row", { hasText: "ava" });
  // ava starts running; if a prior test already stopped it, the Start button is shown instead.
  const stop = ava.getByRole("button", { name: "Stop" });
  if (await stop.count()) {
    await stop.click();
    // Stopped agents drop the success dot and surface a Start button.
    await expect(ava.getByRole("button", { name: "Start" })).toBeVisible();
    await expect(ava.locator(".pl-dot--success")).toHaveCount(0);
  }
});

test("topbar switcher navigates to an agent by slug", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const trigger = page.getByTestId("fleet-switcher");
  await expect(trigger).toBeVisible(); // present because the mock fleet has agents
  await trigger.click();
  const roxy = page.getByRole("menuitem", { name: /roxy/ });
  await expect(roxy).toBeVisible();
  await roxy.click();
  // Slug routing (ADR 0042): picking an agent navigates to its own URL — each window is its
  // own agent. After the nav, the console is focused on roxy.
  await expect(page).toHaveURL(/\/app\/agent\/roxy\//);
  await expect(page.getByTestId("fleet-switcher")).toContainText("roxy");
});

test("host without delegates: add → 404 → Enable delegates → retried add succeeds (#797)", async ({ page }) => {
  // The focused agent (host) doesn't serve /api/delegates until the plugin is enabled;
  // enabling goes through the dedicated /api/plugins/{id}/enabled endpoint and the reload
  // hot-mounts the routes, so the retry lands without a restart.
  let enabled = false;
  let delegatePosts = 0;
  let enablePosts = 0;
  await page.route("**/api/fleet", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    for (const a of json.agents) if (!a.host) a.a2a = `http://127.0.0.1:${a.port}/a2a`;
    await route.fulfill({ json });
  });
  await page.route("**/api/delegates", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    delegatePosts += 1;
    if (!enabled) return route.fulfill({ status: 404, json: { detail: "Not Found" } });
    return route.fulfill({ json: { ok: true } });
  });
  await page.route("**/api/plugins/*/enabled", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    enablePosts += 1;
    enabled = true;
    return route.fulfill({ json: { ok: true, enabled: true, reloaded: true, restart_recommended: false } });
  });

  await openAgents(page);
  await page
    .locator(".fleet-row", { hasText: "ava" })
    .getByRole("button", { name: "Add as a delegate of this agent (delegate_to)" })
    .click();

  const error = page.locator(".pl-alert--error");
  await expect(error).toContainText("can't hold delegates");
  await page.getByTestId("enable-delegates").click();

  await expect.poll(() => enablePosts).toBe(1); // delegates enabled via the dedicated endpoint
  await expect.poll(() => delegatePosts).toBe(2); // the 404'd attempt + the post-enable retry
  await expect(error).toHaveCount(0); // retry succeeded -> error cleared
});

test("rename edits the display name; the id/slug stays", async ({ page }) => {
  await openAgents(page);
  const row = page.locator(".fleet-row", { hasText: "ava" });
  await row.getByRole("button", { name: /Rename/ }).click();
  const input = page.getByLabel("New agent name");
  await input.fill("nova");
  await input.press("Enter");

  const renamed = page.locator(".fleet-row", { hasText: "nova" });
  await expect(renamed).toBeVisible();
  // The slug (stable id) is untouched: switching to the renamed agent still
  // navigates to its original id URL.
  await closeOverlay(page);
  await page.getByTestId("fleet-switcher").click();
  await page.getByRole("menuitem", { name: /nova/ }).click();
  await expect(page).toHaveURL(/\/app\/agent\/ava\//);
});

test("discover → add to fleet → switch into the remote member (ADR 0042 §I)", async ({ page }) => {
  await openAgents(page);
  await page.getByRole("button", { name: /Discover agents/ }).click();
  const found = page.locator(".fleet-row", { hasText: "remy" });
  await expect(found).toBeVisible();

  await found.getByRole("button", { name: "Add to this fleet (a switchable remote member)" }).click();

  // Now a fleet member: remote tag + its URL, no start/stop controls.
  const member = page.locator(".fleet-row", { hasText: "http://192.168.5.50:7871" });
  await expect(member).toBeVisible();
  await expect(member.getByText("remote", { exact: true })).toBeVisible();
  await expect(member.getByRole("button", { name: "Stop" })).toHaveCount(0);

  // And switchable: the topbar switcher navigates to its slug window, where the hub
  // proxies the console (the mock strips /agents/<slug>/ — the app boots normally).
  await closeOverlay(page);
  await page.getByTestId("fleet-switcher").click();
  await page.getByRole("menuitem", { name: /remy/ }).click();
  await expect(page).toHaveURL(/\/app\/agent\/remy-re01\//);
  await expect(page.getByTestId("fleet-switcher")).toContainText("remy");

  // Unregister from the fleet manager (the remote agent itself is untouched). Fleet is a
  // host-console-only Box section now (2026-06 settings consolidation), so return to the host
  // console first — the member window we navigated into doesn't carry the Box group.
  await openAgents(page);
  await page.locator(".fleet-row", { hasText: "remy" })
    .getByRole("button", { name: /Remove from this fleet/ }).click();
  await expect(page.locator(".fleet-row", { hasText: "remy" })).toHaveCount(0);
});
