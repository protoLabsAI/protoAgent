import { expect, test } from "@playwright/test";

// Deleting a chat tab summons a confirmation dialog (not window.confirm) so a
// stray click can't silently drop a session. Cancel keeps it; confirm removes.
// The dialog is the @protolabsai/ui ConfirmDialog (role="dialog", labelled by title).

test("closing a chat tab confirms first; cancel keeps it, confirm deletes", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Start with two sessions so a delete is unambiguous.
  await page.locator(".chat-tab-new").click();
  await expect(page.locator(".chat-tab")).toHaveCount(2);

  // Click a tab's × → the confirm dialog appears (no native confirm).
  await page.locator(".chat-tab").first().locator(".chat-tab-close").click();
  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await expect(dialog).toBeVisible();

  // Cancel → nothing deleted.
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".chat-tab")).toHaveCount(2);

  // Delete again → confirm → the tab is gone.
  await page.locator(".chat-tab").first().locator(".chat-tab-close").click();
  await expect(dialog).toBeVisible();
  await page.getByRole("button", { name: "Delete chat" }).click();
  await expect(page.locator(".chat-tab")).toHaveCount(1);
});

test("Escape and click-outside cancel the delete confirmation", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(".chat-tab-new").click();
  await expect(page.locator(".chat-tab")).toHaveCount(2);

  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await page.locator(".chat-tab").first().locator(".chat-tab-close").click();
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.locator(".chat-tab")).toHaveCount(2);
});
