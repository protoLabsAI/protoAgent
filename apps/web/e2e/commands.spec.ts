import { expect, test } from "@playwright/test";

import { SLASH_COMMANDS } from "./fixtures.mjs";

// The chat composer fetches the server's registered slash commands
// (GET /api/chat/commands) and autocompletes them as you type "/name".

// Deterministic client-side commands (ADR 0057) surface FIRST, then the server skills.
// `/goal` is a client command that claims only `/goal new` (a guided goal form, ADR 0073) —
// everything else falls through to the SERVER `/goal`. The menu DEDUPS by token, so a command
// that's both a client command and a server skill (`/goal`, `/clear`) appears ONCE, client-first
// — the server duplicate is dropped.
const CLIENT_SLASH = ["/new", "/clear", "/export", "/btw", "/trajectory", "/prompt", "/perf", "/compact", "/effort", "/model", "/incognito", "/help", "/bypass", "/goal", "/watch"];
// The server rows the menu shows, with client-token duplicates deduped away.
const serverRows = () => SLASH_COMMANDS.map((c) => `/${c.name}`).filter((n) => !CLIENT_SLASH.includes(n));

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("slash menu opens and lists the client + server commands", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/");

  const menu = page.locator(".slash-menu");
  await expect(menu).toBeVisible();
  // Each command renders as a `.slash-name` row (the description can repeat the
  // name, so scope to the name span to avoid matching twice). Client commands first.
  const names = await menu.locator(".slash-name").allInnerTexts();
  expect(names).toEqual([...CLIENT_SLASH, ...serverRows()]);
  // Workflows are listed as slash commands too (ADR 0002).
  expect(names).toContain("/research-and-brief");
  // Deduped: exactly one `/goal` and one `/clear`, not the client+server pair.
  expect(names.filter((n) => n === "/goal")).toHaveLength(1);
  expect(names.filter((n) => n === "/clear")).toHaveLength(1);
});

test("the ?flag: override reveals a flag-gated command (/publish, ADR 0068)", async ({ page }) => {
  // The ?flag: query override is the shareable "try this build" layer — here it turns the
  // chat.publish flag ON over its default-off tier, so /publish must list. (/compact was
  // this test's previous subject; #2785 / ADR 0101 D5 made it generally available.)
  await page.goto("/app/?flag:chat.publish=on", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/");

  const menu = page.locator(".slash-menu");
  await expect(menu).toBeVisible();
  const names = await menu.locator(".slash-name").allInnerTexts();
  expect(names).toContain("/publish");
  expect(names.filter((n) => n === "/compact")).toHaveLength(1); // un-gated since #2785
});

test("a chat.publish=off command (#2179 P2, #2683) is absent by default and appears when forced on", async ({ page }) => {
  // Default channel: chat.publish is tier "off" (the hosted service, #2685, doesn't exist
  // yet), so /publish must NOT be in the base list already asserted above — this test only
  // needs to confirm the reveal path (a shareable ?flag: link) actually works.
  await page.goto("/app/?flag:chat.publish=on", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/");

  const menu = page.locator(".slash-menu");
  await expect(menu).toBeVisible();
  const names = await menu.locator(".slash-name").allInnerTexts();
  expect(names).toContain("/publish");
  // Registered right after /export in coreSlashCommands.ts — pin the position too, not
  // just presence, so a future reorder is caught.
  expect(names.indexOf("/publish")).toBe(names.indexOf("/export") + 1);
});

test("filtering narrows the menu and selecting completes the command", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/go");

  const menu = page.locator(".slash-menu");
  // "/go" matches a single `/goal` now — the client command and the server `/goal` are
  // deduped to one row (client wins). Bare `/goal` falls through, inserting `/goal ` to edit.
  await expect(menu.locator(".slash-item")).toHaveCount(1);
  await expect(menu.getByText("/goal", { exact: true }).first()).toBeVisible();

  // Enter completes the highlighted command into the composer.
  await composer.press("Enter");
  await expect(composer).toHaveValue("/goal ");
  // Completing closes the menu (a space follows the command).
  await expect(menu).toBeHidden();
});

test("clicking Send on a bare command closes the slash menu so the picker is mouse-usable (#2492)", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/effort");
  await expect(page.locator(".slash-menu")).toBeVisible();

  // The MOUSE path: clicking Send fires none of the textarea events the slash
  // popover refreshes on, so the stale menu used to stay mounted OVER the form
  // and intercept its clicks (".slash-title … intercepts pointer events").
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator(".slash-menu")).toBeHidden();

  const form = page.locator(".hitl-card", { hasText: "Reasoning effort" });
  await expect(form).toBeVisible();
  // Playwright fails a click whose hit target is covered — these clicks ARE the
  // regression assertion, not just setup.
  await form.locator(".hitl-card-option", { hasText: "low" }).click();
  await form.getByRole("button", { name: /submit/i }).click();
  await expect(form).toBeHidden();
});

