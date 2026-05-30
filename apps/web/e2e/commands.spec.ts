import { expect, test } from "@playwright/test";

import { SLASH_COMMANDS } from "./fixtures.mjs";

// The chat composer fetches the server's registered slash commands
// (GET /api/chat/commands) and autocompletes them as you type "/name".

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "networkidle" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("slash menu opens and lists the server commands", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/");

  const menu = page.locator(".slash-menu");
  await expect(menu).toBeVisible();
  // Each command renders as a `.slash-name` row (the description can repeat the
  // name, so scope to the name span to avoid matching twice).
  const names = await menu.locator(".slash-name").allInnerTexts();
  expect(names).toEqual(SLASH_COMMANDS.map((c) => `/${c.name}`));
});

test("filtering narrows the menu and selecting completes the command", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("/go");

  const menu = page.locator(".slash-menu");
  await expect(menu.locator(".slash-item")).toHaveCount(1);
  await expect(menu.getByText("/goal", { exact: true })).toBeVisible();

  // Enter completes the highlighted command into the composer.
  await composer.press("Enter");
  await expect(composer).toHaveValue("/goal ");
  // Completing closes the menu (a space follows the command).
  await expect(menu).toBeHidden();
});
