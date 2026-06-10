import { expect, test } from "@playwright/test";

// When the agent delegates with the `task` tool, the child tool calls that run
// inside the subagent are nested under the parent task card instead of a flat
// list. (A tool that starts while a `task` is still running is its child.)

test("subagent child tools nest under the task card", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("SUBAGENT delegate this");
  await composer.press("Enter");

  // The parent task card is top-level inside a group. Frame = DS ToolCard (#832):
  // `.pl-toolcard-group` / `.pl-toolcard` / `.pl-toolcard__children`.
  const group = page.locator(".tool-calls > .pl-toolcard-group");
  await expect(group).toHaveCount(1);
  await expect(group.locator("> .pl-toolcard .pl-toolcard__name")).toHaveText("task");

  // The web_search child renders inside the nested children container.
  const children = group.locator("> .pl-toolcard__children");
  await expect(children).toBeVisible();
  await expect(children.locator(".pl-toolcard__name")).toHaveText("web_search");

  // And it is NOT also rendered as a sibling top-level card.
  await expect(page.locator(".tool-calls > .pl-toolcard-group")).toHaveCount(1);
  await expect(page.locator(".tool-calls > .pl-toolcard")).toHaveCount(0);
});
