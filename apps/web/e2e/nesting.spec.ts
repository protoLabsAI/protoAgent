import { expect, test } from "@playwright/test";

// When the agent delegates with the `task` tool, the child tool calls that run
// inside the subagent are nested under the parent task card instead of a flat
// list. (A tool that starts while a `task` is still running is its child.)

test("subagent child tools nest under the task card", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "networkidle" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("SUBAGENT delegate this");
  await composer.press("Enter");

  // The parent task card is top-level inside a group.
  const group = page.locator(".tool-calls > .tool-card-group");
  await expect(group).toHaveCount(1);
  await expect(group.locator("> .tool-card .tool-card-name")).toHaveText("task");

  // The web_search child renders inside the nested children container.
  const children = group.locator("> .tool-children");
  await expect(children).toBeVisible();
  await expect(children.locator(".tool-card-name")).toHaveText("web_search");

  // And it is NOT also rendered as a sibling top-level card.
  await expect(page.locator(".tool-calls > .tool-card-group")).toHaveCount(1);
  await expect(page.locator(".tool-calls > .tool-card")).toHaveCount(0);
});
