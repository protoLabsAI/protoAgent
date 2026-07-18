import { expect, type Page } from "@playwright/test";

/**
 * Make the CURRENT chat tab non-pristine, so the next "+" actually creates a tab.
 *
 * `chatStore.createSession` reuses an unused blank (no messages, still titled "New chat")
 * instead of piling up identical empty tabs — otherwise "+" spams blanks the operator then
 * closes one by one. A spec that needs a SECOND tab therefore has to use the first one, or
 * "+" just hands the same session back.
 *
 * Sends a real message rather than poking localStorage: it exercises the same path a user
 * takes, and the mock backend answers immediately.
 */
export async function seedCurrentChat(page: Page, prompt = "seed") {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
  // Wait for it to land in the store — the tab is only "used" once the message exists.
  await expect(page.locator(".pl-message--user").filter({ hasText: prompt })).toBeVisible();
}

/** Seed the current tab, then open a genuinely new one via the tab-bar "+". */
export async function addChatTab(page: Page) {
  await seedCurrentChat(page);
  await page.locator(".pl-tabbar__add:visible").click();
}
