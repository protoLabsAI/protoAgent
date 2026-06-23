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

  // Final state: one top-level task group with web_search nested inside — NOT a stray
  // top-level sibling, even though the child frames streamed after the task closed.
  const group = page.locator(".tool-calls > .pl-toolcard-group");
  await expect(group).toHaveCount(1);
  await expect(group.locator("> .pl-toolcard .pl-toolcard__name")).toHaveText("task");
  await expect(group.locator("> .pl-toolcard__children .pl-toolcard__name")).toHaveText("web_search");
  // The child is nested, not rendered as a sibling top-level card.
  await expect(page.locator(".tool-calls > .pl-toolcard")).toHaveCount(0);
});
