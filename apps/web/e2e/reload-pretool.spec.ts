import { expect, test } from "@playwright/test";

// Reload MID-TURN keeps stream order. A reasoning model streamed a sentence, then called a
// tool that is still running when the browser reloads. Cold hydration rebuilds the turn from
// the durable task and the reattach replays its snapshot — both used to replay every work
// frame first and the flattened answer text after, so the sentence the operator had been
// reading came back folded inside the collapsed "Working…" block. The work frames carry the
// answer-text offset they streamed at, so the sentence now comes back ABOVE the block, as
// the live turn drew it (mock-server.mjs pretoolTask).
test("a reload mid-turn keeps the pre-tool sentence on screen above Working…", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-pretool-midturn": "1" });
  await page.goto("/app/", { waitUntil: "load" });

  const msg = page.locator(".pl-message--assistant").last();
  const work = msg.locator(".work");
  await expect(work).toBeVisible();
  await expect(work).toContainText("Working");
  const sentence = msg.locator(".markdown", { hasText: "I am protoAgent, a plugin-extensible desktop agent." });
  await expect(sentence).toHaveCount(1);
  await expect(sentence).toBeVisible();
  // On screen in the bubble — not inside the collapsed WorkBlock — and above it.
  expect(await sentence.evaluate((el) => !!el.closest(".work"))).toBe(false);
  const [s, w] = await Promise.all([sentence.boundingBox(), work.boundingBox()]);
  expect(s!.y).toBeLessThan(w!.y);
  // The running tool is still spotlighted under the block.
  await expect(msg.locator(".work-spotlight")).toContainText("append_note");
});
