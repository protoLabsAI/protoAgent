import { expect, test } from "@playwright/test";

// The Studio (Workflows surface, reworked in #2830): recipes list from
// GET /api/plugins/workflows/list and render as dependency-depth DAG lanes;
// Run starts a DETACHED run (POST /{name}/start) and the timeline polls its
// record (GET /runs/{run_id}); History lists every recorded run; the builder
// EDITs an existing recipe loaded from GET /{name}/recipe.

async function openWorkflows(page) {
  await page.goto("/app/", { waitUntil: "load" });
  // Studio is Workflows-only now (ADR 0020) — clicking the rail lands here.
  await page.getByRole("button", { name: "Studio", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Workflows" })).toBeVisible();
}

test("lists a recipe with its DAG lanes and inputs", async ({ page }) => {
  await openWorkflows(page);
  // The kicker reads "…recipes the engine runs over subagents · 1 recipe" (ADR 0009 contrast copy).
  await expect(page.getByText(/\b1 recipe\b/)).toBeVisible();
  await expect(page.getByText("Research a topic, then write a brief.")).toBeVisible();
  // Dependency depth renders as lanes: gather | brief in two columns.
  const lanes = page.locator(".workflow-lanes .workflow-lane");
  await expect(lanes).toHaveCount(2);
  await expect(lanes.nth(0)).toContainText("gather");
  await expect(lanes.nth(1)).toContainText("brief");
  // Required + optional inputs are present; the optional one shows its default.
  await expect(page.locator(".field span", { hasText: /^topic \*$/ })).toBeVisible();
  await expect(page.locator('input[placeholder="default: deep"]')).toBeVisible();
});

test("requires the required input before running", async ({ page }) => {
  await openWorkflows(page);
  const run = page.locator(".panel-actions").getByRole("button", { name: "Run", exact: true });
  await expect(run).toBeDisabled(); // topic is empty
});

test("runs detached and renders the live timeline", async ({ page }) => {
  await openWorkflows(page);
  const topic = page.locator(".subagent-grid .field").first().locator("input");
  await topic.fill("AI");
  const run = page.locator(".panel-actions").getByRole("button", { name: "Run", exact: true });
  await expect(run).toBeEnabled();
  await run.click();
  // The timeline appears, polling the run record — terminal in the mock, so it
  // renders done: status badge, both steps with durations, the final output.
  const timeline = page.locator(".run-timeline");
  await expect(timeline).toBeVisible();
  await expect(timeline.locator(".run-status")).toHaveText("done");
  const steps = timeline.locator(".run-step");
  await expect(steps).toHaveCount(2);
  await expect(steps.nth(0)).toContainText("gather");
  // Expanding a step reveals its output.
  await steps.nth(0).locator("summary").click();
  await expect(steps.nth(0)).toContainText("raw research notes");
  await expect(timeline.locator(".workflow-result .output-block")).toContainText("Brief on AI");
});

test("history lists recorded runs and reopens one in the timeline", async ({ page }) => {
  await openWorkflows(page);
  const history = page.locator(".run-history");
  await history.locator("summary").click();
  const row = history.locator(".run-history-row").first();
  await expect(row).toContainText("research-and-brief");
  await expect(row).toContainText("2/2 steps");
  await row.click();
  await expect(page.locator(".run-timeline .run-status")).toHaveText("done");
});

test("edit loads the full recipe into the builder", async ({ page }) => {
  await openWorkflows(page);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  // Edit opens focused on the FIRST STEP (outline-and-focus layout): its prompt
  // came from GET /{name}/recipe and fills the editor pane.
  await expect(page.locator(".builder-prompt").first()).toHaveValue(/Research \{\{inputs\.topic\}\}/);
  // Template-ref chips render for inputs and the OTHER steps (the focused
  // step never offers its own output).
  await expect(page.locator(".builder-chip", { hasText: "{{inputs.topic}}" }).first()).toBeVisible();
  await expect(page.locator(".builder-chip", { hasText: "{{steps.brief.output}}" }).first()).toBeVisible();
  // Focusing the second step through the outline swaps the editor pane.
  await expect(page.locator(".builder-card-step")).toHaveCount(2);
  await page.locator(".builder-card-step", { hasText: "brief" }).click();
  await expect(page.locator(".builder-chip", { hasText: "{{steps.gather.output}}" }).first()).toBeVisible();
  // The outline lists the workflow's shape; focusing the workflow card shows
  // the (fixed-in-edit) name.
  await page.locator(".builder-card", { hasText: "research-and-brief" }).first().click();
  const name = page.getByPlaceholder("my-workflow");
  await expect(name).toHaveValue("research-and-brief");
  await expect(name).toBeDisabled();
  // The inputs editor carries the typed-input contract fields.
  await page.locator(".builder-card", { hasText: "Inputs" }).first().click();
  await expect(page.getByPlaceholder("description — the run form's field hint").first()).toBeVisible();
});

