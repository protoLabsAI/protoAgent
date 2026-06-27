import { expect, test } from "@playwright/test";

// Shift+click a chat tab's ✕ → quick-delete: no confirm dialog, no knowledge harvest.
// (Plain click keeps the confirm dialog.)

test("Shift+click a tab's ✕ deletes it with no confirm dialog", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-tabbar__add:visible").click(); // a 2nd tab so the surface stays put
  const tabs = page.locator(".pl-tabbar__tab");
  await expect(tabs).toHaveCount(2);

  await tabs.first().locator(".pl-tabbar__close").click({ modifiers: ["Shift"] });

  await expect(tabs).toHaveCount(1); // gone immediately
  await expect(page.getByRole("dialog", { name: /Delete this chat/i })).toHaveCount(0); // no confirm
});

test("with Shift held, the trash shows only on the hovered tab's ✕ (#1373)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-tabbar__add:visible").click();
  const tabs = page.locator(".pl-tabbar__tab");
  await expect(tabs).toHaveCount(2);

  await page.keyboard.down("Shift"); // arms quick-delete mode (the --del class)
  const firstClose = tabs.first().locator(".pl-tabbar__close");
  await firstClose.hover();
  // The hovered ✕ renders the ::after trash silhouette (13px); the other tab's ✕ does not.
  const hovered = await firstClose.evaluate((el) => getComputedStyle(el, "::after").width);
  const other = await tabs.nth(1).locator(".pl-tabbar__close").evaluate((el) => getComputedStyle(el, "::after").width);
  await page.keyboard.up("Shift");
  expect(hovered).toBe("13px");
  expect(other).not.toBe("13px");
});

test("plain click a tab's ✕ still opens the confirm dialog", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-tabbar__add:visible").click();
  const tabs = page.locator(".pl-tabbar__tab");
  await expect(tabs).toHaveCount(2);

  await tabs.first().locator(".pl-tabbar__close").click();

  await expect(page.getByRole("dialog", { name: /Delete this chat/i })).toBeVisible();
  await expect(tabs).toHaveCount(2); // not deleted until confirmed
});
