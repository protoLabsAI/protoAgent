import { expect, test, type Page } from "@playwright/test";

import { seedCurrentChat } from "./chat-helpers";

// The chat TabBar is `responsive` (ChatSurface): wide it's the full strip (a tab per
// session), and when its container falls below 26rem it collapses — as of ui@0.68.2
// (protoContent#609) — to a quiet ghost trigger that opens a themed DS Menu with a radio
// row per session, NOT the earlier native <select>. These specs drive that collapsed
// switcher: switching sessions, the per-row status dots, and that the wide strip is
// unchanged. The `.pl-tabbar__tab` / `.pl-tabbar__add:visible` selectors the other chat
// specs lean on keep targeting the wide strip, which still renders exactly as before.

// Force the `responsive` container below its 26rem collapse threshold by constraining the
// TabBar's own query container (`.chat-tabbar-wrap` is `display:contents`, so the wrap that
// carries `container-type: inline-size` is `.pl-tabbar-wrap--responsive`). This narrows the
// chat panel's switcher region above the 767px mobile breakpoint, so the strip collapses
// without tripping the phone shell (which would hide the TabBar entirely for SessionSheet).
async function collapseTabBar(page: Page) {
  await page.addStyleTag({
    content: ".pl-tabbar-wrap--responsive{width:320px !important;max-width:320px !important}",
  });
  await expect(page.locator(".pl-tabbar__switcher")).toBeVisible();
}

// Two chats with distinct, searchable titles — "alpha session" on tab 1, "beta session" on
// tab 2 — titles derived from each tab's first message (same pattern as palette-chat-tabs).
// The operator is left on tab 2 (beta), so a switch to alpha is a visible move.
async function twoChats(page: Page): Promise<void> {
  await page.goto("/app/", { waitUntil: "load" });
  const tabs = page.locator(".pl-tabbar__tab");
  await seedCurrentChat(page, "alpha session");
  await expect(tabs.nth(0)).toContainText("alpha session");

  // tab 1 is now non-pristine, so "+" opens a genuinely new tab (not a reused blank).
  await page.locator(".pl-tabbar__add:visible").click();
  await expect(tabs).toHaveCount(2);
  const second = page.locator(".chat-session-slot:not([hidden])").getByPlaceholder(/Message protoAgent/i);
  await second.fill("beta session");
  await second.press("Enter");
  await expect(tabs.nth(1)).toContainText("beta session");
  await expect(tabs.nth(1)).toHaveClass(/pl-tabbar__tab--active/);
}

test("collapsed: the Menu switcher (no <select>) switches the active session", async ({ page }) => {
  await twoChats(page);
  await collapseTabBar(page);

  // The collapse is a DS Menu, not the old native control: no <select> under any pl-tabbar* node.
  await expect(page.locator('[class*="pl-tabbar"] select')).toHaveCount(0);
  await expect(page.locator(".pl-tabbar__select")).toHaveCount(0);

  // The ghost trigger shows the active chat's title (operator is on "beta session").
  const switcherLabel = page.locator(".pl-tabbar__switcher-label");
  await expect(switcherLabel).toHaveText("beta session");

  // Open the menu and pick the other session via its radio row.
  await page.locator(".pl-tabbar__switcher").click();
  const rows = page.getByRole("menuitemradio");
  await expect(rows).toHaveCount(2);
  await page.getByRole("menuitemradio", { name: "alpha session" }).click();

  // That session is now active: the trigger re-labels, and the (hidden) strip's active tab agrees.
  await expect(switcherLabel).toHaveText("alpha session");
  await expect(page.locator(".pl-tabbar__tab--active .pl-tabbar__label")).toHaveText("alpha session");
});

test("collapsed: every switcher row shows its session status dot", async ({ page }) => {
  await twoChats(page);
  await collapseTabBar(page);

  // The collapsed trigger itself carries the active session's dot in its icon slot — a
  // regression that dropped the dot from the trigger (not the rows) would otherwise slip
  // past the row assertions below (CodeRabbit PRRT_kwDOSFdkr86tk2If).
  await expect(page.locator(".pl-tabbar__switcher .session-dot")).toBeVisible();

  await page.locator(".pl-tabbar__switcher").click();
  const rows = page.getByRole("menuitemradio");
  await expect(rows).toHaveCount(2);
  // Each row carries its status dot in the DS icon slot (the old <option> could host none).
  await expect(page.locator('[role="menuitemradio"] .session-dot')).toHaveCount(2);
  await expect(rows.nth(0).locator(".session-dot")).toBeVisible();
  await expect(rows.nth(1).locator(".session-dot")).toBeVisible();
});

