import { expect, test } from "@playwright/test";

import { seedCurrentChat } from "./chat-helpers";

// The command palette (⌘⇧K) lists every OPEN CHAT TAB by title (#3290) — core's dogfood of
// ADR 0061's dynamic palette source. The unit tests pin the rows and the run path; only a
// real palette can prove the chain between them: App's side-effect import registers the
// source, the adapter wires the DS provider BECAUSE a source exists, and the DS renders that
// provider's rows (after its 120ms debounce) under their own group. Every link there is
// invisible to a unit test and silent when it breaks — a palette with no chats in it looks
// exactly like a palette.

const PANEL = ".pl-cmdk__panel";

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
  await page.locator(`${PANEL} .pl-cmdk-commands__input`).fill("release");

  // One row, under its own "Chats" header — this is the assertion that the provider path is
  // actually wired; a frozen or unwired source shows the palette's "No matches" here.
  const row = page.locator(`${PANEL} [role="option"]`).filter({ hasText: "release notes" });
  await expect(row).toHaveCount(1);
  await expect(page.locator(`${PANEL} .pl-cmdk-commands__group`)).toHaveText("Chats");
  await row.click();

  // The palette closes and the strip is on the chat we named — reached by NAME, from a tab
  // the ⌘1–9 ordinals would have made you count to.
  await expect(page.locator(PANEL)).toHaveCount(0);
  await expect(tabs.nth(0)).toHaveClass(/pl-tabbar__tab--active/);
});
