import { expect, test } from "@playwright/test";

// Clutter cleanup: once a turn fans out (≥2 finished tool calls), the settled cards fold
// into a single expandable "N tools" summary chip so the answer isn't buried under a wall
// of cards. The chip is collapsed by default; expanding it reveals the folded cards.
test("settled tool cards fold into a summary chip", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("FANOUT do two things");
  await composer.press("Enter");

  // Both finished tools collapse behind one chip — the cards aren't rendered while folded.
  const chip = page.locator(".pl-toolcard-summary");
  await expect(chip).toBeVisible();
  await expect(chip.locator(".pl-toolcard-summary__text")).toHaveText("2 tools");
  await expect(page.locator(".pl-toolcard")).toHaveCount(0);

  // Expand → both folded cards appear.
  await chip.locator(".pl-toolcard-summary__head").click();
  await expect(page.locator(".pl-toolcard")).toHaveCount(2);
  await expect(page.locator(".pl-toolcard__name").first()).toHaveText("web_search");
});
