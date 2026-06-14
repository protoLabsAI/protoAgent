import { expect, test } from "@playwright/test";

// The assistant answer streams in as append:true deltas, then the terminal
// append:false frame reconciles the authoritative final text. Guards the
// client's incremental-append path (the other specs only exercise the terminal
// replace).

test("assistant answer streams in and reconciles to the final text", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("STREAM the answer");
  await composer.press("Enter");

  const answer = page.locator(".pl-message--assistant .markdown");
  // Partial text appears before the full answer (append:true delta).
  await expect(answer).toContainText("Testing");
  // Final reconciled text — concatenated cleanly, not duplicated.
  await expect(answer).toHaveText("Testing catches bugs before users do.");
});
