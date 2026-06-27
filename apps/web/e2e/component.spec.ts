import { expect, test } from "@playwright/test";

// Inline chat components (ADR 0051 / #1323): show_component emits a component-v1 DataPart
// that the console's extensible registry renders inline — below the answer, in order, with
// the show_component tool card suppressed (it's a render directive, not work to fold).

test("show_component renders an inline table; its tool card is suppressed (#1323)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("COMPONENT: show the fleet");
  await composer.press("Enter");

  const msg = page.locator(".pl-message--assistant").first();

  // The table renders inline via the component registry.
  const comp = msg.locator(".chat-comp").first();
  await expect(comp).toBeVisible();
  await expect(comp).toContainText("Fleet"); // title
  await expect(comp).toContainText("Ship");
  await expect(comp).toContainText("Status");
  await expect(comp).toContainText("Hauler");
  await expect(comp).toContainText("active");

  // The show_component tool card is SUPPRESSED — no work-timeline noise.
  await expect(msg.locator(".pl-toolcard")).toHaveCount(0);

  // Stable order: the answer text precedes the inline component.
  await expect(msg.getByText("Here's the fleet breakdown.")).toBeVisible();
});
