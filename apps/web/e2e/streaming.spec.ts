import { expect, test } from "@playwright/test";

// Both tests below stream a turn against e2e/mock-server.mjs — one Node process on
// ONE fixed port, single-threaded, shared by every spec file this run. Each test's
// own session (contextId) is already hermetic (frameIsForeign in src/lib/api.ts
// drops any frame stamped with a different contextId, so one test's stream can
// never render into the other's message — verified: a failure here never showed
// cross-talk, always a single self-contained session). What ISN'T hermetic is
// TIMING: two SSE streams serviced by the same event loop at once can jitter each
// other's frame-delivery gaps, and the preamble/tool/answer test asserts on
// millisecond-scale DOM state (y-ordering while the turn is still interleaving
// text↔tool parts). Under fullyParallel that occasionally raced — `boundingBox()`
// caught the preamble mid-reclassification from "answer" to "work" (a real,
// one-time React key change the instant the first tool call arrives; see
// src/chat/parts.ts's foldPlan) and returned null (#2650). `describe.configure`
// serial removes the concurrent-stream jitter between these two specific tests
// (they're a few hundred ms each — serializing them costs nothing observable);
// waiting for the turn to fully settle before measuring removes the underlying
// mid-stream race outright, independent of concurrency.
test.describe.configure({ mode: "serial" });

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
  // Wait for the turn to fully settle (the action row is streaming-gated — see
  // ChatMessageView.tsx) before measuring positions. Mid-stream, the preamble's
  // ordered `part` is reclassified from "answer" to "work" the instant the tool
  // call starts (foldPlan in src/chat/parts.ts) — a real React key change that
  // detaches and reattaches its DOM node. All three texts can pass their own
  // `toBeVisible` and still be caught mid-reclassification a moment later; settle
  // first so the y-order measurement below reads the final, stable layout.
  await expect(msg.getByRole("button", { name: "Copy" })).toBeVisible();

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
