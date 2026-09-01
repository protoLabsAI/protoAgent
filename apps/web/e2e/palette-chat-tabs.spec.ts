import { expect, test, type Page } from "@playwright/test";

import { seedCurrentChat } from "./chat-helpers";

// The command palette (⌘⇧K) lists every OPEN CHAT TAB by title (#3290) — core's dogfood of
// ADR 0061's palette-command seam. The unit tests pin the rows and the run path; only a real
// palette can prove the chain between them: App's side-effect import registers the rows and
// subscribes to the chat store, the adapter maps them onto DS commands, and the DS renders
// them under their own group. Every link there is invisible to a unit test and silent when it
// breaks — a palette with no chats in it looks exactly like a palette.
//
// The last three specs are the ones that matter most, because the first shipped version of
// this feature passed the first spec and was still broken. Serving the rows through the
// seam's read-time SOURCE path arms a `CommandProvider`, which the palette debounces 120ms,
// and for that window the previous query's provider rows are still listed — provider rows are
// ordered but never re-filtered, deliberately, so a remote source's hits are not deleted. A
// chat title matches no other command, so during the debounce the ranked corpus is empty and
// the list is nothing BUT stale chat rows: ⌘K lists rows the query excludes, the selection
// resets onto one, and Enter runs the wrong chat — or, before any rows have arrived at all,
// falls on an empty list and does nothing. Note the shape of each: they act on the palette
// IMMEDIATELY (`press("Enter")`, read the rows in the same tick) and never `click()` a row,
// because Playwright's auto-wait for a row to exist is exactly the wait a real operator does
// not do — it is what let the original spec go green over all of it.
//
// One of the four is no longer discriminating and is kept anyway: "the highlighted row stays
// where the operator put it" was a DS `commandsView` defect (a `setSel(0)` on every row-count
// change), and #3289's host-owned root view fixed it at the root for every path — provider
// rows included, pinned in `src/app/palette/rootView.test.ts`. It stays here as the end-to-end
// half of that guarantee, because nothing else asserts it against a real palette.

const PANEL = ".pl-cmdk__panel";
const OPTION = `${PANEL} [role="option"]`;
const INPUT = `${PANEL} .pl-cmdk-commands__input`;

test("the palette switches to a chat by its title (#3290)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const tabs = page.locator(".pl-tabbar__tab");
  // The composer of the VISIBLE session slot (a 2nd tab mounts a 2nd slot/composer).
  const composer = () =>
    page.locator(".chat-session-slot:not([hidden])").getByPlaceholder(/Message protoAgent/i);

  // Tab 1 takes its title from its first message (that derivation is exactly why these rows
  // can't be a snapshot); tab 2 is the one the operator is sitting on.
  await seedCurrentChat(page, "release notes");
  await expect(tabs.nth(0)).toContainText("release notes");
  await composer().focus(); // ⌘T is chat-scoped
  await page.keyboard.press("ControlOrMeta+t");
  await expect(tabs).toHaveCount(2);
  await expect(tabs.nth(1)).toHaveClass(/pl-tabbar__tab--active/);

  await page.keyboard.press("ControlOrMeta+Shift+k");
  await expect(page.locator(PANEL)).toBeVisible();

  // The rows carry their own "Chats" header, asserted on the UNTYPED list because that is the
  // only list that has headers: the root view drops them once the list is ranked (#3289 — a
  // relevance order has no sections, so a contiguity header would re-emit every few rows).
  // The untyped list is also where the group has to EARN its place: it is capped at nine with
  // a per-group quota, so a "Chats" header here means the chats really are a group of the
  // root and not just something you can search for.
  await expect(page.locator(`${PANEL} .pl-cmdk-commands__group`)).toContainText(["Chats"]);

  await page.locator(INPUT).fill("release");
  // One row — this is the assertion that the rows really reached the palette; an unregistered
  // or frozen list shows "No matches" here.
  const row = page.locator(OPTION).filter({ hasText: "release notes" });
  await expect(row).toHaveCount(1);
  await row.click();

  // The palette closes and the strip is on the chat we named — reached by NAME, from a tab
  // the ⌘1–9 ordinals would have made you count to.
  await expect(page.locator(PANEL)).toHaveCount(0);
  await expect(tabs.nth(0)).toHaveClass(/pl-tabbar__tab--active/);
});

/** Two chats — "release notes" on tab 1, "smoke rehearsal" on tab 2 — with the operator
 *  sitting on tab 1, so a switch to tab 2 is a move you can see. Neither title shares a word
 *  with a command (a chat called "fleet …" is legitimately outranked by **Fleet Room**), so
 *  the only thing a query here can match is the chat it names. */
