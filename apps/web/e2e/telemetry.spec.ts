import { expect, test } from "@playwright/test";

// The Telemetry section renders the per-turn rollups from
// /api/telemetry/* (ADR 0006 Slice 3): summary cards + a recent-turns table.

test("Box ▸ Telemetry shows the summary cards and recent turns", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // Telemetry is a section in the Settings dialog (Box group), opened from the utility-bar
  // Settings pill — the single Settings door (ADR 0048; the drawer shortcut was removed).
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Telemetry", exact: true }).click();

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
  // A traced turn deep-links to Langfuse; the untraced one shows no link.
  const traceLink = surface.getByTestId("telemetry-trace-link");
  await expect(traceLink).toHaveCount(1);
  await expect(traceLink).toHaveAttribute(
    "href",
    "https://langfuse.example.com/project/p1/traces/0f9c1d2e3a4b5c6d7e8f90a1b2c3d4e5",
  );

  // Insights (Slice 4, advise-only): flagged-turn warning + proven cache lever.
  const insights = surface.getByTestId("telemetry-insights");
  await expect(insights).toBeVisible();
  await expect(insights.getByText(/1 turn flagged/)).toBeVisible();
  await expect(insights.getByText(/Prompt cache: 60% hit/)).toBeVisible();
});
