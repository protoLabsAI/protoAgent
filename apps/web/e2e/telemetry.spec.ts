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

  // Per-model + per-tool (#2697) + recent-turns tables.
  await expect(surface.getByText("By model")).toBeVisible();
  await expect(surface.getByText("claude-opus-4-8").first()).toBeVisible();
  await expect(surface.getByText("By tool")).toBeVisible();
  await expect(surface.getByText("web_search")).toBeVisible();
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

  // Tracing is ON in the default mock, so the untraced turn is a plain "—" — the
  // "tracing is disabled" cell below must not appear here (#3017).
  await expect(surface.getByTestId("telemetry-trace-off")).toHaveCount(0);
});

// #3017 — with Langfuse off, every row's trace_id is blank. A column of dashes reads
// as "these turns weren't traced", which is how a fleet ran a month of turns with
// tracing dark and nothing in the product said so. The column has to say "off".
test("Box ▸ Telemetry says tracing is off rather than showing an empty trace column", async ({ page }) => {
  await page.route("**/api/telemetry/recent*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        enabled: true,
        // Both turns untraced, and the server reports tracing disabled — no template
        // either, since _resolve_trace_url_template short-circuits when tracing is off.
        turns: [
          { task_id: "task-3", session_id: "s1", state: "completed", success: 1, model: "claude-opus-4-8",
            input_tokens: 6000, output_tokens: 900, cost_usd: 0.12, duration_ms: 8100,
            llm_calls: 3, tool_calls: 2, ended_at: "2026-06-01T05:00:08+00:00", trace_id: null },
          { task_id: "task-2", session_id: "s1", state: "failed", success: 0, model: "claude-haiku-4-5",
            input_tokens: 1800, output_tokens: 0, cost_usd: 0.0054, duration_ms: 700,
            llm_calls: 1, tool_calls: 0, ended_at: "2026-06-01T04:59:01+00:00", trace_id: null },
        ],
        langfuse_trace_url_template: null,
        tracing_enabled: false,
      }),
    });
  });

  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Telemetry", exact: true }).click();

  const surface = page.getByTestId("telemetry-surface");
  await expect(surface).toBeVisible();

  const off = surface.getByTestId("telemetry-trace-off");
  await expect(off).toHaveCount(2);           // one per untraced row, not a blank dash
  await expect(off.first()).toHaveText("off");
  await expect(off.first()).toHaveAttribute("title", /Tracing is disabled/);
  // And no link/copy affordance is fabricated for a turn that has no trace.
  await expect(surface.getByTestId("telemetry-trace-link")).toHaveCount(0);
  await expect(surface.getByTestId("telemetry-trace-copy")).toHaveCount(0);
});