test("collapsed: keyboard selection and Escape return focus to the accessible trigger", async ({ page }) => {
  await twoChats(page);
  await collapseTabBar(page);

  const switcher = page.locator(".pl-tabbar__switcher");
  const menu = page.getByRole("menu");
  const alpha = page.getByRole("menuitemradio", { name: "alpha session" });
  const beta = page.getByRole("menuitemradio", { name: "beta session" });
  await expect(switcher).toHaveAccessibleName(/\S/);
  await expect(switcher).toHaveAttribute("aria-haspopup", "menu");
  await expect(switcher).toHaveAttribute("aria-expanded", "false");

  await switcher.focus();
  await switcher.press("Enter");
  await expect(menu).toBeVisible();
  await expect(switcher).toHaveAttribute("aria-expanded", "true");
  await expect(alpha).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(beta).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await expect(alpha).toBeFocused();
  await page.keyboard.press("Enter");

  // The selection took: that session is active and the menu closed.
  await expect(page.locator(".pl-tabbar__switcher-label")).toHaveText("alpha session");
  await expect(page.locator(".pl-tabbar__tab--active .pl-tabbar__label")).toHaveText("alpha session");
  await expect(menu).toBeHidden();
  await expect(switcher).toHaveAttribute("aria-expanded", "false");
  await expect(switcher).toBeFocused();

  // The pure #620 focus-return check: open and Escape with NO session change, so nothing else
  // claims focus and the Menu alone is responsible for returning it to the trigger.
  await switcher.focus();
  await page.keyboard.press("ArrowDown");
  await expect(menu).toBeVisible();
  await expect(switcher).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
  await expect(switcher).toHaveAttribute("aria-expanded", "false");
  await expect(switcher).toBeFocused();
});

test("collapsed: the visible add button creates and activates a third session", async ({ page }) => {
  await twoChats(page);
  await collapseTabBar(page);

  const add = page.locator(".pl-tabbar__add:visible");
  await expect(add).toBeVisible();
  await add.click();
  await expect(page.locator(".pl-tabbar__switcher-label")).toHaveText("New chat");
  await expect(page.locator(".pl-tabbar__tab--active .pl-tabbar__label")).toHaveText("New chat");

  await page.locator(".pl-tabbar__switcher").click();
  await expect(page.getByRole("menuitemradio")).toHaveCount(3);
  await expect(page.getByRole("menuitemradio", { name: "New chat", exact: true })).toHaveAttribute("aria-checked", "true");
});

test("collapsed: the Manage menu closes a non-active session (row count drops)", async ({ page }) => {
  await twoChats(page);
  await collapseTabBar(page);

  // Operator is on "beta session". The collapse exposes a "Manage" section OUTSIDE the radio
  // group: one per-session submenu (role="menuitem") per chat, each with Rename…/Close — an
  // affordance the old native <select> collapse could not offer. Close the OTHER (non-active)
  // session without first switching to it (CodeRabbit PRRT_kwDOSFdkr86tk2Id).
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);
  await page.locator(".pl-tabbar__switcher").click();

  // Open "alpha session"'s management submenu (the menuitem, not its menuitemradio twin), then
  // its destructive Close row.
  await page.getByRole("menuitem", { name: "alpha session" }).hover();
  const closeAlpha = page.getByRole("menuitem", { name: "Close", exact: true });
  await expect(closeAlpha).toBeVisible();
  await closeAlpha.click();

  // onClose routes through the same confirm dialog as the wide strip (no silent drop); confirm.
  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Delete chat" }).click();

  // One fewer session; the still-active "beta session" survives and keeps the trigger label.
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
  await expect(page.locator(".pl-tabbar__switcher-label")).toHaveText("beta session");
});

test("wide: the full tab strip renders a tab per session", async ({ page }) => {
  await twoChats(page);
  // At the default 1200px desktop width the strip is wide: a visible tab per session, no
  // collapsed switcher.
  const tabs = page.locator(".pl-tabbar__tab");
  await expect(tabs).toHaveCount(2);
  await expect(tabs.nth(0)).toBeVisible();
  await expect(tabs.nth(1)).toBeVisible();
  await expect(page.locator(".pl-tabbar__switcher")).toBeHidden();
});
