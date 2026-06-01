import { expect, test } from "@playwright/test";

// The System ▸ Telemetry tab renders the per-turn rollups from
// /api/telemetry/* (ADR 0006 Slice 3): summary cards + a recent-turns table.

test("System → Telemetry shows summary cards and recent turns", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Into System, then the Telemetry sub-tab.
  await page.getByRole("button", { name: "System" }).click();
  await page.getByRole("button", { name: "Telemetry" }).click();

  const surface = page.getByTestId("telemetry-surface");
  await expect(surface).toBeVisible();

  // Summary cards (from TELEMETRY_SUMMARY fixture).
  await expect(surface.getByText("Total cost")).toBeVisible();
  await expect(surface.getByText("$0.22")).toBeVisible();      // 0.2154 → $0.22
  await expect(surface.getByText("Cache hit")).toBeVisible();
  await expect(surface.getByText("60%", { exact: true })).toBeVisible(); // cache-hit card

  // Per-model + recent-turns tables.
  await expect(surface.getByText("By model")).toBeVisible();
  await expect(surface.getByText("claude-opus-4-8").first()).toBeVisible();
  await expect(surface.getByText("Recent turns")).toBeVisible();
  // The failed turn renders its state pill.
  await expect(surface.getByText("failed")).toBeVisible();

  // Insights (Slice 4, advise-only): flagged-turn warning + proven cache lever.
  const insights = surface.getByTestId("telemetry-insights");
  await expect(insights).toBeVisible();
  await expect(insights.getByText(/1 turn flagged/)).toBeVisible();
  await expect(insights.getByText(/Prompt cache: 60% hit/)).toBeVisible();
});
