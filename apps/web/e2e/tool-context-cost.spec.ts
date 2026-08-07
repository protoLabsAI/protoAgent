import { expect, test } from "@playwright/test";

// Per-tool-call context cost (#2282). The chip is an ESTIMATE from the result's size, so
// these specs pin the two things a wrong implementation gets wrong in the DOM: that it
// appears on a fat result, and that it stays off everything else. The arithmetic itself
// is unit-tested in src/chat/toolCost.test.ts.

async function run(page, prompt: string) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
  const card = page.locator(".pl-toolcard").first();
  await expect(card).toBeVisible();
  await expect(card.locator(".pl-toolcard__status--done")).toBeVisible();
  // Settled layout only: while the turn is live the card lives in `.tool-spotlight` and
  // remounts on settle (e2e/toolcard.ts), so a header read before then can catch the
  // running branch — which deliberately renders no chip.
  await expect(page.locator(".tool-spotlight")).toHaveCount(0);
  return card;
}

test("a fat tool result gets an estimated context-cost chip", async ({ page }) => {
  // Keyword only — fixtures.mjs uppercases the prompt and matches FETCH (and friends)
  // ahead of BIGRESULT, so any incidental "fetch"/"time"/"calc" word in the prompt would
  // silently select a different scenario.
  const card = await run(page, "BIGRESULT");
  const cost = card.locator(".tool-ctx-cost");
  await expect(cost).toBeVisible();
  // 8000+ chars / 4 ≈ 2k tokens, rendered compactly by lib/format `tokens()`.
  await expect(cost).toContainText("ctx");
  await expect(cost).toContainText("~2");
  // Never presented as a measurement — the "~" and the tooltip are the honesty.
  await expect(cost).toHaveAttribute("title", /estimate, not a measurement/i);
});

test("a small tool result gets no chip — the estimate is signal, not decoration", async ({ page }) => {
  const card = await run(page, "TIME in tokyo");
  await expect(card.locator(".tool-ctx-cost")).toHaveCount(0);
});

// NOT covered here: the error branch (`status === "error"` ⇒ no chip). The mock's
// `toWire` maps every non-start phase to "completed" and has no failure vocabulary, so
// the TOOLERR scenario settles GREEN — an e2e written against it would pass because its
// output is 44 chars, not because the call failed, and would keep passing if the error
// branch were deleted. It is covered honestly in src/chat/toolCost.test.ts, which can
// construct `{status: "error"}` with a large output directly.
