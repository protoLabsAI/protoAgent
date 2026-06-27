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

test("plain click a tab's ✕ still opens the confirm dialog", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".pl-tabbar__add:visible").click();
  const tabs = page.locator(".pl-tabbar__tab");
  await expect(tabs).toHaveCount(2);

  await tabs.first().locator(".pl-tabbar__close").click();

  await expect(page.getByRole("dialog", { name: /Delete this chat/i })).toBeVisible();
  await expect(tabs).toHaveCount(2); // not deleted until confirmed
});
