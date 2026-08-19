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
  // Edit opens focused on the FIRST STEP: its prompt came from
  // GET /{name}/recipe and fills the editor pane beside the canvas.
  await expect(page.locator(".builder-prompt").first()).toHaveValue(/Research \{\{inputs\.topic\}\}/);
  // The insert-variable menu offers only what THIS step can read: declared
  // inputs, and upstream step outputs (gather has none — brief is downstream).
  await page.locator(".builder-insert-row button").click();
  await expect(page.getByRole("menuitem", { name: "{{inputs.topic}}" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "{{steps.brief.output}}" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  // The DAG canvas holds one node per step, edges from depends_on; focusing
  // the second step through its NODE swaps the editor pane.
  await expect(page.locator(".dag-node")).toHaveCount(2);
  await expect(page.locator(".react-flow__edge")).toHaveCount(1);
  await page.locator('.react-flow__node[data-id="brief"]').click();
  await page.locator(".builder-insert-row button").click();
  await expect(page.getByRole("menuitem", { name: "{{steps.gather.output}}" })).toBeVisible();
  await page.keyboard.press("Escape");
  // The toolbar chips open the section editors: workflow (fixed-in-edit name)…
  await page.locator(".builder-toolchip", { hasText: "research-and-brief" }).click();
  const name = page.getByPlaceholder("my-workflow");
  await expect(name).toHaveValue("research-and-brief");
  await expect(name).toBeDisabled();
  // …and inputs, carrying the typed-input contract fields.
  await page.locator(".builder-toolchip", { hasText: "inputs" }).click();
  await expect(page.getByPlaceholder("description — the run form's field hint").first()).toBeVisible();
});


test("duplicate step clones the focused step with a unique id", async ({ page }) => {
  await openWorkflows(page);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  // Edit opens on the first step (gather); duplicating inserts gather-copy after it.
  await page.getByRole("button", { name: "Duplicate step" }).click();
  await expect(page.locator(".dag-node")).toHaveCount(3);
  await expect(page.locator('.react-flow__node[data-id="gather-copy"]')).toBeVisible();
  // Focus moved to the clone — its id fills the editor.
  await expect(page.getByPlaceholder("step id")).toHaveValue("gather-copy");
});

test("save & test lands on the run form with the recipe selected", async ({ page }) => {
  await openWorkflows(page);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByRole("button", { name: "Save & test" }).click();
  // The builder closes onto the run view: recipe selected, inputs seeded
  // (the optional input shows its default), Run available.
  await expect(page.locator(".builder-prompt")).toHaveCount(0);
  await expect(page.getByText("Research a topic, then write a brief.")).toBeVisible();
  await expect(page.locator('input[placeholder="default: deep"]')).toBeVisible();
});
