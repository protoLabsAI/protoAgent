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

test("subagents surface lists the registered subagent", async ({ page }) => {
  await openSub(page, "Studio", "Subagents");
  await expect(page.getByRole("heading", { name: "Manual Subagent" })).toBeVisible();
  // The registered-count kicker reflects the mocked subagent list.
  await expect(page.getByText("1 registered")).toBeVisible();
});

test("schedule surface lists scheduled jobs", async ({ page }) => {
  await openSub(page, "Studio", "Schedule");
  await expect(page.getByRole("heading", { name: "Schedule" })).toBeVisible();
  await expect(page.getByText("Summarize overnight activity")).toBeVisible();
});

test("goals surface lists active goals", async ({ page }) => {
  await openSub(page, "Studio", "Goals");
  await expect(page.getByRole("heading", { name: "Goals" })).toBeVisible();
  await expect(page.getByText("All tests pass")).toBeVisible();
});

test("runtime surface surfaces skills, MCP servers, and plugins", async ({ page }) => {
  await openSub(page, "System", "Runtime");
  await expect(page.getByRole("heading", { name: "Runtime" })).toBeVisible();

  // Extensibility blocks — the features added across the initiative.
  await expect(page.getByText("SKILL.md skills loaded")).toBeVisible();
  await expect(page.getByText("echo · stdio")).toBeVisible(); // MCP server
  await expect(page.getByText("Demo Plugin", { exact: false })).toBeVisible(); // plugin
});
