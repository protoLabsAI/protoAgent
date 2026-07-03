import { expect, test } from "@playwright/test";

// Shift+click the tab bar's "+" → a NEW INCOGNITO session (#1697): same semantics as the
// tab context menu's "New incognito chat" (createSession({incognito:true})) — the new tab
// carries the eye-off glyph and the composer shows the incognito chip. Plain click is
// unchanged (a regular session, no glyph).

test("Shift+click the + opens an incognito chat; plain click stays regular", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const tabs = page.locator(".pl-tabbar__tab");
  await expect(tabs).toHaveCount(1);

  // Shift+click the add "+" → the new (active) tab is incognito.
  await page.locator(".pl-tabbar__add:visible").click({ modifiers: ["Shift"] });
  await expect(tabs).toHaveCount(2);
  await expect(page.locator(".pl-tabbar__tab--active .session-incognito-icon")).toBeVisible();
  // …and the composer shows the incognito chip for that session.
  await expect(page.locator(".composer-incognito-toggle:visible")).toBeVisible();

  // Plain click still creates a REGULAR session: no glyph on the new active tab, no chip.
  await page.locator(".pl-tabbar__add:visible").click();
  await expect(tabs).toHaveCount(3);
  await expect(page.locator(".pl-tabbar__tab--active .session-incognito-icon")).toHaveCount(0);
  await expect(page.locator(".composer-incognito-toggle:visible")).toHaveCount(0);
});

test("Shift+Enter on the focused + is the keyboard twin of the gesture", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const tabs = page.locator(".pl-tabbar__tab");
  await expect(tabs).toHaveCount(1);

  await page.locator(".pl-tabbar__add:visible").focus();
  await page.keyboard.press("Shift+Enter");
  await expect(tabs).toHaveCount(2);
  await expect(page.locator(".pl-tabbar__tab--active .session-incognito-icon")).toBeVisible();

  // Plain Enter on the focused + still creates a REGULAR session.
  await page.locator(".pl-tabbar__add:visible").focus();
  await page.keyboard.press("Enter");
  await expect(tabs).toHaveCount(3);
  await expect(page.locator(".pl-tabbar__tab--active .session-incognito-icon")).toHaveCount(0);
});

test("the + button's hover hint teaches the Shift+click gesture", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".pl-tabbar__add:visible")).toHaveAttribute(
    "title",
    /Shift\+click for incognito/,
  );
});
