import { expect, test } from "@playwright/test";

import { expandToolCard } from "./toolcard";

// When the agent delegates with the `task` tool, the subagent's own tool calls collapse
// INSIDE the task card (revealed on expand) and the header shows a running count — so the
// card holds a stable height as the subagent works instead of growing a nested rail.

test("subagent child tools collapse inside the task card with a count", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("SUBAGENT delegate this");
  await composer.press("Enter");

  // The task renders as a single card; its header carries the nested-tool count.
  const card = page.locator(".tool-calls .pl-toolcard").first();
  await expect(card).toBeVisible();
  await expect(card.locator(".pl-toolcard__name")).toContainText("task");
  await expect(card.locator(".pl-toolcard__name")).toContainText("1 tool");
  // The child is NOT rendered until you expand — no always-on rail (that's the bounce fix).
  await expect(page.locator(".pl-toolcard__children")).toHaveCount(0);

  await expect(page.getByText("Delegated research to a subagent and summarized.")).toBeVisible();

  // Expand → the subagent's web_search appears nested in the body. Gate on the SETTLED
  // layout, not on the answer text: the text streams in while the turn is still live, so
  // it left exactly the load-sensitive window this spec's old comment described
  // (#1272-76 regroup remounts the card, dropping the click). See e2e/toolcard.ts.
  await expandToolCard(page, card);
  await expect(card.locator(".pl-toolcard__children .pl-toolcard__name")).toHaveText("web_search");
});
