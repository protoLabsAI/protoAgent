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

// #4053: deleting an ordinary chat saves a summary by default — "Save a summary" opens selected,
// so confirming untouched reaches the server as `harvest=true`. The two harvest/forget switches
// are now one 3-way memory RadioGroup (no switches survive).
test("the delete dialog saves a summary by default; confirming untouched sends harvest=true", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar > .pl-tabbar__add").click();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);

  await page.locator(".pl-tabbar__tab").first().locator(".pl-tabbar__close").click();
  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("will be deleted. This can't be undone.");
  // The 3-way memory choice replaced the two switches: no switches, "Save a summary" selected.
  await expect(dialog.locator(".pl-switch__input")).toHaveCount(0);
  await expect(dialog.getByText("Save a summary")).toBeVisible();
  await expect(dialog.locator('.chat-memory-choice .pl-radio__input[value="summary"]')).toBeChecked();
  await expect(dialog.locator('.chat-memory-choice .pl-radio__input[value="erase"]')).not.toBeChecked();
  // The dialog surfaces the otherwise-undiscoverable Shift+click quick-delete gesture (#1373).
  await expect(dialog.locator(".chat-delete-shift-hint")).toContainText("to delete instantly");

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

// #3493: "Erase from memory" removes what the chat already saved (archives + summaries) — its
// copy says so, the honesty the old compaction paragraph used to carry — and reaches the server
// as forget=true, harvest=false.
test("the delete dialog's 'Erase from memory' option reaches the server as forget=true, harvest=false", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar > .pl-tabbar__add").click();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);

  await page.locator(".pl-tabbar__tab").first().locator(".pl-tabbar__close").click();
  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Also removes summaries and archives this chat already saved.");

  await dialog.getByText("Erase from memory").click();
  await expect(dialog.locator('.chat-memory-choice .pl-radio__input[value="erase"]')).toBeChecked();
  await expect(dialog.locator('.chat-memory-choice .pl-radio__input[value="summary"]')).not.toBeChecked();
  const deleted = page.waitForRequest(
    (req) => req.method() === "DELETE" && /\/api\/chat\/sessions\/[^/?]+\?/.test(req.url()),
  );
  await page.getByRole("button", { name: "Delete chat" }).click();
  const url = new URL((await deleted).url());
  expect(url.searchParams.get("forget")).toBe("true");
  expect(url.searchParams.get("harvest")).toBe("false");
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
});

// #3493: "Don't save anything" leaves existing archives in place — harvest=false, forget absent.
test("the delete dialog's 'Don't save anything' option sends harvest=false with no forget", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar > .pl-tabbar__add").click();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);

  await page.locator(".pl-tabbar__tab").first().locator(".pl-tabbar__close").click();
  const dialog = page.getByRole("dialog", { name: "Delete this chat?" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Anything already archived from this chat stays.");

  await dialog.getByText("Don't save anything").click();
  await expect(dialog.locator('.chat-memory-choice .pl-radio__input[value="none"]')).toBeChecked();
  const deleted = page.waitForRequest(
    (req) => req.method() === "DELETE" && /\/api\/chat\/sessions\/[^/?]+\?/.test(req.url()),
  );
  await page.getByRole("button", { name: "Delete chat" }).click();
  const url = new URL((await deleted).url());
  expect(url.searchParams.get("harvest")).toBe("false");
  expect(url.searchParams.get("forget")).not.toBe("true"); // forget omitted when off
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
});

// #4053 incognito: an incognito chat is never saved to memory — the dialog drops the "Save a
// summary" option for a "never saved" note, preselects "Don't save anything", and the delete
// reaches the server as harvest=false.
test("an incognito chat has no 'Save a summary' option and deletes with harvest=false", async ({ page }) => {
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
  await expect(dialog.getByText("Save a summary")).toHaveCount(0);
  await expect(dialog.locator('.chat-memory-choice .pl-radio__input[value="summary"]')).toHaveCount(0);
  await expect(dialog).toContainText("Incognito — never saved to memory.");
  await expect(dialog.locator('.chat-memory-choice .pl-radio__input[value="none"]')).toBeChecked();
  await expect(dialog.locator('.chat-memory-choice .pl-radio__input[value="erase"]')).toHaveCount(1); // Erase still shows

  const deleted = page.waitForRequest(
    (req) => req.method() === "DELETE" && /\/api\/chat\/sessions\/[^/?]+\?/.test(req.url()),
  );
  await page.getByRole("button", { name: "Delete chat" }).click();
  const url = new URL((await deleted).url());
  expect(url.searchParams.get("harvest")).toBe("false");
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);
});

// #1373 quick-delete: Shift+click a tab's ✕ deletes with no dialog AND no harvest (harvest=false,
// forget absent) — the original contract. #4055 briefly made it auto-harvest a regular chat, which
// left the ✕ dead for the 10–30s the server-side harvest took; that is reverted.
test("Shift+click quick-delete never harvests a regular chat (harvest=false)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar > .pl-tabbar__add").click();
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);

  const deleted = page.waitForRequest(
    (req) => req.method() === "DELETE" && /\/api\/chat\/sessions\/[^/?]+\?/.test(req.url()),
  );
  await page.locator(".pl-tabbar__tab").first().locator(".pl-tabbar__close").click({ modifiers: ["Shift"] });
  const url = new URL((await deleted).url());
  expect(url.searchParams.get("harvest")).toBe("false");
  expect(url.searchParams.get("forget")).not.toBe("true");
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
