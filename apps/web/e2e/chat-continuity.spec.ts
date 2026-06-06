import { expect, test } from "@playwright/test";

// Chat continuity (the protoMaker pattern): navigating to another surface and
// back must NOT tear down the chat — ChatSurface stays mounted (hidden), so an
// in-flight turn keeps streaming and the conversation is intact on return.

async function rail(page, name: string) {
  await page.getByRole("button", { name, exact: true }).click();
}

test("conversation survives navigating away and back (surface stays mounted)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });

  await composer.fill("remember this message");
  await composer.press("Enter");
  await expect(page.locator(".message-user")).toHaveText(/remember this message/);

  // Leave the Chat tab → the chat surface is hidden but still mounted in the DOM
  // (not unmounted), so its state + any in-flight stream are preserved.
  await rail(page, "Activity");
  await expect(page.locator(".chat-stage")).toHaveCount(1); // still in the DOM
  await expect(page.locator(".chat-stage")).not.toBeVisible(); // just hidden
  await expect(page.locator(".message-user")).toHaveCount(1); // message not lost

  // Return → the conversation is exactly as we left it.
  await rail(page, "Chat");
  await expect(page.locator(".chat-stage")).toBeVisible();
  await expect(page.locator(".message-user")).toHaveText(/remember this message/);
});
