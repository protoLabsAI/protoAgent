import { expect, test, type Page } from "@playwright/test";

// ⌘K → every Settings section (#3291). The rows are GENERATED from settings/sections.ts, so
// what unit tests can pin is the row factory and the registry wiring — both stop at the
// emitted NavIntent. Everything past that (store → overlay → SettingsSurface) is only
// observable end to end, which is where the repeat-deep-link regression below lived.

const palette = (page: Page) => page.locator(".pl-cmdk__panel");
const rows = (page: Page) => palette(page).locator(".pl-cmdk-commands__item");
const searchBox = (page: Page) => palette(page).locator(".pl-cmdk-commands__input");
/** The section the open Settings dialog is actually showing (DS SideNav marks it selected). */
const activeSection = (page: Page) =>
  page.locator(".settings-overlay .pl-sidenav__item--active .pl-sidenav__label");

async function openPalette(page: Page) {
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await expect(palette(page)).toBeVisible();
}

/** Type `query`, then run the first row — the one Enter selects. */
async function runFirstMatch(page: Page, query: string) {
  await openPalette(page);
  await searchBox(page).fill(query);
  await expect(rows(page).first()).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(palette(page)).toHaveCount(0);
}

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("a section row opens Settings on that section — by the word an operator types", async ({ page }) => {
  // "dark mode" is a Theme KEYWORD, not its label: the whole point of SECTION_KEYWORDS.
  await runFirstMatch(page, "dark mode");
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await expect(activeSection(page)).toHaveText("Theme");
});

test("re-running the SAME deep-link re-lands it after the operator navigated away", async ({ page }) => {
  // THE REGRESSION (#3291 review). `openGlobalSettings` used to write only the overlay's
  // remount seed, so running the row for the section the dialog ALREADY held was a no-change
  // `set`: no remount, no `initialSection` change, no effect — the palette closed and the
  // operator sat on whatever pane they had wandered to. From the desktop Launcher (which
  // hides itself and focuses the console) there is no feedback at all that nothing moved.
  await runFirstMatch(page, "dark mode");
  await expect(activeSection(page)).toHaveText("Theme");

  // Wander: click another section in the open dialog's own rail.
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Model", exact: true }).click();
  await expect(activeSection(page)).toHaveText("Model");

  // …then run the very same row again. It must come back to Theme.
  await runFirstMatch(page, "dark mode");
  await expect(activeSection(page)).toHaveText("Theme");
});

test("a DIFFERENT section's row still lands (the control for the case above)", async ({ page }) => {
  await runFirstMatch(page, "rag"); // Knowledge
  await expect(activeSection(page)).toHaveText("Knowledge");
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Model", exact: true }).click();
  await expect(activeSection(page)).toHaveText("Model");
  await runFirstMatch(page, "backup"); // Snapshot
  await expect(activeSection(page)).toHaveText("Snapshot");
});

test("closing Settings and re-opening it plainly resumes the SELECTED section, not the deep-link", async ({ page }) => {
  // "rebind", not "shortcuts": #3292's `/help` row is labelled "…commands & shortcuts", so
  // that word is in its LABEL and lands it in WORD_PREFIX, which correctly outranks this
  // row's keyword-only META match — Enter ran /help and no dialog ever opened. The ranking
  // is right; the query was ambiguous. "rebind" is Keyboard's alone.
  await runFirstMatch(page, "rebind"); // Keyboard
  await expect(activeSection(page)).toHaveText("Keyboard");
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Behavior", exact: true }).click();
  // Wait for the selection to actually commit before closing: clicking the tab writes the
  // persisted section, but that store write drives an async re-render — Escaping before the
  // DOM reflects it (as its siblings above assert after every click) races the close against
  // the write and reopens on the stale deep-link section under CI load.
  await expect(activeSection(page)).toHaveText("Behavior");
  await page.keyboard.press("Escape");
  await expect(page.locator(".settings-overlay")).toHaveCount(0);

  // The utility-bar pill carries NO section. The stale deep-link seed must not re-seed it.
  await page.getByTestId("settings-widget").click();
  await expect(activeSection(page)).toHaveText("Behavior");
});

test("the generated rows cover the table, carry their nav heading, and honour the gates", async ({ page }) => {
  await openPalette(page);
  await searchBox(page).fill("settings:");
  const labels = await rows(page).locator(".pl-cmdk-commands__label").allInnerTexts();
  // Every visible section, in table order — the coverage this feature exists to guarantee.
  expect(labels).toEqual([
    "Settings: Identity",
    "Settings: Operator & access",
    "Settings: Devices",
    "Settings: Model",
    "Settings: Behavior",
    "Settings: Knowledge",
    "Settings: Tracing",
    // Present because the mock serves channel "dev", where `secrets-panel` is ON — the row
    // gate resolving TRUE, not just absent gates passing by default.
    "Settings: Secrets",
    "Settings: Plugins",
    "Settings: Snapshot",
    "Settings: Tools",
    "Settings: MCP",
    "Settings: Skills",
    "Settings: Subagents",
    "Settings: Delegates",
    "Settings: Overview",
    "Settings: Fleet",
    "Settings: Telemetry",
    "Settings: Theme",
    "Settings: Chat",
    "Settings: Keyboard",
    "Settings: Storage",
  ]);
  // Absent, and the absence is the assertion. Developer IS in the dialog's rail here
  // (settings.spec.ts pins that), and SETTINGS_PALETTE_EXCLUDED still drops its row, because
  // its visibility is a CHANNEL decision — neither of the two axes a row gate can express —
  // so an ungated row would list it to production operators.
  expect(labels).not.toContain("Settings: Developer");
  // The nav heading rides as the trailing hint, and is searchable.
  await searchBox(page).fill("capabilities");
  expect(await rows(page).locator(".pl-cmdk-commands__label").allInnerTexts()).toEqual([
    "Settings: Tools",
    "Settings: MCP",
    "Settings: Skills",
    "Settings: Subagents",
    "Settings: Delegates",
  ]);
});

test("a flag-gated section's row appears — and works — once the flag is on", async ({ page }) => {
  // The gates ride the row as DATA and are resolved per render, so a late `/api/flags` answer
  // (or a ?flag: override) reveals the row rather than it having been filtered out at module
  // load — the fail-closed trap settingsPalette.ts's header describes.
  // The mock serves `secrets-panel` ON (its real dev tier), so force the server's answer OFF
  // first: the row must be absent, and the override must be what brings it back.
  await page.route("**/api/flags", (route) =>
    route.fulfill({
      json: {
        channel: "dev",
        flags: [{ id: "secrets-panel", description: "", tier: "dev", owner: "kj", remove_by: "", enabled: false, source: "channel" }],
      },
    }),
  );
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  await openPalette(page);
  await searchBox(page).fill("settings: secrets");
  await expect(rows(page).filter({ hasText: "Settings: Secrets" })).toHaveCount(0);

  await page.goto("/app/?flag:secrets-panel=on", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  await runFirstMatch(page, "settings: secrets");
  await expect(activeSection(page)).toHaveText("Secrets");
});
