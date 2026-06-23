import { expect, test } from "@playwright/test";

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

  // Expand → the subagent's web_search appears nested in the body.
  await card.locator(".pl-toolcard__head").click();
  await expect(card.locator(".pl-toolcard__children .pl-toolcard__name")).toHaveText("web_search");
});
