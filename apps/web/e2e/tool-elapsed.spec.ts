import { expect, test } from "@playwright/test";

// A running tool card used to show a spinner and nothing else. `durationMs` is only
// computed at the tool-END frame, so while a call was in flight there was no duration to
// render — a card three seconds in looked exactly like one fifteen minutes in, which is
// the number an operator needs before deciding whether to hit Stop.
//
// The counter reports AGE, not liveness (it is `now - startedAt`, so it climbs whether or
// not the server is doing anything — ending a wedged turn is the server's job, #2349).
// This spec holds a tool open (the mock streams through the tool's START frame and then
// stops) and pins the two things that matter: the counter appears, and it keeps counting.
test("a long-running tool card shows an elapsed counter that keeps counting", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("hold the tool open");
  await composer.press("Enter");

  const card = page.locator(".pl-toolcard").first();
  await expect(card).toBeVisible();
  await expect(card).toHaveClass(/pl-toolcard--running/);

  // Appears once the call passes the display threshold (SHOW_ELAPSED_AFTER_MS).
  const elapsed = card.locator(".tool-elapsed");
  await expect(elapsed).toBeVisible({ timeout: 15_000 });

  // …and keeps counting, so the operator can watch it cross into "this is unusual"
  // rather than being shown a spinner of unknowable age.
  const first = (await elapsed.innerText()).trim();
  await expect(elapsed).not.toHaveText(first, { timeout: 15_000 });
});

// The counter is for calls that are actually taking a while — a fast tool must render
// exactly as before, with the DS's settled `duration` on the right and no live counter.
test("a fast tool call renders no elapsed counter", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("FANOUT do two things");
  await composer.press("Enter");

  const chip = page.locator(".pl-toolcard-summary");
  await expect(chip).toBeVisible();
  await chip.locator(".pl-toolcard-summary__head").click();
  await expect(page.locator(".pl-toolcard")).toHaveCount(2);
  await expect(page.locator(".tool-elapsed")).toHaveCount(0);
});
