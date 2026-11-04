import { expect, test } from "@playwright/test";

// "View run" (#4726): a settled assistant turn opens the run inspector drawer on its run
// (served from /api/runs/{taskId}; the mock stamps every canned turn task-e2e-1), and a
// `?run=` link opens the same drawer at boot and leaves the address bar.

async function send(page, prompt: string) {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
}

test("View run opens the turn's run tree", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await send(page, "hello there");
  const assistant = page.locator(".pl-message--assistant").last();
  await expect(assistant.locator(".markdown")).toContainText("Done — found 8 results.");

  await assistant.getByRole("button", { name: "View run" }).click();

  const drawer = page.locator(".run-inspector");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId("run-header")).toContainText("$0.42");
  await expect(drawer.getByTestId("run-header")).toContainText("3m12s");
  const tree = drawer.getByRole("tree", { name: "Run tree" });
  await expect(tree.getByRole("treeitem")).toHaveCount(3);
  await expect(tree.getByTestId("run-node-error")).toHaveText("timed out after 600s");
});

test("a ?run= link opens the drawer at boot and is stripped from the URL", async ({ page }) => {
  await page.goto("/app/?run=task-e2e-1", { waitUntil: "load" });
  await expect(page.locator(".run-inspector").getByRole("tree", { name: "Run tree" })).toBeVisible();
  await expect.poll(() => new URL(page.url()).search).toBe("");
});
