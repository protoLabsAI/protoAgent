import { expect, test, type Page } from "@playwright/test";

import { expandToolCard } from "./toolcard";

// When the agent delegates with the `task` tool, the subagent's own tool calls collapse
// INSIDE the task card (revealed on expand) and the header shows a running count — so the
// card holds a stable height as the subagent works instead of growing a nested rail.
//
// Both cases drive the same flow and differ only in the mock trigger — i.e. in the ORDER the
// child's frames reach the console.

async function delegate(page: Page, prompt: string) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");

  // The task renders as a single card; its header carries the nested-tool count.
  const card = page.locator(".tool-calls .pl-toolcard").first();
  await expect(card).toBeVisible();
  await expect(card.locator(".pl-toolcard__name")).toContainText("task");
  await expect(card.locator(".pl-toolcard__name")).toContainText("1 tool");
  return card;
}

test("subagent child tools collapse inside the task card with a count", async ({ page }) => {
  const card = await delegate(page, "SUBAGENT delegate this");
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

// The robust nesting fix: a subagent's tool frames carry their parent `task` id
// (`parentToolCallId` on the wire), so the console nests them under the delegation card
// even when those frames arrive AFTER the task card has closed — the detached-delegation
// ordering the old "last open task wins" timing heuristic could not handle.
test("a subagent tool nests under the task even when its frames arrive after the task closes", async ({ page }) => {
  // The header counts the child even though its frames streamed in AFTER the task closed —
  // proof the explicit parent-id linkage attached it.
  const card = await delegate(page, "NESTLATE delegate this");
  await expect(page.getByText("Delegated; the child frame arrived after the task closed.")).toBeVisible();

  // It's nested in the body (revealed on expand), not a stray top-level sibling.
  // Expand only once the cards stop remounting — the answer text above is NOT that
  // moment (it streams in mid-turn); see e2e/toolcard.ts.
  await expandToolCard(page, card);
  await expect(card.locator(".pl-toolcard__children .pl-toolcard__name")).toHaveText("web_search");
});
