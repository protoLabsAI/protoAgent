import { expect, test } from "@playwright/test";

// Fleet manager + archetype picker (Settings → Agents, ADR 0042). Drives the live
// control-plane endpoints (mocked): list, create from an archetype, stop. The mock
// FLEET is shared module state, so run serially + assert by presence (not exact counts).

test.describe.configure({ mode: "serial" });

async function openAgents(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs").getByRole("tab", { name: "Agents", exact: true }).click();
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
  await expect(page.locator(".archetype-card")).toHaveCount(2); // from GET /api/archetypes
  await page.locator(".archetype-card", { hasText: "Project Manager" }).click();
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
    await expect(ava.locator(".fleet-dot.stopped")).toBeVisible();
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
  // enabling appends plugins.enabled via /api/config and the reload hot-mounts the routes,
  // so the retry lands without a restart.
  let enabled = false;
  let delegatePosts = 0;
  let configPosts = 0;
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
  await page.route("**/api/config", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    configPosts += 1;
    enabled = true;
    return route.fulfill({ json: { ok: true, messages: ["config saved", "reloaded"] } });
  });

  await openAgents(page);
  await page
    .locator(".fleet-row", { hasText: "ava" })
    .getByRole("button", { name: "Add as a delegate of this agent (delegate_to)" })
    .click();

  const error = page.locator(".fleet-error");
  await expect(error).toContainText("can't hold delegates");
  await page.getByTestId("enable-delegates").click();

  await expect.poll(() => configPosts).toBe(1); // plugins.enabled += delegates applied
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
  await page.getByTestId("fleet-switcher").click();
  await page.getByRole("menuitem", { name: /remy/ }).click();
  await expect(page).toHaveURL(/\/app\/agent\/remy-re01\//);
  await expect(page.getByTestId("fleet-switcher")).toContainText("remy");

  // Unregister from the fleet manager (the remote agent itself is untouched).
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".pl-tabs").getByRole("tab", { name: "Agents", exact: true }).click();
  await page.locator(".fleet-row", { hasText: "remy" })
    .getByRole("button", { name: /Remove from this fleet/ }).click();
  await expect(page.locator(".fleet-row", { hasText: "remy" })).toHaveCount(0);
});
