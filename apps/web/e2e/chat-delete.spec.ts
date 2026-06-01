import { expect, test } from "@playwright/test";

// Deleting a chat tab summons a custom confirmation (not window.confirm) so a
// stray click can't silently drop a session. Cancel keeps it; confirm removes.

test("closing a chat tab confirms first; cancel keeps it, confirm deletes", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Start with two sessions so a delete is unambiguous.
  await page.locator(".chat-tab-new").click();
  await expect(page.locator(".chat-tab")).toHaveCount(2);

  // Click a tab's × → the custom dialog appears (no native confirm).
  await page.locator(".chat-tab").first().locator(".chat-tab-close").click();
  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();

  // Cancel → nothing deleted.
  await page.getByTestId("confirm-cancel").click();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".chat-tab")).toHaveCount(2);

  // Delete again → confirm → the tab is gone.
  await page.locator(".chat-tab").first().locator(".chat-tab-close").click();
  await expect(page.getByTestId("confirm-dialog")).toBeVisible();
  await page.getByTestId("confirm-accept").click();
  await expect(page.locator(".chat-tab")).toHaveCount(1);
});

test("Escape and click-outside cancel the delete confirmation", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".chat-tab-new").click();
  await expect(page.locator(".chat-tab")).toHaveCount(2);

  await page.locator(".chat-tab").first().locator(".chat-tab-close").click();
  await expect(page.getByTestId("confirm-dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("confirm-dialog")).toBeHidden();
  await expect(page.locator(".chat-tab")).toHaveCount(2);
});
