import { expect, test } from "@playwright/test";

import { requiresToolsNotice } from "../src/lib/archetypeConfig";
import { CONFIGURE_REQUIRED_COPY, HARD_GATE_HINT, HARD_GATE_HINT_COLLAPSED } from "../src/lib/pickerCopy";
import { ARCHETYPES } from "./fixtures.mjs";

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

test("New agent → archetype picker → create navigates into the new agent", async ({ page }) => {
  await openAgents(page);
  await page.getByRole("button", { name: "New agent" }).click();
  await expect(page.getByRole("heading", { name: "New agent" })).toBeVisible();
  await expect(page.locator(".pl-radiocard")).toHaveCount(2); // DS RadioCard, from GET /api/archetypes (Custom filtered out)
  await page.locator(".pl-radiocard", { hasText: "Product Manager" }).click();
  await page.getByLabel("Agent name").fill("newbot");
  await page.getByRole("button", { name: /Create/ }).click();
  // Create lands the operator IN the new agent's console — the id is the URL slug
  // (ADR 0042, the same navigation the FleetSwitcher uses) — because the next move is
  // configuring the agent just made, not re-reading the fleet list.
  await expect(page).toHaveURL(/\/app\/agent\/newbot-ab12\//);
  await expect(page.getByTestId("fleet-switcher")).toContainText("newbot");
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

  // Create navigates into the new agent (see the picker test above); reaching the slug
  // URL also proves the POST has been captured before the payload assertions below.
  await expect(page).toHaveURL(/\/app\/agent\/ghbot-ab12\//);
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

// ── The archetype picker's hard gate (#2977/#2979/#2984) ──────────────────────────
// A required bundle `config_inputs` answer has no env fallback — the server refuses the
// create — so the picker must not offer a Create that can only 400. The Project Manager
// fixture is the contract-carrying, advanced archetype with two such answers.

// Open the picker and pick the (advanced, collapsed) Project Manager card.
async function pickProjectManager(page) {
  await page.getByRole("button", { name: "New agent" }).click();
  await page.getByRole("button", { name: /^Advanced \(1\)/ }).click();
  await page.locator(".pl-radiocard", { hasText: "Project Manager" }).click();
  // The Configure step is open by default; its fields come from the preview peek.
  await expect(page.getByLabel("Repository path")).toBeVisible();
}

// The DS DropdownSelect trigger carries the field id (origin:key — escape the colon/dot).
const coderTrigger = (page) => page.locator('[id="config:project_board.coder"]');
const createButton = (page) => page.getByRole("button", { name: /^Create/ });
// The contract note, computed by the same helper the card renders with — the spec and
// the component can't drift apart on wording.
const PM = ARCHETYPES.find((a) => a.id === "project-manager");
const PM_CONTRACT_NOTICE = requiresToolsNotice(PM.label, PM.requires_tools);

test("picking the Project Manager archetype shows its capability contract under the card (#2979)", async ({ page }) => {
  await openAgents(page);
  await pickProjectManager(page);
  // The contract note names the tool the persona commits to — at choose-time, so a
  // contract break is a known trade-off rather than a post-boot banner.
  await expect(page.getByRole("note").filter({ hasText: PM_CONTRACT_NOTICE })).toBeVisible();
  // The toggle copy says the answers are required, not "optional — skip".
  await expect(page.getByRole("button", { name: /Configure Project Manager/ })).toContainText(CONFIGURE_REQUIRED_COPY);
});

test("Create stays disabled while a required bundle answer is blank; the hint says why (#2977)", async ({ page }) => {
  await openAgents(page);
  await pickProjectManager(page);
  await page.getByLabel("Agent name").fill("pmbot");
  // A valid name alone isn't enough: the two hard-required answers are blank.
  await expect(createButton(page)).toBeDisabled();
  await expect(page.getByText(HARD_GATE_HINT, { exact: true })).toBeVisible();
  // Required fields are starred; the optional string and the defaulted boolean are not.
  await expect(page.locator(".archetype-configure-fields label", { hasText: "Repository path *" })).toBeVisible();
  await expect(page.locator(".archetype-configure-fields label", { hasText: "Coding delegate *" })).toBeVisible();
  await expect(page.locator(".archetype-configure-fields label", { hasText: "Default branch" })).not.toContainText("*");
  await expect(page.locator(".archetype-configure-fields label", { hasText: "Auto-merge green PRs" })).not.toContainText("*");
  // Filling just ONE of the two keeps the gate shut.
  await page.getByLabel("Repository path").fill("/Users/me/dev/repo");
  await expect(createButton(page)).toBeDisabled();
});

test("the coding-delegate dropdown lists ONLY acp delegates (#2934)", async ({ page }) => {
  await openAgents(page);
  await pickProjectManager(page);
  // DropdownSelect (#274): open the trigger, then read the portaled menu items.
  await coderTrigger(page).click();
  await expect(page.getByRole("menuitemradio", { name: "coder", exact: true })).toBeVisible();
  // /api/delegates also serves an openai endpoint ("opus") and an a2a peer ("peer-pm") —
  // neither can take a build, so neither may be offered as the coder.
  await expect(page.getByRole("menuitemradio", { name: "opus", exact: true })).toHaveCount(0);
  await expect(page.getByRole("menuitemradio", { name: "peer-pm", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
});

test("Enter in the Name field does NOT submit while a required answer is blank (#2979)", async ({ page }) => {
  await openAgents(page);
  const posted = [];
  await page.route("**/api/fleet", async (route) => {
    if (route.request().method() === "POST") posted.push(route.request().postDataJSON());
    return route.continue();
  });
  await pickProjectManager(page);
  const name = page.getByLabel("Agent name");
  await name.fill("pmbot");
  await name.press("Enter");
  await expect(page.getByRole("heading", { name: "New agent" })).toBeVisible();
  await expect(createButton(page)).toBeDisabled();

  // Positive control, and the proof the gated press above has fully run: the console
  // keeps an event stream open so there is no network-idle to wait for — instead, fill
  // the answers and press Enter AGAIN. The keyboard path still submits when ungated, and
  // the one POST that lands carries the answers; a gated press that had fired would have
  // landed first (same page, same handler) and be sitting in `posted` ahead of it.
  await page.getByLabel("Repository path").fill("/Users/me/dev/repo");
  await coderTrigger(page).click();
  await page.getByRole("menuitemradio", { name: "coder", exact: true }).click();
  await expect(createButton(page)).toBeEnabled();
  await name.press("Enter");
  await expect(page).toHaveURL(/\/app\/agent\/pmbot-ab12\//);
  expect(posted).toHaveLength(1);
  expect(posted[0].config_inputs).toEqual({ "project_board.repo": "/Users/me/dev/repo", "project_board.coder": "coder" });
});

test("collapsing Configure with a required answer blank shows the collapsed-state hint (#2979)", async ({ page }) => {
  await openAgents(page);
  await pickProjectManager(page);
  await page.getByLabel("Agent name").fill("pmbot");
  const toggle = page.getByRole("button", { name: /Configure Project Manager/ });
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  // The fields are gone but the explanation is not — the hint moved OUT of the collapsible
  // block so a disabled Create never reads as a mystery.
  await expect(page.getByLabel("Repository path")).toHaveCount(0);
  await expect(page.getByText(HARD_GATE_HINT_COLLAPSED, { exact: true })).toBeVisible();
  await expect(createButton(page)).toBeDisabled();
});

test("filling both required answers enables Create; config_inputs ride the POST even after collapsing Configure (#2979)", async ({ page }) => {
  await openAgents(page);
  let posted = null;
  await page.route("**/api/fleet", async (route) => {
    if (route.request().method() === "POST") posted = route.request().postDataJSON();
    return route.continue();
  });
  await pickProjectManager(page);
  await page.getByLabel("Agent name").fill("pmbot");

  await page.getByLabel("Repository path").fill("/Users/me/dev/repo");
  await coderTrigger(page).click();
  await page.getByRole("menuitemradio", { name: "coder", exact: true }).click();
  await expect(createButton(page)).toBeEnabled();
  await expect(page.getByText(HARD_GATE_HINT, { exact: true })).toHaveCount(0);

  // The fill-then-collapse regression (QA panel on #2979): the answers were collected but
  // the mutation only sent config values while Configure was open → server 400.
  await page.getByRole("button", { name: /Configure Project Manager/ }).click();
  await expect(page.getByLabel("Repository path")).toHaveCount(0);
  await expect(createButton(page)).toBeEnabled();
  await createButton(page).click();

  // Create navigates into the new agent; reaching the slug URL proves the POST was captured.
  await expect(page).toHaveURL(/\/app\/agent\/pmbot-ab12\//);
  expect(posted?.config_inputs).toEqual({ "project_board.repo": "/Users/me/dev/repo", "project_board.coder": "coder" });
  // The contract rides along so the member's workspace.yaml records it (ADR 0100).
  expect(posted?.requires_tools).toEqual(["github_create_issue"]);
  expect(posted?.bundle).toBe("https://github.com/protoLabsAI/project-manager-archetype");
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

test("a fleet row's name links to that agent's own window (#2240)", async ({ page }) => {
  await openAgents(page);
  // A peer's name is the click-through — a real <a href> (so cmd/middle-click opens it in a
  // new window), pointing at the SLUG: the stable id, never the editable display name.
  const roxy = page.locator(".fleet-row", { hasText: "roxy" }).locator(".fleet-name-link");
  await expect(roxy).toHaveAttribute("href", /\/agent\/roxy\/$/);
  // The focused agent's own row stays plain text — a link there is just a reload.
  await expect(page.locator(".fleet-row.active .fleet-name-link")).toHaveCount(0);
  await roxy.click();
  await expect(page).toHaveURL(/\/app\/agent\/roxy\//);
});

test("a member that IS a delegate can be unlinked from its row (#2266)", async ({ page }) => {
  // Stub the slug-scoped registry so ava reads as an existing delegate, and capture the
  // removal. Stateful on purpose: after the DELETE the list comes back empty, and the row
  // flipping to the add button is the ONLY success feedback the panel gives (no toast).
  let delegates = [{ name: "ava", type: "a2a", url: "http://127.0.0.1:7890/a2a" }];
  let deleted = null;
  await page.route("**/api/delegates", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({ json: { delegates } });
  });
  await page.route("**/api/delegates/*", async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    deleted = decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop());
    delegates = [];
    return route.fulfill({ json: { ok: true, message: "Removed.", delegates } });
  });

  await openAgents(page);
  const ava = page.locator(".fleet-row", { hasText: "ava" });
  await expect(ava.getByText("delegate")).toBeVisible(); // the state badge
  await ava.getByRole("button", { name: "Remove as a delegate of this agent (delegate_to)" }).click();

  await expect.poll(() => deleted).toBe("ava"); // removal lands on the FOCUSED agent's registry
  await expect(ava.getByText("delegate")).toHaveCount(0);
  // ...and the add button is back, so the gesture is symmetric rather than one-way.
  await expect(ava.getByRole("button", { name: "Add as a delegate of this agent (delegate_to)" })).toBeVisible();
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
  // Address the two lists by their OWN selectors: a discovery result is `--found`, a member
  // is not. They share the row shape, so a bare `.fleet-row` matched both the instant the
  // add landed and strict-mode-flaked under load.
  const found = page.locator(".fleet-row--found", { hasText: "remy" });
  await expect(found).toBeVisible();

  await found.getByRole("button", { name: "Add to this fleet (a switchable remote member)" }).click();

  // …and it leaves the found list: an agent that's already a member must not keep offering
  // "Add to this fleet", which would 400. (The re-scan satisfies this too — the point of the
  // contract is the end state, not which of the two paths got there first.)
  await expect(page.locator(".fleet-row--found", { hasText: "remy" })).toHaveCount(0);

  // Now a fleet member: remote tag + its URL, no start/stop controls.
  const member = page.locator(".fleet-row:not(.fleet-row--found)", { hasText: "http://192.168.5.50:7871" });
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

// ── Folded-in fleet controls (#1733 quick-chat + #1769 toggle → the Fleet Room) ─────
// The per-member root commands and the `Toggle Fleet Agent ▸` submorph are gone: member
// names ride the Fleet Room command's keywords, and the roster row carries DM / open /
// start / stop. These tests pin the fold — the old flows keep working, one hop away.

test("⌘K root: member names surface the Fleet Room; the old per-member commands are gone", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".app-shell-main")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await expect(page.locator(".pl-cmdk__panel")).toBeVisible();
  const input = page.locator(".pl-cmdk__panel .pl-cmdk-commands__input");
  // The toggle submorph is folded away.
  await input.fill("Toggle Fleet Agent");
  await expect(page.getByRole("option", { name: "Toggle Fleet Agent" })).toHaveCount(0);
  // Typing a member's name routes to the room (roster keywords), not a per-member row.
  await input.fill("ava");
  await expect(page.getByRole("option", { name: "Fleet Room" })).toBeVisible();
  await expect(page.getByRole("option", { name: /^ava\b/ })).toHaveCount(0);
  await page.getByRole("option", { name: "Fleet Room" }).click();
  await expect(page.locator(".flr")).toBeVisible();
});

test("Fleet Room roster: stop a running member, start a stopped one (folded #1769)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await openFleetRoom(page);
  const room = page.locator(".flr");

  // Stop ava (running in baseline) straight from her roster row; the dot flips on the
  // invalidated poll. The host (main) never gets a toggle — it serves this console.
  await expect(room.locator(".flr__member", { hasText: "main" }).getByRole("button", { name: /^(Stop|Start) main$/ })).toHaveCount(0);
  await room.locator(".flr__member", { hasText: "ava" }).getByRole("button", { name: "Stop ava" }).click();
  await expect(page.locator(".pl-toast", { hasText: "Stopping ava" })).toBeVisible();
  await expect(room.locator(".flr__member", { hasText: "ava" }).locator(".flr__dot--stopped")).toBeVisible();

  // Start roxy (stopped in baseline).
  await room.locator(".flr__member", { hasText: "roxy" }).getByRole("button", { name: "Start roxy" }).click();
  await expect(page.locator(".pl-toast", { hasText: "Starting roxy" })).toBeVisible();
  await expect(room.locator(".flr__member", { hasText: "roxy" }).locator(".flr__dot--online")).toBeVisible();
});

test("Fleet Room: a parked member turn shows 'needs approval', then hands back (#2132)", async ({ page }, testInfo) => {
  // setExtraHTTPHeaders REPLACES the set — re-carry the fleet scope alongside the HITL gate.
  await page.setExtraHTTPHeaders({
    "x-e2e-fleet": `fleet-spec-${testInfo.parallelIndex}`,
    "x-e2e-hitl": "ava",
  });
  await page.goto("/app/", { waitUntil: "load" });
  await openFleetRoom(page);
  const room = page.locator(".flr");
  const ava = room.locator(".flr__member", { hasText: "ava" });

  // ava's stream emits turn.input_required → attention pill + the actionable feed row.
  await expect(ava.locator(".flr__pill--attn")).toBeVisible();
  await expect(room.locator(".flr-feed__event", { hasText: "needs your approval" }).first()).toBeVisible();

  // The answer lands (turn.resumed) — needs-approval hands back to a live "running" pill…
  await expect(ava.locator(".flr__pill--run")).toBeVisible();
  await expect(room.locator(".flr-feed__event", { hasText: "resumed — input received" }).first()).toBeVisible();

  // …and the terminal turn.usage clears it.
  await expect(ava.locator(".flr__pill--run")).toHaveCount(0, { timeout: 6000 });
  await expect(ava.locator(".flr__pill--attn")).toHaveCount(0);
});

async function openFleetRoom(page) {
  // Boot readiness is asynchronous; wait until the workspace has mounted its keybindings.
  await expect(page.locator(".app-shell-main")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+Shift+k");
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
  // The DM header names the member (DmTitle store) — not the old generic "Direct message".
  await expect(page.locator(".pl-cmdk__title")).toHaveText("@ava");
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
  // The feed streams each online member's event bus (/agents/<slug>/api/events) — the mock
  // pushes activity/inbox/goal frames, so a mapped event lands in the column.
  await expect(room.locator(".flr-feed__event").first()).toBeVisible({ timeout: 6000 });
});

test("⌘K → Fleet Room: @-address a member in the composer, then send opens its DM", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await openFleetRoom(page);
  const room = page.locator(".flr");
  // Typing "@" opens a member picker; picking sets the address chip.
  await room.locator(".flr__input").fill("@ava");
  await room.locator(".flr__mention", { hasText: "ava" }).click();
  await expect(room.locator(".flr__target")).toContainText("@ava");
  // Type a message and send → morphs into ava's DM (the wired chat), message pre-sent.
  await room.locator(".flr__input").fill("ship it");
  await room.locator(".flr__send").click();
  await expect(page.getByPlaceholder(/Message ava/i)).toBeVisible();
});

test("⌘K → Fleet Room: a TYPED @name addresses that member without using the picker", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await openFleetRoom(page);
  const room = page.locator(".flr");
  // Never touch the picker — just type "@ava <message>" and send, the way people actually
  // type. It must address ava (open its DM), NOT broadcast the literal text.
  await room.locator(".flr__input").fill("@ava ship it");
  await room.locator(".flr__send").click();
  await expect(page.getByPlaceholder(/Message ava/i)).toBeVisible();
  await expect(page.locator(".pl-toast", { hasText: /Broadcast to/ })).toHaveCount(0);
});

// ── Sister agents get the fleet surfaces too (#1708/#1999 revisited) ───────────────────
// The three affordances used to be host-console-only, on the theory that a member window
// would be managing a fleet-of-one and could only nest. That's false for a slug window:
// /api/fleet + /api/archetypes are HUB paths (lib/api.ts `isHubPath`), so a sister agent's
// console drives the SAME fleet the host does. These pin that it stays reachable there —
// and that the window can't act on the agent serving it.

// Assert by ACTING, not by reading an aria-disabled attribute: a disabled DS MenuItem is
// pointer-events:none, so a click that lands is proof the item is live — and it also proves
// the deep-link RESOLVES, which is the half that was actually broken (the Box group was
// dropped wholesale off the host, so "Fleet settings" fell back to some other section).
test("a sister agent's window: Fleet settings opens the fleet panel from the switcher", async ({ page }) => {
  await page.goto("/app/agent/ava/", { waitUntil: "load" });
  await page.getByTestId("fleet-switcher").click();
  await page.getByRole("menuitem", { name: "Fleet settings" }).click();
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
});

test("a sister agent's window: New agent opens the archetype picker from the switcher", async ({ page }) => {
  await page.goto("/app/agent/ava/", { waitUntil: "load" });
  await page.getByTestId("fleet-switcher").click();
  await page.getByRole("menuitem", { name: "New agent" }).click();
  await expect(page.getByRole("heading", { name: "New agent" })).toBeVisible();
});

test("a sister agent's window: the ⌘K Fleet Room opens on the hub's roster", async ({ page }) => {
  await page.goto("/app/agent/ava/", { waitUntil: "load" });
  await openFleetRoom(page);
  const room = page.locator(".flr");
  // The hub's real roster — its siblings are here, not an empty fleet-of-one.
  await expect(room.locator(".flr__member", { hasText: "main" })).toBeVisible();
  await expect(room.locator(".flr__member", { hasText: "roxy" })).toBeVisible();
  // But no Stop on its OWN row: that button would kill the agent serving this window.
  await expect(
    room.locator(".flr__member", { hasText: "ava" }).getByRole("button", { name: /^(Stop|Start) ava$/ }),
  ).toHaveCount(0);
  // A sibling still toggles normally.
  await expect(room.locator(".flr__member", { hasText: "roxy" }).getByRole("button", { name: "Start roxy" })).toBeVisible();
});

test("a sister agent's window: the fleet panel won't stop or remove the agent serving it", async ({ page }) => {
  await page.goto("/app/agent/ava/", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  await page.getByTestId("app-drawer").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Fleet", exact: true }).click();
  const self = page.locator(".fleet-row", { hasText: "ava" });
  await expect(self).toBeVisible();
  await expect(self.getByRole("button", { name: "Stop" })).toHaveCount(0);
  await expect(self.getByRole("button", { name: "Remove" })).toHaveCount(0);
  // A sibling keeps its controls — the guard is about SELF, not about being a member window.
  await expect(page.locator(".fleet-row", { hasText: "roxy" }).getByRole("button", { name: "Start" })).toBeVisible();
});

// ── Roster reordering (#3197) — the persisted-order API (PUT /api/fleet/order) + the
// accessible move-up/move-down controls (ADR 0042 hub control-plane) ─────────────────────
// The reorder gesture is presentation-only: it must submit the COMPLETE immutable-id order
// (never editable names), persist across a refetch, roll back a rejected reorder without
// dropping rows or breaking the non-reorder row actions, and stay off the discovery list
// (a found row is not a member). Drag-and-drop is intentionally NOT covered: the UI slice
// deferred it (no reusable DS sortable/DnD primitive — a component-gap request was filed
// instead of a bespoke control), so the move-up/move-down buttons are the whole reorder
// surface. If a native-DnD affordance ever ships, add its drag path here.

// The member rows only (never a `--found` discovery result), in DOM order — i.e. the roster's
// live display order. Baseline order from the FLEET fixture is: main (host), ava, roxy.
const memberNames = (page) => page.locator(".fleet-row:not(.fleet-row--found) .fleet-name");

test("reorder: the accessible move control submits the immutable-id order + the list updates (#3197)", async ({ page }) => {
  await openAgents(page);

  // Capture the reorder PUT, then let the mock persist + answer (route.continue).
  let ordered = null;
  await page.route("**/api/fleet/order", async (route) => {
    if (route.request().method() === "PUT") ordered = route.request().postDataJSON();
    return route.continue();
  });

  await expect(memberNames(page)).toHaveText([/main/, /ava/, /roxy/]);

  // Boundary controls are disabled: the first row can't move up, the last can't move down —
  // so a move can never submit a no-op / out-of-range order.
  await expect(
    page.locator(".fleet-row", { hasText: "main" }).getByRole("button", { name: "Move main up in the fleet order" }),
  ).toBeDisabled();
  await expect(
    page.locator(".fleet-row", { hasText: "roxy" }).getByRole("button", { name: "Move roxy down in the fleet order" }),
  ).toBeDisabled();

  // Drive the reorder through the KEYBOARD (focus + Enter), not a drag — the guaranteed
  // non-pointer / screen-reader path. Move roxy up one slot: ava ⇄ roxy.
  const moveRoxyUp = page
    .locator(".fleet-row", { hasText: "roxy" })
    .getByRole("button", { name: "Move roxy up in the fleet order" });
  await moveRoxyUp.focus();
  await page.keyboard.press("Enter");

  // The PUT carries EVERY member's IMMUTABLE id (host included), in the new order — a complete
  // permutation, not a partial slice.
  await expect.poll(() => ordered?.order).toEqual(["main", "roxy", "ava"]);
  // …and the visible roster reflects it (optimistic apply now, confirmed by the settle refetch).
  await expect(memberNames(page)).toHaveText([/main/, /roxy/, /ava/]);
});

test("reorder: the payload uses the immutable id even after the member is renamed (#3197)", async ({ page }) => {
  await openAgents(page);
  // Rename ava → nova: the display label moves, the id/slug ("ava") is unchanged (the reorder
  // payload must key on the STABLE id, since a member can rename itself and names aren't unique).
  const ava = page.locator(".fleet-row", { hasText: "ava" });
  await ava.getByRole("button", { name: /Rename/ }).click();
  const input = page.getByLabel("New agent name");
  await input.fill("nova");
  await input.press("Enter");
  await expect(page.locator(".fleet-row", { hasText: "nova" })).toBeVisible();

  let ordered = null;
  await page.route("**/api/fleet/order", async (route) => {
    if (route.request().method() === "PUT") ordered = route.request().postDataJSON();
    return route.continue();
  });

  // Move the renamed member down (nova ⇄ roxy). The control is addressed by the new LABEL,
  // but the request must carry the stable id "ava" — never "nova".
  await page
    .locator(".fleet-row", { hasText: "nova" })
    .getByRole("button", { name: "Move nova down in the fleet order" })
    .click();
  await expect.poll(() => ordered?.order).toEqual(["main", "roxy", "ava"]);
});

test("reorder: a refetched fleet renders in the saved order (#3197)", async ({ page }) => {
  await openAgents(page);
  await expect(memberNames(page)).toHaveText([/main/, /ava/, /roxy/]);

  // Move roxy up and let the mock persist the new order (no route interception). WAIT for the
  // PUT to land before reloading — a reload mid-flight would abort the write, so the mock has to
  // have persisted the reorder before the refetch below can observe it.
  const saved = page.waitForResponse(
    (r) => r.url().endsWith("/api/fleet/order") && r.request().method() === "PUT",
  );
  await page
    .locator(".fleet-row", { hasText: "roxy" })
    .getByRole("button", { name: "Move roxy up in the fleet order" })
    .click();
  await saved;
  await expect(memberNames(page)).toHaveText([/main/, /roxy/, /ava/]);

  // Reload the console + reopen the panel: the SAVED order comes back from the server (the mock
  // reordered this scope's roster), not the fixture's baseline.
  await openAgents(page);
  await expect(memberNames(page)).toHaveText([/main/, /roxy/, /ava/]);
});

test("reorder: a rejected reorder rolls back without losing rows or breaking row actions (#3197)", async ({ page }) => {
  await openAgents(page);
  await expect(memberNames(page)).toHaveText([/main/, /ava/, /roxy/]);

  // The reorder API rejects the write (the contract's 400 error envelope). The UI rolls the
  // optimistic move back to the pre-move snapshot and re-syncs with the server on settle.
  await page.route("**/api/fleet/order", async (route) => {
    if (route.request().method() !== "PUT") return route.fallback();
    return route.fulfill({ status: 400, json: { detail: "order must be a complete permutation of the current fleet member ids" } });
  });
  await page
    .locator(".fleet-row", { hasText: "roxy" })
    .getByRole("button", { name: "Move roxy up in the fleet order" })
    .click();

  // The failure surfaces as a toast, and the roster reconciles to the original order — every
  // row survives, in baseline order, with no duplicate and no vanished member.
  await expect(page.locator(".pl-toast", { hasText: /Couldn.t reorder the fleet/ })).toBeVisible();
  await expect(memberNames(page)).toHaveText([/main/, /ava/, /roxy/]);

  // The non-reorder row actions are uncorrupted: stopping a member after the failed reorder
  // still flips its status (a rejected reorder didn't leave the rows in a broken state).
  await page.unroute("**/api/fleet/order");
  const ava = page.locator(".fleet-row", { hasText: "ava" });
  await ava.getByRole("button", { name: "Stop" }).click();
  await expect(ava.getByRole("button", { name: "Start" })).toBeVisible();
  await expect(ava.locator(".pl-dot--success")).toHaveCount(0);
});

test("reorder: only actual members get move controls — discovery rows don't (#3197)", async ({ page }) => {
  await openAgents(page);
  // A member row carries the reorder group…
  await expect(
    page
      .locator(".fleet-row:not(.fleet-row--found)", { hasText: "ava" })
      .getByRole("button", { name: "Move ava down in the fleet order" }),
  ).toBeVisible();

  // …but a DISCOVERED sibling is a candidate, not a member, so its row gets no reorder controls —
  // you can't reorder something that isn't in the roster yet.
  await page.getByRole("button", { name: /Discover agents/ }).click();
  const found = page.locator(".fleet-row--found", { hasText: "remy" });
  await expect(found).toBeVisible();
  await expect(found.locator(".fleet-row-reorder")).toHaveCount(0);
  await expect(found.getByRole("button", { name: /Move .* in the fleet order/ })).toHaveCount(0);
});

test("reorder API: the mock enforces the complete-permutation contract, incl. the error (#3197)", async ({ page }, testInfo) => {
  // Assert the mock control-plane is faithful to the real supervisor.set_roster_order contract
  // (a hub route, so hit it directly with the fleet scope this spec's beforeEach reset). The
  // request context doesn't inherit setExtraHTTPHeaders, so carry the scope header explicitly.
  const headers = { "x-e2e-fleet": `fleet-spec-${testInfo.parallelIndex}` };
  const order = "/api/fleet/order";

  // Not a complete permutation of the current member ids → rejected 400 with a detail envelope,
  // WITHOUT touching the saved order (the same failure the FastAPI route raises).
  const incomplete = await page.request.put(order, { headers, data: { order: ["main"] } });
  expect(incomplete.status()).toBe(400);
  expect((await incomplete.json()).detail).toBeTruthy();
  // An unknown id (never a member) is rejected the same way.
  const unknown = await page.request.put(order, { headers, data: { order: ["main", "ava", "ghost"] } });
  expect(unknown.status()).toBe(400);
  // A duplicate id (not a valid permutation) is rejected too.
  const dupe = await page.request.put(order, { headers, data: { order: ["main", "ava", "ava"] } });
  expect(dupe.status()).toBe(400);

  // A COMPLETE permutation of the current member ids is accepted and echoed back verbatim.
  const ok = await page.request.put(order, { headers, data: { order: ["main", "roxy", "ava"] } });
  expect(ok.ok()).toBeTruthy();
  expect((await ok.json()).order).toEqual(["main", "roxy", "ava"]);
  // …and the saved order now reads back from GET /api/fleet in that order (ids preserved).
  const roster = await (await page.request.get("/api/fleet", { headers })).json();
  expect(roster.agents.map((a) => a.id)).toEqual(["main", "roxy", "ava"]);
});
