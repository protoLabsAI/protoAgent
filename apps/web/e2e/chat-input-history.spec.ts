import { expect, test } from "@playwright/test";

// Terminal-style input history (#1496): ↑ recalls previously-submitted messages into the
// composer, ↓ walks back toward the live draft. Fresh browser context per test → clean
// localStorage-backed history.

test("↑/↓ recalls and walks submitted-message history", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });

  // Submit two messages.
  await composer.fill("first message");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user").filter({ hasText: "first message" })).toBeVisible();
  await composer.fill("second message");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user").filter({ hasText: "second message" })).toBeVisible();
  await expect(composer).toHaveValue(""); // cleared on send

  // ↑ recalls newest-first; a second ↑ walks further back.
  await composer.press("ArrowUp");
  await expect(composer).toHaveValue("second message");
  await composer.press("ArrowUp");
  await expect(composer).toHaveValue("first message");
  await composer.press("ArrowUp"); // already at oldest → stays put
  await expect(composer).toHaveValue("first message");

  // ↓ walks forward; past the newest restores the (empty) draft we started from.
  await composer.press("ArrowDown");
  await expect(composer).toHaveValue("second message");
  await composer.press("ArrowDown");
  await expect(composer).toHaveValue("");
});

test("a recalled message is editable and resends the edited text", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });

  await composer.fill("draft one");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user").filter({ hasText: "draft one" })).toBeVisible();

  // Recall, append, resend → the EDITED text is what's sent (and the newest history entry).
  await composer.press("ArrowUp");
  await expect(composer).toHaveValue("draft one");
  await composer.type(" edited");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user").filter({ hasText: "draft one edited" })).toBeVisible();

  await composer.press("ArrowUp");
  await expect(composer).toHaveValue("draft one edited");
});