async function twoChats(page: Page) {
  await page.goto("/app/", { waitUntil: "load" });
  const tabs = page.locator(".pl-tabbar__tab");
  const composer = () =>
    page.locator(".chat-session-slot:not([hidden])").getByPlaceholder(/Message protoAgent/i);
  await seedCurrentChat(page, "release notes");
  await expect(tabs.nth(0)).toContainText("release notes");
  await composer().focus();
  await page.keyboard.press("ControlOrMeta+t");
  await expect(tabs).toHaveCount(2);
  const second = composer();
  await second.fill("smoke rehearsal");
  await second.press("Enter");
  await expect(tabs.nth(1)).toContainText("smoke rehearsal");
  await tabs.nth(0).click();
  await expect(tabs.nth(0)).toHaveClass(/pl-tabbar__tab--active/);
  return tabs;
}

async function openPalette(page: Page) {
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await expect(page.locator(PANEL)).toBeVisible();
}

test("Enter runs the chat you typed, with no wait between the two (#3290)", async ({ page }) => {
  const tabs = await twoChats(page);
  await openPalette(page);
  // Let the empty-query list settle FIRST, so a row left over from it is visibly the previous
  // query's row and not a list that simply hasn't arrived yet. "release notes" is the chat the
  // capped root list holds (one row per group, and it is the current tab); "smoke rehearsal"
  // is NOT on it. So typing "smoke" next is the sharpest form of the question: if anything
  // from before the keystroke survives, the only row on screen is "release notes", Enter lands
  // there, and this fails.
  await expect(page.locator(OPTION).filter({ hasText: "release notes" })).toBeVisible();
  const input = page.locator(INPUT);
  await input.fill("smoke");
  await input.press("Enter"); // the gesture: type a name, hit Enter, don't pause

  await expect(page.locator(PANEL)).toHaveCount(0);
  await expect(tabs.nth(1)).toHaveClass(/pl-tabbar__tab--active/);
  await expect(tabs.nth(0)).not.toHaveClass(/pl-tabbar__tab--active/);
});

test("Enter is never swallowed, even on the first keystroke (#3290)", async ({ page }) => {
  const tabs = await twoChats(page);
  await openPalette(page);
  // No settle at all this time: type and commit before anything the palette might be waiting
  // on could arrive. A chat title matches no other command, so if the rows are not simply
  // THERE the keypress falls on an empty list and the operator gets nothing — palette still
  // open, tab strip unmoved, feature looks broken.
  const input = page.locator(INPUT);
  await input.fill("smoke");
  await input.press("Enter");

  await expect(page.locator(PANEL)).toHaveCount(0);
  await expect(tabs.nth(1)).toHaveClass(/pl-tabbar__tab--active/);
});

test("no row survives a query that excludes it (#3290)", async ({ page }) => {
  await twoChats(page);
  await openPalette(page);
  // Settle the empty list, then type a query that EXCLUDES rows it was holding — the capped
  // root list carries agents, plugin views and commands as well as the chat row.
  await expect(page.locator(OPTION).filter({ hasText: "release notes" })).toBeVisible();
  await page.locator(INPUT).fill("release");
  // Read in the same tick as the keystroke — no auto-wait, no retry. Every row on screen has
  // to match what was typed, including the row Enter is aimed at.
  const rows = await page.locator(OPTION).allInnerTexts();
  expect(rows.length).toBeGreaterThan(0);
  for (const row of rows) expect(row.toLowerCase()).toContain("release");
  const selected = await page.locator(`${PANEL} [data-sel="true"]`).innerText();
  expect(selected.toLowerCase()).toContain("release");
});

// Selection stability is the ROOT VIEW's now (#3289), not this feature's — see the note at
// the top. This is the end-to-end check that it holds in a real palette with live chat rows in
// it, which no unit test covers.
test("the highlighted row stays where the operator put it (#3290)", async ({ page }) => {
  await twoChats(page);
  await openPalette(page);
  const input = page.locator(INPUT);
  await input.press("ArrowDown");
  await input.press("ArrowDown");
  const selected = page.locator(`${PANEL} [data-sel="true"]`);
  const chosen = await selected.innerText();

  // Nothing may move the selection but the operator: a list that grows under the cursor
  // re-points Enter at a command they never highlighted.
  await page.waitForTimeout(600);
  expect(await selected.innerText()).toBe(chosen);
});
