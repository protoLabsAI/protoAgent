import { expect, test } from "@playwright/test";

import { seedCurrentChat } from "./chat-helpers";

// Runs under the `mobile` Playwright project (iPhone 13 device profile) — see
// playwright.config.ts. The desktop project ignores this file: the chat-first shell
// only exists below 768px (ADR 0086), so these would fail at 1200px by design.
// Viewport is set by the device profile; no setViewportSize needed.

// ── Chat-first mobile shell (ADR 0035 D6 as amended) ────────────────────────────────
// Replaces the old "bottom quick-bar" spec: mobile is no longer a responsive collapse of
// the dual-rail IA, it's a distinct shell where chat is the root and surfaces push over it.
test("mobile shell: chat is the root, surfaces push over it", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Below the breakpoint the DS shell is gone entirely — rails AND the old bottom quick-bar.
  await expect(page.locator(".pl-rail")).toHaveCount(0);
  await expect(page.locator(".pl-mobilenav")).toHaveCount(0);
  await expect(page.locator(".mshell")).toBeVisible();

  // Chat is the root: no back affordance, and the session title doubles as the switcher
  // (the DS TabBar's <select> collapse is suppressed here).
  await expect(page.locator('button[aria-label="Back"]')).toHaveCount(0);
  await expect(page.locator(".chat-tabbar-wrap")).toHaveCount(0);
  await expect(page.locator(".pl-prompt")).toBeVisible();

  // Drawer → a surface pushes over chat, with a back affordance and the surface's label.
  await page.getByTestId("header-menu").click();
  const drawer = page.getByTestId("app-drawer");
  await expect(drawer).toBeVisible();
  await drawer.getByRole("button", { name: "Work", exact: true }).click();
  await expect(drawer).toHaveCount(0); // picking a surface closes the drawer
  await expect(page.locator(".mshell-pushed")).toBeVisible();
  await expect(page.locator(".mshell-title-text")).toHaveText("Work");

  // The streaming-continuity contract (#613): chat is COVERED, never unmounted — a pushed
  // view must not tear down an in-flight turn.
  await expect(page.locator(".chat-stage")).toHaveCount(1);

  // Back returns to the chat root.
  await page.locator('button[aria-label="Back"]').click();
  await expect(page.locator(".mshell-pushed")).toHaveCount(0);
});

test("mobile shell: the session sheet replaces the tab strip", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Tapping the title opens the sheet; it lists the sessions and can start a new one.
  await page.locator(".mshell-title").click();
  await expect(page.locator(".session-sheet")).toBeVisible();
  await expect(page.locator(".session-sheet-row")).not.toHaveCount(0);

  // The current chat is a pristine blank, so "New" would just hand it back — both the
  // sheet's New and the header "+" are disabled rather than reading as a dead tap.
  await expect(page.locator(".session-sheet-new")).toBeDisabled();
  await page.locator(".session-sheet-backdrop").click();
  await expect(page.locator(".session-sheet")).toHaveCount(0);
  await expect(page.locator('button[aria-label="New chat"]')).toBeDisabled();

  // Use the chat, and creating becomes available again.
  await seedCurrentChat(page);
  await expect(page.locator('button[aria-label="New chat"]')).toBeEnabled();
  await page.locator(".mshell-title").click();
  await page.locator(".session-sheet-new").click();
  await expect(page.locator(".session-sheet")).toHaveCount(0); // creating closes the sheet

  await page.locator(".mshell-title").click();
  await expect(page.locator(".session-sheet-row")).toHaveCount(2);
  await page.locator(".session-sheet-backdrop").click();
  await expect(page.locator(".session-sheet")).toHaveCount(0);
});

// Guards the actual complaint: "+" was trivially spammable into a pile of blank chats.
test("mobile shell: new-chat never piles up blanks", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  const newChat = page.locator('button[aria-label="New chat"]');
  await expect(newChat).toBeDisabled(); // pristine blank → nothing to create

  await seedCurrentChat(page);
  await newChat.click(); // now a real second chat
  await expect(newChat).toBeDisabled(); // …which is itself pristine, so no third

  await page.locator(".mshell-title").click();
  await expect(page.locator(".session-sheet-row")).toHaveCount(2); // never 3+
});

// Guards the two defect classes the mobile audit turned up — both silent, both the kind
// that creep back without a test pinning them.
test("mobile shell: no input zooms iOS, no touch target under 44px", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // iOS Safari zooms the viewport on focus of any control computing under 16px. Before the
  // touch-input pass EVERY control in the app was 13-14.4px, including the chat composer.
  const zoomers = await page.evaluate(() =>
    [...document.querySelectorAll("input,textarea,select")]
      .filter((el) => parseFloat(getComputedStyle(el).fontSize) < 16)
      .map((el) => el.className?.toString() || el.tagName),
  );
  expect(zoomers).toEqual([]);

  // 44px is the HIG minimum. Hit areas may be expanded via an ::after overlay rather than
  // the box itself (so the composer bar doesn't reflow), so measure both.
  const small = await page.evaluate(() => {
    const out: string[] = [];
    for (const el of document.querySelectorAll("button,a,[role=button]")) {
      const r = el.getBoundingClientRect();
      if (!r.width) continue;
      const after = getComputedStyle(el, "::after");
      const w = Math.max(r.width, parseFloat(after.minWidth) || 0);
      const h = Math.max(r.height, parseFloat(after.minHeight) || 0);
      if (w < 44 || h < 44) out.push(`${el.className?.toString() || el.tagName} ${w}x${h}`);
    }
    return out;
  });
  expect(small).toEqual([]);
});