// ⌘⇧K knows the chat's verbs too (#3292). The two halves have DIFFERENT semantics and the
// difference is the whole risk: a client command RUNS, a user-facing skill cannot be run at
// all (the server rewrites the message on the next SEND) so its row must draft and stop.
test("the palette lists the chat's slash commands, and a skill row DRAFTS rather than runs", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await page.keyboard.press("ControlOrMeta+Shift+k");
  const palette = page.locator(".pl-cmdk__panel");
  await expect(palette).toBeVisible();
  const input = palette.locator(".pl-cmdk-commands__input");

  // A client command, by the token the operator already types in the composer, and reading
  // `/token · what it does` — the description has to be ON the row, since the hint slot is
  // spent on the row's caveat.
  await input.fill("/clear");
  await expect(
    page.getByRole("option", { name: "/clear · Clear this chat's history" }),
  ).toBeVisible();

  // A user-facing skill: picking it must leave the operator in chat with the token typed and
  // the send still theirs — never a turn fired on their behalf.
  await input.fill("/triage");
  await page.getByRole("option", { name: "/triage" }).click();
  await expect(palette).toBeHidden();
  await expect(composer).toHaveValue("/triage ");

  // …and picking one must not EAT a message already in progress. A skill directive leads the
  // message, so the token goes in FRONT of the draft: the operator gets "/triage <what they
  // were writing>", which is what they meant, rather than watching it disappear.
  await composer.fill("the deploy is flapping");
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await expect(palette).toBeVisible();
  await input.fill("/triage");
  await page.getByRole("option", { name: "/triage" }).click();
  await expect(composer).toHaveValue("/triage the deploy is flapping");
});

// The chat's rows are STATIC, and that is a correctness property, not a performance one: the
// DS commands view keeps a read-time provider's PREVIOUS results on screen — appended to
// `filtered` unfiltered, and runnable by Enter — for the 120ms it debounces the new query.
// With the chat's ACTION rows on that path, retyping a query and hitting Enter as one motion
// ran the command from the query BEFORE. Statics are client-filtered synchronously instead,
// so a query that matches nothing lists nothing, immediately.
test("a query that matches nothing lists nothing — and Enter runs nothing", async ({ page }) => {
  const palette = page.locator(".pl-cmdk__panel");
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await expect(palette).toBeVisible();
  const input = palette.locator(".pl-cmdk-commands__input");

  // Settle on a query that DOES match a chat row — this is the row a stale provider would
  // have left listed and runnable.
  await input.fill("yolo");
  await expect(page.getByRole("option")).toHaveCount(1);

  // …then type something that matches nothing, anywhere, and press Enter immediately. No
  // waiting: the whole point is that there is no window in which the old row is still live.
  await input.fill("zzqqxx-nothing-matches-this");
  await expect(page.getByRole("option")).toHaveCount(0);
  await page.keyboard.press("Enter");
  // Nothing ran, so the palette is still open on the empty result.
  await expect(palette).toBeVisible();
  await expect(page.locator(".pl-cmdk-commands__empty")).toHaveText("No matches");
});

// Hiding the chat's dock is a one-click gesture, and the DS AppShell collapses a dock by
// UNMOUNTING it — taking the chat slot and its `slashDispatch` registration with it. Gating
// the rows on "is a dispatcher registered right now?" therefore emptied the entire Chat +
// Skills group out of ⌘⇧K in exactly the state the palette is most useful in.
test("the chat's rows survive collapsing the panel chat lives on — and still run", async ({ page }) => {
  await page.getByTestId("toggle-left").click();
  await expect(page.locator(".pl-appshell__col--left")).toHaveCount(0);

  await page.keyboard.press("ControlOrMeta+Shift+k");
  const palette = page.locator(".pl-cmdk__panel");
  await expect(palette).toBeVisible();
  const input = palette.locator(".pl-cmdk-commands__input");

  await input.fill("/clear");
  const clear = page.getByRole("option", { name: "/clear · Clear this chat's history" });
  await expect(clear).toBeVisible();
  await expect(clear).toBeEnabled(); // listed AND live — there is still a chat, it is just hidden

  // Running it brings the panel back (openView un-collapses the dock chat lives on) and then
  // dispatches into the remounted slot — here, `/clear`'s confirm.
  await clear.click();
  await expect(page.locator(".pl-appshell__col--left")).toBeVisible();
  await expect(page.getByRole("dialog").filter({ hasText: /Clear this conversation/i })).toBeVisible();
});

// A palette row must never arm a permission. `/bypass` turns off the approval gate on
// `run_command`, and dispatched bare it TOGGLES — so from a fuzzy search one Enter would flip
// a trust boundary in a direction the row never named. Its row drafts instead: the operator
// types the direction and sends it themselves, on the tab it applies to.
test("/bypass drafts into the composer instead of arming auto-approval", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await page.keyboard.press("ControlOrMeta+Shift+k");
  const palette = page.locator(".pl-cmdk__panel");
  await expect(palette).toBeVisible();
  await palette.locator(".pl-cmdk-commands__input").fill("yolo");

  // The row names the tab's CURRENT mode — the thing an operator opens the palette to find out.
  const bypass = page.getByRole("option", { name: /\/bypass · .* — now off/ });
  await expect(bypass).toBeVisible();
  await bypass.click();

  await expect(palette).toBeHidden();
  await expect(composer).toHaveValue("/bypass ");
  // Nothing was armed: no system note, and the composer's bypass chip stays away.
  await expect(page.getByText(/Bypass permissions ON/i)).toHaveCount(0);
});
