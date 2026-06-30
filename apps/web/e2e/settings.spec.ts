import { expect, test } from "@playwright/test";

// Settings IA (2026-06-18 consolidation): there is ONE settings surface — a DS Dialog
// (title "Settings") opened from the utility-bar Settings PILL (data-testid
// "settings-widget"), the header drawer's "Settings" item, or a ⌘K deep-link. The dialog's
// sidenav splits into two labeled groups — Agent (always) and Box (host console only). The
// old Global overlay + the old rail Settings surface both fold into this one dialog; there is
// no scope toggle and no "Configuration" section (host-scoped FIELDS edit inline in the Agent
// group with a "box default" badge). Each section renders GET /api/settings/schema groups and
// saves via POST /api/settings.

// Open the consolidated settings dialog from the utility-bar pill.
async function openSettings(page, url = "/app/") {
  await page.goto(url, { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
}

// Open the same dialog from the header drawer's "Settings" item (the former "Global settings").
async function openFromDrawer(page, item = "Settings") {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  const drawer = page.getByTestId("app-drawer");
  await expect(drawer).toBeVisible();
  await drawer.getByRole("button", { name: item, exact: true }).click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
}

// Click a section in the dialog's sidenav (the dialog is wide enough that the DS SideNav is
// NOT responsive-collapsed, so role="tab" works).
async function section(page, name, scope = ".settings-overlay .pl-sidenav") {
  await page.locator(scope).getByRole("tab", { name, exact: true }).click();
}

// Field groups are collapsed by default — open every group so the fields are visible.
async function expandAllGroups(page) {
  await expect(page.locator(".pl-accordion__trigger").first()).toBeVisible();
  const triggers = page.locator(".pl-accordion__trigger");
  for (let i = 0; i < (await triggers.count()); i++) {
    const t = triggers.nth(i);
    if ((await t.getAttribute("aria-expanded")) !== "true") await t.click();
  }
}

test("the settings dialog lists the domain groups (host, no scope toggle)", async ({ page }) => {
  await openSettings(page);
  // One consolidated surface — no Global/Workspace segmented toggle (ADR 0048: scope is a
  // per-field badge, not a nav axis).
  await expect(page.locator(".pl-tabs--segmented")).toHaveCount(0);
  // The e2e default (/app/, no /agent/<slug>/) is the host console, so the Agent + Capabilities
  // + host-only Box + This-console groups all render, by domain.
  const sidenav = page.locator(".settings-overlay .pl-sidenav");
  expect(await sidenav.locator("button").allTextContents()).toEqual([
    // Agent group
    "Identity",
    "Operator & access",
    "Model",
    "Behavior",
    "Knowledge",
    "Integrations",
    // Capabilities group (sharing knobs live on each manager's chip, not a separate panel)
    "Tools",
    "MCP",
    "Skills",
    "Subagents",
    "Delegates",
    // Box group (host console only)
    "Overview",
    "Fleet",
    "Telemetry",
    // This console group
    "Theme",
    "Chat",
    "Keyboard",
  ]);
  await section(page, "Behavior");
  await expect(page.locator(".pl-accordion__title").first()).toBeVisible();
  expect(await page.locator(".pl-accordion__title").allTextContents()).toEqual(["Compaction", "Runtime"]);
});

test("the host scope badge marks box defaults", async ({ page }) => {
  await openSettings(page);
  await expect(page.locator(".settings-overlay .settings-scope-badge")).toContainText("Host · box defaults");
});

test("opening from the header drawer's Settings item shows the same dialog + the Box sections", async ({ page }) => {
  await openFromDrawer(page);
  const sidenav = page.locator(".settings-overlay .pl-sidenav");
  // The Box group (host console) — Configuration is GONE (host fields are inline in the Agent group).
  await expect(sidenav.getByRole("tab", { name: "Overview", exact: true })).toBeVisible();
  await expect(sidenav.getByRole("tab", { name: "Configuration", exact: true })).toHaveCount(0);
  // Fleet section shows the agents panel; Telemetry renders the dashboard.
  await sidenav.getByRole("tab", { name: "Fleet", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
  await sidenav.getByRole("tab", { name: "Telemetry", exact: true }).click();
  await expect(page.getByTestId("telemetry-surface")).toBeVisible();
});

// The drawer no longer has a Telemetry shortcut (ADR 0048 §2.4 — one Settings door); Telemetry
// is reachable via the Box group in the dialog or a ⌘K deep-link.
test("the drawer has a single Settings door, no Telemetry shortcut", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  const drawer = page.getByTestId("app-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("button", { name: "Telemetry", exact: true })).toHaveCount(0);
});

test("Model shows the agent's Model + Routing fields", async ({ page }) => {
  await openSettings(page);
  await section(page, "Model");
  await expect(page.locator(".pl-accordion__title").first()).toBeVisible();
  expect(await page.locator(".pl-accordion__title").allTextContents()).toEqual(["Model", "Routing"]);
  await expandAllGroups(page);
  await expect(page.locator('.setting-row[data-key="routing.aux_model"] input')).toHaveValue("protolabs/fast");
  await expect(page.locator('.setting-row[data-key="model.api_key"] input')).toHaveAttribute("placeholder", /set/);
});

test("editing an Agent setting enables save and round-trips", async ({ page }) => {
  await openSettings(page);
  await section(page, "Model");
  await expandAllGroups(page);
  const save = page.getByRole("button", { name: /Save & apply/ });
  await expect(save).toBeDisabled();
  await page.locator('.setting-row[data-key="routing.aux_model"] input').fill("protolabs/turbo");
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.locator(".pl-toast", { hasText: "config saved" })).toBeVisible();
});

test("a restart-flagged Behavior field shows the restart banner", async ({ page }) => {
  await openSettings(page);
  await section(page, "Behavior");
  await expect(page.locator(".settings-banner")).toHaveCount(0);
  await expandAllGroups(page);
  await page.locator('.setting-row[data-key="runtime.autostart_on_boot"] .pl-switch').click();
  await expect(page.locator(".settings-banner")).toContainText("restart");
});

// On the HOST console the host-scoped fields ARE the box defaults — they carry a "box
// default" badge inline in the Model domain (editing these writes the host layer).
// model.name / routing.aux_model are host-scoped.
test("host-scoped fields show the 'box default' badge inline in Model", async ({ page }) => {
  await openSettings(page);
  await section(page, "Model");
  await expandAllGroups(page);
  await expect(page.locator('.setting-row[data-key="model.name"] .setting-inheritance')).toContainText("box default");
  await expect(page.locator('.setting-row[data-key="routing.aux_model"] .setting-inheritance')).toContainText(
    "box default",
  );
  // An agent-scoped field (no host layer) carries no inheritance badge on the host.
  await expect(page.locator('.setting-row[data-key="routing.fallback_models"] .setting-inheritance')).toHaveCount(0);
  // A host-scoped field the agent leaf ALSO sets (model.temperature, source=agent) is shadowed:
  // the host console warns instead of mislabelling it "box default", and offers reset (issue #1459).
  const temp = page.locator('.setting-row[data-key="model.temperature"]');
  await expect(temp.locator(".setting-inheritance")).toContainText("overridden by agent config");
  await expect(temp.getByRole("button", { name: /Reset to inherited/ })).toBeVisible();
});

// On the host these same host-scoped edits save to the host layer (ADR 0047): the mock echoes
// "config saved (host)". The host-scoped subset edits inline in the Model domain, writing the
// box-shared host layer.
test("a host-scoped edit on the host console saves to the host layer", async ({ page }) => {
  await openSettings(page);
  await section(page, "Model");
  await expandAllGroups(page);
  await page.locator('.setting-row[data-key="routing.aux_model"] input').fill("protolabs/host-fast");
  await page.getByRole("button", { name: /Save & apply/ }).click();
  await expect(page.locator(".pl-toast", { hasText: "(host)" })).toBeVisible();
});

// On a FLEET MEMBER console (/agent/<slug>/) the same fields show the ADR 0047 inheritance
// view instead — inherited-from / overridden-here badges + reset-to-inherited. There is no
// Box group on a member.
test("per-agent (fleet member) settings show ADR 0047 inheritance badges + reset", async ({ page }) => {
  await openSettings(page, "/app/agent/ava/");
  // No Box group on a fleet member.
  await expect(page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Fleet", exact: true })).toHaveCount(0);
  await section(page, "Model");
  await expandAllGroups(page);
  await expect(page.locator('.setting-row[data-key="model.name"] .setting-inheritance')).toContainText(
    "inherited from host",
  );
  await expect(page.locator('.setting-row[data-key="routing.aux_model"] .setting-inheritance')).toContainText(
    "inherited from default",
  );
  const temp = page.locator('.setting-row[data-key="model.temperature"]');
  await expect(temp.locator(".setting-inheritance")).toContainText("overridden here");
  await temp.getByRole("button", { name: /Reset to inherited/ }).click();
  await expect(page.locator(".pl-toast", { hasText: /inherited/i })).toBeVisible();
  await expect(page.locator('.setting-row[data-key="routing.fallback_models"] .setting-inheritance')).toHaveCount(0);
});

test("Identity: a name change saves via /api/settings, not /api/config (no operator clobber)", async ({ page }) => {
  // The Identity panel routes the name through the canonical settings cascade; SOUL (unchanged
  // here) is what goes via /api/config. The old code ALWAYS POSTed /api/config echoing a cached
  // operator, which could clobber a fresh Operator & access edit — assert that's gone: a name-only
  // save POSTs /api/settings with identity.name and never touches /api/config.
  await openSettings(page);
  await section(page, "Identity");
  const nameInput = page.getByTestId("identity-name");
  await expect(nameInput).toBeVisible();

  let configPosted = false;
  page.on("request", (r) => {
    if (r.method() === "POST" && new URL(r.url()).pathname.endsWith("/api/config")) configPosted = true;
  });

  await nameInput.fill("renamed-agent");
  const settingsReq = page.waitForRequest(
    (r) => r.method() === "POST" && new URL(r.url()).pathname.endsWith("/api/settings"),
  );
  await page.getByTestId("identity-save").click();
  const req = await settingsReq;
  expect(req.postDataJSON()?.updates?.["identity.name"]).toBe("renamed-agent");
  await expect(page.locator(".pl-toast", { hasText: /Identity saved/i })).toBeVisible();
  expect(configPosted).toBe(false);
});
