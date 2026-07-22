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
  await expect(page.locator(".pl-radiocard")).toHaveCount(2); // DS RadioCard, from GET /api/archetypes (Custom filtered out)
  await page.locator(".pl-radiocard", { hasText: "Product Manager" }).click();
  await page.getByLabel("Agent name").fill("newbot");
  await page.getByRole("button", { name: /Create/ }).click();
  await expect(page.locator(".fleet-row", { hasText: "newbot" })).toBeVisible();
});

test("New agent → configure a bundle's MCP inputs → create seeds them (#2041)", async ({ page }) => {
  await openAgents(page);

  // Capture the create payload — the Configure step must carry the operator's inputs.
  let posted = null;
  await page.route("**/api/fleet", async (route) => {
    if (route.request().method() === "POST") posted = route.request().postDataJSON();
    return route.continue();
  });

  await page.getByRole("button", { name: "New agent" }).click();
  await page.locator(".pl-radiocard", { hasText: "Product Manager" }).click();

  // The picked bundle asks for a GitHub token (secret, masked) + declares a Brave secret;
  // both surface in the inline Configure step (the preview peek supplies them).
  const token = page.getByLabel("GitHub token");
  await expect(token).toBeVisible();
  await expect(page.getByLabel("Brave API key")).toBeVisible();
  await token.fill("ghp_secret");

  await page.getByLabel("Agent name").fill("ghbot");
  await page.getByRole("button", { name: /Create/ }).click();

  await expect(page.locator(".fleet-row", { hasText: "ghbot" })).toBeVisible();
  expect(posted?.inputs).toEqual({ github_token: "ghp_secret" });
  // The Brave secret was left blank → dropped (env-only fallback), not sent as an empty value.
  expect(posted?.secrets ?? []).toEqual([]);
});

test("New agent preview dialog lists the bundle's MCP servers + secrets (#2041)", async ({ page }) => {
  await openAgents(page);
  await page.getByRole("button", { name: "New agent" }).click();
  await page.locator(".pl-radiocard", { hasText: "Product Manager" }).click();
  await page.getByRole("button", { name: /See what.s included/ }).click();

  const dialog = page.locator(".pl-dialog", { hasText: "What's included" });
  await expect(dialog.getByText("MCP servers: GitHub (needs token)")).toBeVisible();
  await expect(dialog.getByText("Secrets: Brave API key")).toBeVisible();
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

// ── Command-palette fleet toggle (#1769) ───────────────────────────────────────────
// Toggle a local, non-host member on/off straight from ⌘K — no Settings dive. The
// picker lists togglable members with their live process state; picking one flips it and
// toasts. Reuses this file's per-spec fleet scope (beforeEach resets to baseline).

// The DS palette morphs views with a popLayout cross-fade, so the exiting root list lingers
// in the DOM for a beat next to the entering sub-view. A sub-view row's accessible name is
// "<name> <state>" (e.g. "ava on") — and the sub-view states ("on"/"off") never collide with
// the root quick-chat's hints ("switch"/"stopped"/"unreachable"), so keying every assertion
// on the exact "<name> <state>" name sidesteps the transient overlap entirely.
async function openToggleFleet(page) {
  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.locator(".pl-cmdk__panel")).toBeVisible();
  // A reopen momentarily re-morphs the last sub-view back to the root view (the DS palette
  // resets its stack on open), so the sub-view's search box lingers next to the root one for a
  // beat — wait for it to leave before filling, or `.pl-cmdk-commands__input` is ambiguous.
  await expect(page.getByPlaceholder("Toggle a fleet agent on/off…")).toHaveCount(0);
  // Filter the root list to the toggle command, then enter its submorph.
  await page.locator(".pl-cmdk__panel .pl-cmdk-commands__input").fill("Toggle Fleet Agent");
  await page.getByRole("option", { name: "Toggle Fleet Agent" }).click();
  await expect(page.locator(".pl-cmdk__title")).toHaveText("Toggle Fleet Agent"); // in the sub-view
}

const row = (page, name, state) => page.getByRole("option", { name: `${name} ${state}`, exact: true });

test("⌘K → Toggle Fleet Agent lists non-host members with state and flips one", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await openToggleFleet(page);

  // Local members are listed with their live process state; the host (main) never is.
  await expect(row(page, "ava", "on")).toBeVisible(); // running in baseline
  await expect(row(page, "roxy", "off")).toBeVisible(); // stopped, still listed
  await expect(page.getByRole("option", { name: /^main\b/ })).toHaveCount(0); // host excluded

  // Toggle ava off — the palette closes, a toast confirms, and the roster flips.
  await row(page, "ava", "on").click();
  await expect(page.locator(".pl-cmdk__panel")).toHaveCount(0);
  await expect(page.locator(".pl-toast", { hasText: "Stopping ava" })).toBeVisible();

  // Reopen the picker: ava now reads "off" (its running state flipped on the invalidated poll).
  await openToggleFleet(page);
  await expect(row(page, "ava", "off")).toBeVisible();
});

test("⌘K → Toggle Fleet Agent starts a stopped member", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await openToggleFleet(page);

  await expect(row(page, "roxy", "off")).toBeVisible(); // stopped in baseline
  await row(page, "roxy", "off").click();
  await expect(page.locator(".pl-toast", { hasText: "Starting roxy" })).toBeVisible();

  await openToggleFleet(page);
  await expect(row(page, "roxy", "on")).toBeVisible();
});

async function openFleetRoom(page) {
  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.locator(".pl-cmdk__panel")).toBeVisible();
  await page.locator(".pl-cmdk__panel .pl-cmdk-commands__input").fill("Fleet Room");
  await page.getByRole("option", { name: "Fleet Room" }).click();
  await expect(page.locator(".pl-cmdk__title")).toHaveText("Fleet"); // morphed into the room
}

test("⌘K → Fleet Room: presence, DM a member (the wired chat), broadcast", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await openFleetRoom(page);
  const room = page.locator(".flr");

  // Roster with presence: the host is tagged "this instance"; a running member and a
  // stopped one both appear, encoded in the dot class (success vs the stopped default).
  await expect(room.locator(".flr__member", { hasText: "main" }).locator(".flr__tag--host")).toBeVisible();
  await expect(room.locator(".flr__member", { hasText: "ava" }).locator(".flr__dot--online")).toBeVisible();
  await expect(room.locator(".flr__member", { hasText: "roxy" }).locator(".flr__dot--stopped")).toBeVisible();

  // DM a running member — clicking it morphs into the wired chat, pointed at that member
  // (placeholder names them). Back returns to the roster.
  await room.locator(".flr__member", { hasText: "ava" }).locator(".flr__who").click();
  await expect(page.getByPlaceholder(/Message ava/i)).toBeVisible();
  await page.locator(".pl-cmdk__back").click();
  await expect(room.locator(".flr__composer")).toBeVisible();

  // The bottom bar broadcasts to everyone online → a success toast.
  await room.locator(".flr__input").fill("standup in 5");
  await room.locator(".flr__send").click();
  await expect(page.locator(".pl-toast", { hasText: /Broadcast to \d+ member/ })).toBeVisible();
});

test("⌘K → Fleet Room shows the roster + live activity feed side by side", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await openFleetRoom(page);
  const room = page.locator(".flr");
  // Two columns inside the dialog: roster on the left, the activity feed on the right.
  await expect(room.locator(".flr__roster")).toBeVisible();
  await expect(room.locator(".flr__activity")).toBeVisible();
  await expect(room.getByText("Fleet activity", { exact: true })).toBeVisible();
  await expect(room.locator(".flr-feed__empty")).toBeVisible(); // no events until presence changes
});
