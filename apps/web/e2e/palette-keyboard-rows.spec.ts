import { expect, type Page, test } from "@playwright/test";

import { toolCardsSettled } from "./toolcard";

// The keyboard actions that ship as ⌘K rows (ADR 0061 × ADR 0063), exercised from the ONE
// state jsdom cannot see: a COLLAPSED dock.
//
// The DS AppShell renders each column conditionally (`{showLeft && <main …>}`), and the chat
// slot only ever renders inside a column's content — so collapsing takes the whole chat
// subtree, `[data-kb-scope="chat"]` and the composer included, out of the document. #613's
// "mounted for the app's lifetime" is a contract about the slot within its DOCK. That state
// is one ⌘K row away, because "Toggle left rail" is itself one of these rows, so an operator
// reaches it by the palette's own front door.
//
// Two rows depend on the column actually being back before they act, and both were silent
// no-ops from here: the DOM-walking one ran against the tree the navigation hadn't committed
// yet, and the composer one never asked for the navigation at all. The unit suite mounts a
// miniature of this shell (keybindingCommands.test.ts); these cases are the real thing.

/** Open ⌘K and pick the row with this exact label. Matched on the row's LABEL span rather
 *  than its accessible name — the DS renders the advertised combo as a trailing hint inside
 *  the same button, so the accessible name is "New chat ⌘T" and an exact-name match misses. */
async function runPaletteRow(page: Page, label: string): Promise<void> {
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await expect(page.locator(".pl-cmdk__panel")).toBeVisible();
  await page.locator(".pl-cmdk__panel .pl-cmdk-commands__input").fill(label);
  const row = page.locator(".pl-cmdk__panel .pl-cmdk-commands__label", {
    hasText: new RegExp(`^${label}$`),
  });
  await expect(row).toHaveCount(1);
  await row.click();
  await expect(page.locator(".pl-cmdk__panel")).toHaveCount(0);
}

const leftCol = (page: Page) => page.locator(".pl-appshell__col--left");

test("'Toggle latest tool block' toggles the block after re-opening a collapsed dock", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("FANOUT do two things");
  await composer.press("Enter");
  await toolCardsSettled(page);

  // A fan-out settles into one collapsed "2 tools" summary chip — the latest tool block.
  const head = page.locator(".pl-toolcard-summary__head");
  await expect(head).toHaveAttribute("aria-expanded", "false");

  // Baseline, with the dock open: the row works. (So a failure below is the collapse, not
  // the row.)
  await runPaletteRow(page, "Toggle latest tool block");
  await expect(head).toHaveAttribute("aria-expanded", "true");
  await runPaletteRow(page, "Toggle latest tool block");
  await expect(head).toHaveAttribute("aria-expanded", "false");

  // Collapse the dock through the palette row that ships for it, and confirm the chat subtree
  // is GONE rather than hidden — that is the premise the whole case rests on.
  await runPaletteRow(page, "Toggle left rail");
  await expect(leftCol(page)).toHaveCount(0);
  await expect(page.locator(".chat-session-slot")).toHaveCount(0);
  await expect(page.locator('[data-kb-scope="chat"]')).toHaveCount(0);

  // Now the row must do BOTH halves: bring the column back, and toggle the block on it.
  await runPaletteRow(page, "Toggle latest tool block");
  await expect(leftCol(page)).toHaveCount(1);
  await expect(head).toHaveAttribute("aria-expanded", "true");
});

test("'Focus chat composer' re-opens a collapsed dock and lands in the composer", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });

  await runPaletteRow(page, "Toggle left rail");
  await expect(leftCol(page)).toHaveCount(0);
  await expect(composer).toHaveCount(0); // the composer isn't hidden, it doesn't exist

  // `composer.focus` is a GLOBAL binding — nothing about its `scope` says "chat" — so the row
  // has to name the surface itself; its own body's `setSurface("chat")` picks the active
  // surface but never un-collapses the dock.
  await runPaletteRow(page, "Focus chat composer");
  await expect(leftCol(page)).toHaveCount(1);
  await expect(composer).toBeFocused();
});

test("a chat row invoked from ANOTHER surface navigates there first", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".app-shell-main")).toBeVisible();
  // Leave chat entirely (Knowledge takes over the left dock's active surface). The slot stays
  // MOUNTED — that is #613 — but the stage is display:none, so the composer isn't reachable.
  const stage = page.locator(".chat-stage");
  await page.locator(".pl-rail").getByRole("button", { name: "Knowledge", exact: true }).click();
  await expect(stage).not.toBeVisible();

  await runPaletteRow(page, "New chat");
  await expect(stage).toBeVisible();
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeFocused();
});
