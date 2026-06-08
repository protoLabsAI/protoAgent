import { expect, test } from "@playwright/test";

// Every workspace surface mounts and renders its mocked data. Guards against a
// surface crashing on an unexpected payload shape — the Runtime panel in
// particular reads the skills / MCP / plugins blocks.

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

// Grouped nav (heavy consolidation): click a rail group, then its in-surface sub-tab.
async function openSub(page, group: string, tab: string) {
  await page.getByRole("button", { name: group, exact: true }).click();
  await page.getByRole("button", { name: tab, exact: true }).click();
}

test("Studio lands directly on Workflows (Run tab removed — run is a chat gesture)", async ({ page }) => {
  // ADR 0020: execution moved to chat slash commands (/<subagent>, /<workflow>),
  // so Studio is just Workflows — no sub-nav, no Run tab.
  await page.getByRole("button", { name: "Studio", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Workflows" })).toBeVisible();
  // The old Run sub-tab is gone.
  await expect(page.locator(".stage-subnav").getByRole("button", { name: "Run", exact: true })).toHaveCount(0);
});

test("schedule is a right-rail panel that lists scheduled jobs", async ({ page }) => {
  await page.getByRole("button", { name: "Schedule", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Schedule" })).toBeVisible();
  await expect(page.getByText("Summarize overnight activity")).toBeVisible();
});

test("goals tab in the right sidebar lists active goals", async ({ page }) => {
  // Goals moved out of Studio into the right sidebar (Notes / Beads / Goals).
  await page.getByRole("button", { name: "Goals", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Goals" })).toBeVisible();
  await expect(page.getByText("All tests pass")).toBeVisible();
});

test("beads tab in the right sidebar lists issues (query-backed)", async ({ page }) => {
  // Beads panel reads via TanStack Query / Suspense (ADR 0013).
  await page.getByRole("button", { name: "Beads", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Beads" })).toBeVisible();
  await expect(page.getByText("Wire the telemetry rollup")).toBeVisible();
});

test("runtime surface: overview, tools, and MCP tabs", async ({ page }) => {
  await page.getByRole("button", { name: "Runtime", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Runtime" })).toBeVisible();
  await expect(page.getByText("SKILL.md skills loaded")).toBeVisible(); // overview

  await page.locator(".stage-subnav").getByRole("button", { name: "Tools", exact: true }).click();
  await expect(page.getByText("web_search")).toBeVisible();

  await page.locator(".stage-subnav").getByRole("button", { name: "MCP", exact: true }).click();
  await expect(page.getByText("echo · stdio")).toBeVisible(); // MCP server
});

test("plugins are their own section listing loaded plugins", async ({ page }) => {
  await page.locator(".rail").getByRole("button", { name: "Plugins", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Plugins" })).toBeVisible();
  await expect(page.getByText("Demo Plugin", { exact: false })).toBeVisible();
});
