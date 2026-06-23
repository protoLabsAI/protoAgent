import { expect, test } from "@playwright/test";

// The robust nesting fix: a subagent's tool frames carry their parent `task` id
// (`parentToolCallId` on the wire), so the console nests them under the delegation card
// even when those frames arrive AFTER the task card has closed — the detached-delegation
// ordering the old "last open task wins" timing heuristic could not handle.
test("a subagent tool nests under the task even when its frames arrive after the task closes", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("NESTLATE delegate this");
  await composer.press("Enter");

  // The task card counts the child in its header even though that child's frames streamed
  // in AFTER the task closed — proof the explicit parent-id linkage attached it.
  const card = page.locator(".tool-calls .pl-toolcard").first();
  await expect(card).toBeVisible();
  await expect(card.locator(".pl-toolcard__name")).toContainText("task");
  await expect(card.locator(".pl-toolcard__name")).toContainText("1 tool");
  // It's nested in the body (revealed on expand), not a stray top-level sibling.
  await card.locator(".pl-toolcard__head").click();
  await expect(card.locator(".pl-toolcard__children .pl-toolcard__name")).toHaveText("web_search");
});
