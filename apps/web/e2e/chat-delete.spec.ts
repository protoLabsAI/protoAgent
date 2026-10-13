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

// #4053: deleting an ordinary chat harvests it by default — the harvest switch opens ON, so
// confirming untouched reaches the server as `harvest=true`.
test("the delete dialog harvests an ordinary chat by default; confirming untouched sends harvest=true", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar > .pl-tabbar__add").click();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);

  await page.locator(".pl-tabbar__tab").first().locator(".pl-tabbar__close").click();
  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await expect(dialog).toBeVisible();
  const harvest = dialog.locator(".chat-delete-harvest .pl-switch__input");
  const forget = dialog.locator(".chat-delete-forget .pl-switch__input");
  await expect(harvest).toBeChecked();
  await expect(forget).not.toBeChecked();

  const deleted = page.waitForRequest(
    (req) => req.method() === "DELETE" && /\/api\/chat\/sessions\/[^/?]+\?/.test(req.url()),
  );
  await page.getByRole("button", { name: "Delete chat" }).click();
  const url = new URL((await deleted).url());
  expect(url.searchParams.get("harvest")).toBe("true");
  // forget is omitted from the URL entirely when off (api.deleteChatSession), so it's absent.
  expect(url.searchParams.get("forget")).not.toBe("true");
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
});

// #3493 + #4053: the harvest switch never controlled compaction, which may already have
// archived part of the chat — so the dialog says so, and "forget what this chat saved" is a
// second switch. Harvest and forget are mutually exclusive: ticking forget unticks the
// (default-on) harvest, and the delete must reach the server as forget=true, harvest=false.
test("the delete dialog says compaction may have archived the chat; forget unticks harvest and reaches the server", async ({ page }) => {
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
  await expect(harvest).toBeChecked(); // on by default (#4053)
  await expect(forget).not.toBeChecked();

  await dialog.getByText("Forget what this chat already saved to memory").click();
  await expect(forget).toBeChecked();
  await expect(harvest).not.toBeChecked(); // mutual exclusion: forget turned harvest off
  const deleted = page.waitForRequest(
    (req) => req.method() === "DELETE" && /\/api\/chat\/sessions\/[^/?]+\?/.test(req.url()),
  );
  await page.getByRole("button", { name: "Delete chat" }).click();
  const url = new URL((await deleted).url());
  expect(url.searchParams.get("forget")).toBe("true");
  expect(url.searchParams.get("harvest")).toBe("false");
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
});

// #4053 incognito: an incognito chat is never harvested — the dialog drops the harvest switch
// for a "never harvested" note, and the delete reaches the server as harvest=false. The forget
// switch still shows.
test("an incognito chat has no harvest switch and deletes with harvest=false", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await seedCurrentChat(page);
  // Shift+click the "+" opens a NEW incognito chat (#1697), which becomes the active tab.
  await page.locator(".pl-tabbar__add:visible").click({ modifiers: ["Shift"] });
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);
  const incognitoTab = page
    .locator(".pl-tabbar__tab")
    .filter({ has: page.locator(".session-incognito-icon") });
  await expect(incognitoTab).toHaveCount(1);

  await incognitoTab.locator(".pl-tabbar__close").click();
  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".chat-delete-harvest")).toHaveCount(0);
  await expect(dialog).toContainText("Incognito chat — never harvested into the knowledge base");
  await expect(dialog.locator(".chat-delete-forget .pl-switch__input")).toHaveCount(1); // forget still shows

  const deleted = page.waitForRequest(
    (req) => req.method() === "DELETE" && /\/api\/chat\/sessions\/[^/?]+\?/.test(req.url()),
  );
  await page.getByRole("button", { name: "Delete chat" }).click();
  const url = new URL((await deleted).url());
  expect(url.searchParams.get("harvest")).toBe("false");
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
});

// #4053 mutual exclusion the other way: re-ticking harvest after forget unticks forget, and
// the delete reaches the server as harvest=true, forget=false.
test("re-ticking harvest after forget unticks forget", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar > .pl-tabbar__add").click();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);

  await page.locator(".pl-tabbar__tab").first().locator(".pl-tabbar__close").click();
  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await expect(dialog).toBeVisible();
  const harvest = dialog.locator(".chat-delete-harvest .pl-switch__input");
  const forget = dialog.locator(".chat-delete-forget .pl-switch__input");

  await dialog.getByText("Forget what this chat already saved to memory").click();
  await expect(forget).toBeChecked();
  await expect(harvest).not.toBeChecked();
  await dialog.getByText("Harvest into the knowledge base first").click();
  await expect(harvest).toBeChecked();
  await expect(forget).not.toBeChecked();

  const deleted = page.waitForRequest(
    (req) => req.method() === "DELETE" && /\/api\/chat\/sessions\/[^/?]+\?/.test(req.url()),
  );
  await page.getByRole("button", { name: "Delete chat" }).click();
  const url = new URL((await deleted).url());
  expect(url.searchParams.get("harvest")).toBe("true");
  expect(url.searchParams.get("forget")).not.toBe("true"); // forget omitted when off
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
});

// #4053 + #1373 quick-delete: Shift+click a tab's ✕ deletes with no dialog, but a regular chat
// is still auto-harvested (harvest=true) — only incognito chats are skipped.
test("Shift+click quick-delete still harvests a regular chat (harvest=true)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar > .pl-tabbar__add").click();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);

  const deleted = page.waitForRequest(
    (req) => req.method() === "DELETE" && /\/api\/chat\/sessions\/[^/?]+\?/.test(req.url()),
  );
  await page.locator(".pl-tabbar__tab").first().locator(".pl-tabbar__close").click({ modifiers: ["Shift"] });
  const url = new URL((await deleted).url());
  expect(url.searchParams.get("harvest")).toBe("true");
  await expect(page.getByRole("dialog", { name: /Delete this chat/i })).toHaveCount(0); // no confirm
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
});

test("Shift+click quick-delete of an incognito chat never harvests (harvest=false)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar__add:visible").click({ modifiers: ["Shift"] }); // new incognito tab, active
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);
  const incognitoTab = page
    .locator(".pl-tabbar__tab")
    .filter({ has: page.locator(".session-incognito-icon") });
  await expect(incognitoTab).toHaveCount(1);

  const deleted = page.waitForRequest(
    (req) => req.method() === "DELETE" && /\/api\/chat\/sessions\/[^/?]+\?/.test(req.url()),
  );
  await incognitoTab.locator(".pl-tabbar__close").click({ modifiers: ["Shift"] });
  const url = new URL((await deleted).url());
  expect(url.searchParams.get("harvest")).toBe("false");
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
});
