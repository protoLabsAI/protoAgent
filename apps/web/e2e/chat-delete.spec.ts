import { expect, test } from "@playwright/test";

import { seedCurrentChat } from "./chat-helpers";

// Deleting a chat tab summons a confirmation dialog (not window.confirm) so a
// stray click can't silently drop a session. Cancel keeps it; confirm removes.
// The dialog is the @protolabsai/ui ConfirmDialog (role="dialog", labelled by title).
// Tabs are the DS TabBar (#832): .pl-tabbar__tab tabs, .pl-tabbar__add "+",
// .pl-tabbar__close per-tab ✕.

test("closing a chat tab confirms first; cancel keeps it, confirm deletes", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Start with two sessions so a delete is unambiguous.
  // The store reuses a pristine blank rather than piling up empty tabs, so use this one
  // first — otherwise "+" just hands the same session back. (chat-helpers.seedCurrentChat)
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar > .pl-tabbar__add").click();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);

  // Click a tab's × → the confirm dialog appears (no native confirm).
  await page.locator(".pl-tabbar__tab").first().locator(".pl-tabbar__close").click();
  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await expect(dialog).toBeVisible();

  // Cancel → nothing deleted.
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);

  // Delete again → confirm → the tab is gone.
  await page.locator(".pl-tabbar__tab").first().locator(".pl-tabbar__close").click();
  await expect(dialog).toBeVisible();
  await page.getByRole("button", { name: "Delete chat" }).click();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
});

test("Escape and click-outside cancel the delete confirmation", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  // The store reuses a pristine blank rather than piling up empty tabs, so use this one
  // first — otherwise "+" just hands the same session back. (chat-helpers.seedCurrentChat)
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar > .pl-tabbar__add").click();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);

  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await page.locator(".pl-tabbar__tab").first().locator(".pl-tabbar__close").click();
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);
});

// #3493: the harvest switch never controlled compaction, which may already have archived
// part of the chat — so the dialog says so, and "forget what this chat saved" is a second,
// opt-in switch. Confirming with it on must reach the server as `forget=true`.
test("the delete dialog says compaction may have archived the chat; forget is opt-in and reaches the server", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar > .pl-tabbar__add").click();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);

  await page.locator(".pl-tabbar__tab").first().locator(".pl-tabbar__close").click();
  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Parts of this chat may already be in the knowledge base");
  await expect(dialog).toContainText("Deleting the chat leaves them there unless you choose to forget them below.");
  const harvest = dialog.locator(".chat-delete-harvest .pl-switch__input");
  const forget = dialog.locator(".chat-delete-forget .pl-switch__input");
  await expect(harvest).not.toBeChecked();
  await expect(forget).not.toBeChecked();

  await dialog.getByText("Forget what this chat already saved to memory").click();
  await expect(forget).toBeChecked();
  const deleted = page.waitForRequest(
    (req) => req.method() === "DELETE" && /\/api\/chat\/sessions\/[^/?]+\?/.test(req.url()),
  );
  await page.getByRole("button", { name: "Delete chat" }).click();
  const url = new URL((await deleted).url());
  expect(url.searchParams.get("forget")).toBe("true");
  expect(url.searchParams.get("harvest")).toBe("false");
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
});
