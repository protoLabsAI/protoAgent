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
    // "Devices" is NOT here — gated behind the `settings.devices` developer flag, default
    // OFF (ADR 0068). Its absence from this list IS the assertion that the gate holds.
    "Model",
    "Behavior",
    "Knowledge",
    // Langfuse tracing (#3017) — the AGENT group, not Box, because its fields are agent-scoped
    // credentials and the Box group is host-console-only (see the sister-agent test below).
    "Tracing",
    "Secrets", // ADR 0080 — external secrets manager
    "Plugins",
    // Last in the Agent group: it EXPORTS what every section above configures (ADR 0091).
    "Snapshot",
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
    "Developer", // ADR 0068 — shown off prod (the mock serves channel "dev")
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
  expect(await page.locator(".pl-accordion__title").allTextContents()).toEqual(["Model", "Favorite models", "Routing"]);
  await expandAllGroups(page);
  await expect(page.locator('.setting-row[data-key="routing.aux_model"] input')).toHaveValue("protolabs/fast");
  // The endpoint/key/provider triple is no longer part of this form (ADR 0106) —
  // Settings ▸ Model ▸ Connections owns it, and two editors for one value is the
  // contradiction that panel resolved. Secret-field rendering is covered by
  // secrets-settings.spec.ts, which exercises a secret this form still owns.
  for (const retired of ["model.api_key", "model.api_base", "model.provider"]) {
    await expect(page.locator(`.setting-row[data-key="${retired}"]`)).toHaveCount(0);
  }
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
// view instead — inherited-from / overridden-here badges + reset-to-inherited. The Box group
// narrows to Fleet there (see the next test).
test("per-agent (fleet member) settings show ADR 0047 inheritance badges + reset", async ({ page }) => {
  await openSettings(page, "/app/agent/ava/");
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

// The Box group NARROWS on a sister agent's console, it doesn't vanish: Fleet stays (it names
// the hub's fleet from any window — /api/fleet is a hub path), while Overview + Telemetry read
// the focused agent's own endpoints and remain host-console-only.
test("a sister agent's console keeps Box ▸ Fleet, without the agent-scoped Box sections", async ({ page }) => {
  await openSettings(page, "/app/agent/ava/");
  const sidenav = page.locator(".settings-overlay .pl-sidenav");
  await expect(sidenav.getByRole("tab", { name: "Fleet", exact: true })).toBeVisible();
  await expect(sidenav.getByRole("tab", { name: "Overview", exact: true })).toHaveCount(0);
  await expect(sidenav.getByRole("tab", { name: "Telemetry", exact: true })).toHaveCount(0);
  // And it's the real panel — the hub's roster, reachable from here.
  await sidenav.getByRole("tab", { name: "Fleet", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
  await expect(page.locator(".fleet-row", { hasText: "roxy" })).toBeVisible();
});

// #3017 — the acceptance the issue is actually about. A fleet member started by the desktop
// app runs `--ui none`, so it serves no /app of its own: the ONLY console that sees it is this
// slug-scoped member window, and every `hostOnly` section (Box ▸ Telemetry included) is dropped
// here. Tracing therefore lives in the Agent group, and this is the DOM proof that a member's
// Langfuse credentials can be set from a console at all — the thing that was impossible before.
test("a fleet member's console can turn tracing on (Settings ▸ Tracing, #3017)", async ({ page }) => {
  await openSettings(page, "/app/agent/ava/");
  const sidenav = page.locator(".settings-overlay .pl-sidenav");
  await expect(sidenav.getByRole("tab", { name: "Telemetry", exact: true })).toHaveCount(0);

  await sidenav.getByRole("tab", { name: "Tracing", exact: true }).click();
  await expandAllGroups(page);

  // The toggle renders; the host + key pair stay folded until it's on (depends_on, #963).
  const toggle = page.locator('.setting-row[data-key="tracing.enabled"]');
  await expect(toggle).toBeVisible();
  await expect(page.locator('.setting-row[data-key="tracing.host"]')).toHaveCount(0);
  await toggle.locator(".pl-switch").click();
  await expect(page.locator('.setting-row[data-key="tracing.host"]')).toBeVisible();
  await expect(page.locator('.setting-row[data-key="tracing.public_key"] input')).toBeVisible();
  await expect(page.locator('.setting-row[data-key="tracing.secret_key"] input')).toBeVisible();
  // Built once at boot — the restart banner has to say so before the operator walks away.
  await expect(page.locator(".settings-banner")).toContainText("restart");

  // And the save lands on the MEMBER, through the hub's per-agent proxy (ADR 0042 slug
  // routing): agent-scoped fields write the agent leaf, which is this member's own config.
  const req = page.waitForRequest(
    (r) => r.method() === "POST" && new URL(r.url()).pathname.endsWith("/api/settings"),
  );
  await page.locator('.setting-row[data-key="tracing.host"] input').fill("https://cloud.langfuse.com");
  await page.getByRole("button", { name: /Save & apply/ }).click();
  const posted = await req;
  expect(new URL(posted.url()).pathname).toContain("/agents/ava/");
  expect(posted.postDataJSON()?.updates?.["tracing.host"]).toBe("https://cloud.langfuse.com");
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

test("Identity: a REFUSED save reports the failure instead of 'Identity saved'", async ({ page }) => {
  // /api/settings answers a refused write with 200 + {ok:false, messages} — the server rolls the
  // YAML back so disk and the running agent stay on the old identity. This panel used to ignore
  // `ok` and toast success either way, so a rolled-back rename looked like a console bug: the
  // name reverted on the next render and nothing said why. Every other settings panel checks
  // `ok`; assert this one does, and that it surfaces the server's reason.
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: false, messages: ["graph rebuild failed: boom", "rolled back"], restart_required: [] }),
    });
  });

  await openSettings(page);
  await section(page, "Identity");
  await page.getByTestId("identity-name").fill("doomed-agent");
  await page.getByTestId("identity-save").click();

  await expect(page.locator(".pl-toast", { hasText: /Save failed/i })).toBeVisible();
  await expect(page.locator(".pl-toast", { hasText: /graph rebuild failed: boom/i })).toBeVisible();
  await expect(page.locator(".pl-toast", { hasText: /Identity saved/i })).toHaveCount(0);
});
