import { expect, test } from "@playwright/test";

// Knowledge: a searchable window onto the agent's knowledge base (findings,
// notes, daily-log). A single panel — Skills moved to the Agent section.

test("Knowledge lands on the searchable Store and lists chunks", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Knowledge" }).click();

  const surface = page.getByTestId("knowledge-store");
  await expect(surface).toBeVisible(); // single Store panel
  await expect(surface.getByRole("heading", { name: "Knowledge" })).toBeVisible();

  // The mocked chunks render with their content + domain badges.
  await expect(surface.getByText("Releases are cut manually via workflow_dispatch.")).toBeVisible();
  await expect(surface.getByText("protolabs/reasoning", { exact: false })).toBeVisible();
  await expect(surface.getByText("process", { exact: true })).toBeVisible(); // domain badge

  // The search box is present (server-side FTS; the mock returns the fixture).
  await expect(surface.getByPlaceholder(/Search the knowledge base/)).toBeVisible();
});
