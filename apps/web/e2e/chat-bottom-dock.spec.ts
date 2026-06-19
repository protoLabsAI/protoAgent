import { expect, test } from "@playwright/test";

// Chat can dock at the bottom now (not just the side rails). Its slot mounts unconditionally
// on whichever dock holds it, so an in-flight conversation survives switching the bottom dock
// to another surface and back — the #613 streaming-continuity contract, on the bottom dock.

test("chat docks at the bottom and survives a bottom-dock surface switch", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  const rightRail = page.locator(".pl-rail--right");
  const leftRail = page.locator(".pl-rail:not(.pl-rail--right):not(.pl-rail--bottom)");
  const bottomRail = page.locator(".pl-rail--bottom");

  // Put a second surface AND chat on the bottom dock, so the dock can switch between them.
  await rightRail.getByRole("button", { name: "Beads", exact: true }).click({ button: "right" });
  await page.locator(".pl-menu").getByText("Move to bottom dock").click();
  await leftRail.getByRole("button", { name: "Chat", exact: true }).click({ button: "right" });
  await page.locator(".pl-menu").getByText("Move to bottom dock").click();

  await expect(bottomRail.getByRole("button", { name: "Chat", exact: true })).toBeVisible();
  // Chat no longer lives on a side rail — single mount, on the bottom dock.
  await expect(leftRail.getByRole("button", { name: "Chat", exact: true })).toHaveCount(0);

  // The Chat move opens the dock on chat, so the composer is already showing — send a message.
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("remember this message");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user")).toHaveText(/remember this message/);

  // Switch the bottom dock to Beads → chat hides but stays mounted (slot, not torn down).
  await bottomRail.getByRole("button", { name: "Beads", exact: true }).click();
  await expect(page.locator(".chat-stage")).toHaveCount(1); // still in the DOM
  await expect(page.locator(".chat-stage")).not.toBeVisible(); // just hidden

  // Back to chat → the conversation is exactly as we left it (stream never torn down).
  await bottomRail.getByRole("button", { name: "Chat", exact: true }).click();
  await expect(page.locator(".chat-stage")).toBeVisible();
  await expect(page.locator(".pl-message--user")).toHaveText(/remember this message/);
});
