import { expect, test } from "@playwright/test";

// Runs under the `mobile` project (iPhone 13 profile): a deep link — the QR-label path a phone
// actually takes — pushes the plugin view over chat in the chat-first shell (ADR 0086).

test("mobile: a deep link pushes the plugin view with its params", async ({ page }) => {
  await page.goto("/app/?plugin=boardy&view=stats&vp=bin%3DB07", { waitUntil: "load" });
  await expect(page.locator(".mshell-pushed")).toBeVisible();
  await expect(page.locator(".plugin-view-frame")).toHaveAttribute("src", /\/plugins\/boardy\/stats\?bin=B07$/);
  await expect(page).toHaveURL(/\/app\/$/);
});
