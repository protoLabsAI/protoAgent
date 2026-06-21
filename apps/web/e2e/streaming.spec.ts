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

test("pre-tool preamble renders above the tool card, the answer below it", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("PREAMBLE then search the web");
  await composer.press("Enter");

  const msg = page.locator(".pl-message--assistant").last();
  const preamble = msg.getByText("Let me look that up.");
  const card = msg.locator(".pl-toolcard").first();
  const final = msg.getByText("Found it — Agent Client Protocol.");
  await expect(preamble).toBeVisible();
  await expect(card).toBeVisible();
  await expect(final).toBeVisible();

  // Visual order top→bottom: preamble · tool card · answer. The bug was the preamble
  // rendering AFTER the card; stacked vertically, so y-order == DOM/render order.
  const [pre, tool, ans] = await Promise.all([
    preamble.boundingBox(),
    card.boundingBox(),
    final.boundingBox(),
  ]);
  expect(pre!.y).toBeLessThan(tool!.y);
  expect(tool!.y).toBeLessThan(ans!.y);
});
