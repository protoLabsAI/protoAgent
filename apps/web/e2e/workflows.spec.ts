import { expect, test } from "@playwright/test";

// The Workflows surface lists recipes from GET /api/workflows, shows the
// selected recipe's step DAG + inputs, and runs it via
// POST /api/workflows/{name}/run — rendering the output + per-step results.

async function openWorkflows(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Studio", exact: true }).click();
  await page.getByRole("button", { name: "Workflows", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Workflows" })).toBeVisible();
}

test("lists a recipe with its steps and inputs", async ({ page }) => {
  await openWorkflows(page);
  await expect(page.getByText("1 recipe", { exact: true })).toBeVisible();
  // The recipe's description + step DAG render.
  await expect(page.getByText("Research a topic, then write a brief.")).toBeVisible();
  const steps = page.locator(".workflow-step");
  await expect(steps).toHaveCount(2);
  await expect(steps.nth(1)).toContainText("after gather");
  // Required + optional inputs are present; the optional one shows its default.
  await expect(page.locator(".field span", { hasText: /^topic \*$/ })).toBeVisible();
  await expect(page.locator('input[placeholder="default: deep"]')).toBeVisible();
});

test("requires the required input before running", async ({ page }) => {
  await openWorkflows(page);
  const run = page.getByRole("button", { name: "Run", exact: true });
  await expect(run).toBeDisabled(); // topic is empty
});

test("runs the workflow and renders the result", async ({ page }) => {
  await openWorkflows(page);
  // Fill the required input → Run enables.
  const topic = page.locator(".subagent-grid .field").first().locator("input");
  await topic.fill("AI");
  const run = page.getByRole("button", { name: "Run", exact: true });
  await expect(run).toBeEnabled();
  await run.click();
  // The run result output renders.
  await expect(page.locator(".workflow-result .output-block").first()).toContainText("Brief on AI");
  // Per-step details are available.
  await expect(page.getByText(/Per-step output \(2\)/)).toBeVisible();
});
