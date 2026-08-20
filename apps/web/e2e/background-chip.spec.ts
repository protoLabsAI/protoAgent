import { expect, test } from "@playwright/test";

// #2896: background delegations — delegate_to(background=true) / task(run_in_background=
// true) — are dispatch RECEIPTS, not work product (the results arrive later as their own
// report messages). They fold into ONE compact "N background jobs" chip instead of stacking
// full-height cards that push the answer off-screen; expanding the chip discloses the
// individual dispatch cards.
test("background delegations fold into a single compact chip", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("BGFAN dispatch three background jobs");
  await composer.press("Enter");

  // One muted chip, not three full-height cards — and the answer is on screen beside it.
  const chip = page.locator(".tool-bg-summary .pl-toolcard-summary");
  await expect(chip).toBeVisible();
  await expect(chip.locator(".pl-toolcard-summary__text")).toHaveText("3 background jobs");
  await expect(page.locator(".pl-toolcard")).toHaveCount(0);
  await expect(page.getByText("Dispatched three background jobs.")).toBeVisible();

  // Expand → the three dispatch cards appear (existing DS disclosure behavior).
  await chip.locator(".pl-toolcard-summary__head").click();
  await expect(page.locator(".pl-toolcard")).toHaveCount(3);
  await expect(page.locator(".pl-toolcard__name").first()).toHaveText("delegate_to");
});

// Mixed turn: the foreground tool keeps the existing rendering (a lone settled tool is an
// inline card — no pointless "1 tool" chip) while the background dispatch folds into its
// own separate chip.
test("mixed turn keeps foreground cards while background folds into its chip", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("BGMIX search then delegate one background job");
  await composer.press("Enter");

  const chip = page.locator(".tool-bg-summary .pl-toolcard-summary");
  await expect(chip).toBeVisible();
  await expect(chip.locator(".pl-toolcard-summary__text")).toHaveText("1 background job");
  // Exactly one full-height card — the foreground web_search, never the bg dispatch.
  const cards = page.locator(".pl-toolcard");
  await expect(cards).toHaveCount(1);
  await expect(cards.locator(".pl-toolcard__name")).toHaveText("web_search");
  // And no second (foreground) summary chip: the only chip is the background one.
  await expect(page.locator(".pl-toolcard-summary")).toHaveCount(1);
});
