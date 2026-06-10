import { expect, test } from "@playwright/test";

// Each expanded tool section has a copy button that writes the raw value to the
// clipboard. Grant clipboard permissions so we can read it back and verify.
test.use({ permissions: ["clipboard-read", "clipboard-write"] });

test("copy button writes the raw value to the clipboard", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("CALC compute it");
  await composer.press("Enter");

  // Frame + copy button are the DS ToolCard/ToolSection (#832): `.pl-toolcard*`.
  const card = page.locator(".pl-toolcard").first();
  await expect(card.locator(".pl-toolcard__status--done")).toBeVisible();
  await card.locator(".pl-toolcard__head").click();

  // Copy the input section's raw value.
  const inputSection = card.locator(".pl-toolcard__section").first();
  await expect(inputSection.locator(".pl-toolcard__copy")).toBeVisible();
  await inputSection.locator(".pl-toolcard__copy").click();

  // Button flips to the copied (check) state.
  await expect(inputSection.locator(".pl-toolcard__copy")).toHaveAttribute("aria-label", "Copied");

  // Clipboard holds the raw tool input (JSON for the calculator expression).
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  expect(clip).toContain("19 * 23");
});
