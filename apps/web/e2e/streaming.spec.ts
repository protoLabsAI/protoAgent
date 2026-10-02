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
// caught the preamble mid-reclassification from "answer" to "work" (then a real,
// one-time React key change the instant the first tool call arrived; parts are now
// keyed by their index in the turn, so the node survives — ChatMessageView.tsx) and
// returned null (#2650). `describe.configure`
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
  // call starts (foldPlan in src/chat/parts.ts) and the layout reflows around it.
  // All three texts can pass their own
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

// The launch-demo glitch: a reasoning model streams a sentence, THEN calls a tool. The tool
// completes the reason+tool pair and folds the turn into the WorkBlock — and the sentence used
// to vanish into the collapsed "Working…" block at that instant. A per-frame probe records the
// sentence's on-screen state for the whole turn: once shown, it must never disappear.
test("text streamed before a tool call never disappears when the turn folds (THINKPRE)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });

  const SENTENCE = "I am protoAgent, a plugin-extensible desktop agent.";
  await page.evaluate((sentence) => {
    const w = window as unknown as { __shown: boolean[] };
    w.__shown = [];
    const tick = () => {
      const msgs = document.querySelectorAll(".pl-message--assistant");
      const last = msgs[msgs.length - 1];
      if (last) {
        // On screen = rendered in the bubble, NOT behind the collapsed WorkBlock disclosure.
        // Matched on its opening words: the reveal queue paints the sentence word by word.
        const shown = [...last.querySelectorAll(".markdown")].some(
          (el) => !el.closest(".work") && (el.textContent ?? "").includes(sentence.slice(0, 15)),
        );
        const prev = w.__shown[w.__shown.length - 1];
        if (prev !== shown) w.__shown.push(shown);
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }, SENTENCE);

  // SLOW stretches the mock's frame gaps to 300ms, so the sentence is on screen for a real
  // beat before the tool frame lands — at the 40ms default the yank could fall between frames.
  await composer.fill("THINKPRE SLOW then append a note");
  await composer.press("Enter");

  const msg = page.locator(".pl-message--assistant").last();
  await expect(msg.getByRole("button", { name: "Copy" })).toBeVisible(); // settled
  // The turn folded (reasoning + a tool call) …
  await expect(msg.locator(".work")).toBeVisible();
  // … the sentence is on screen exactly once, above the WorkBlock, the answer below it.
  const sentence = msg.locator(".markdown", { hasText: SENTENCE });
  await expect(sentence).toHaveCount(1);
  await expect(sentence).toBeVisible();
  const final = msg.getByText("Done — the note says hi.");
  await expect(final).toHaveCount(1);
  const [s, w, a] = await Promise.all([sentence.boundingBox(), msg.locator(".work").boundingBox(), final.boundingBox()]);
  expect(s!.y).toBeLessThan(w!.y);
  expect(w!.y).toBeLessThan(a!.y);

  // Never yanked: the probe saw it appear and never saw it go (no true → false transition).
  const shown = await page.evaluate(() => (window as unknown as { __shown: boolean[] }).__shown);
  expect(shown.indexOf(true)).toBeGreaterThanOrEqual(0);
  expect(shown.slice(shown.indexOf(true))).toEqual([true]);
});

// The launch-demo flash: a tool turn's whole markdown answer lands at once, and on a fresh page
// that answer is the FIRST Markdown mount. The lazy renderer suspended there and its fallback
// painted the raw SOURCE — `Done — … - **protoAgent** … \`plugins.lock\`` on one line — for
// ~0.3s (React's fallback reveal throttle) before the rendered list replaced it. A per-frame
// probe asserts the bubble never shows markdown syntax, and that the answer still renders.
test("a markdown answer never paints as raw source on its first frames (MDFLASH)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });

  await page.evaluate(() => {
    const w = window as unknown as { __raw: string[]; __frames: number };
    w.__raw = [];
    w.__frames = 0;
    const tick = () => {
      w.__frames++;
      for (const el of document.querySelectorAll(".pl-message--assistant .markdown")) {
        const text = (el as HTMLElement).innerText ?? "";
        if (/\*\*|`|(^|\n)- /.test(text)) w.__raw.push(text.slice(0, 120));
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });

  await composer.fill("MDFLASH then append a note");
  await composer.press("Enter");

  const msg = page.locator(".pl-message--assistant").last();
  await expect(msg.getByRole("button", { name: "Copy" })).toBeVisible(); // settled
  await expect(msg.locator(".markdown [data-streamdown=\"strong\"]", { hasText: "protoAgent" })).toBeVisible();
  await expect(msg.locator(".markdown li")).toHaveCount(2);
  await expect(msg.locator(".markdown [data-streamdown=\"inline-code\"]", { hasText: "plugins.lock" })).toBeVisible();

  const { raw, frames } = await page.evaluate(() => {
    const w = window as unknown as { __raw: string[]; __frames: number };
    return { raw: w.__raw, frames: w.__frames };
  });
  expect(frames).toBeGreaterThan(10); // the probe really ran through the turn
  expect(raw).toEqual([]);
});
