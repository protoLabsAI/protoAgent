import { expect, test } from "@playwright/test";

// Knowledge → Store (ADR 0020): a searchable window onto the agent's knowledge
// base (findings, notes, daily-log). Lands as the default Knowledge sub-tab.

test("Knowledge lands on the searchable Store and lists chunks", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Knowledge" }).click();

  const surface = page.getByTestId("knowledge-store");
  await expect(surface).toBeVisible(); // Store is the default sub-tab
  await expect(surface.getByRole("heading", { name: "Knowledge" })).toBeVisible();

  // The mocked chunks render with their content + domain badges.
  await expect(surface.getByText("Releases are cut manually via workflow_dispatch.")).toBeVisible();
  await expect(surface.getByText("protolabs/reasoning", { exact: false })).toBeVisible();
  await expect(surface.getByText("process", { exact: true })).toBeVisible(); // domain badge

  // The search box is present (server-side FTS; the mock returns the fixture).
  await expect(surface.getByPlaceholder(/Search the knowledge base/)).toBeVisible();
});

test("Knowledge sub-nav switches between Store and Skills", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Knowledge" }).click();

  await expect(page.getByTestId("knowledge-store")).toBeVisible();
  await page.locator(".stage-subnav").getByRole("button", { name: "Skills", exact: true }).click();
  await expect(page.getByTestId("playbooks-surface")).toBeVisible();
  await page.locator(".stage-subnav").getByRole("button", { name: "Store", exact: true }).click();
  await expect(page.getByTestId("knowledge-store")).toBeVisible();
});
