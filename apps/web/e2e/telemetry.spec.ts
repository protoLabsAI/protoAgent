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

  // The drill-downs are tabs now (#3329), not stacked sections: the surface opens on
  // recent turns, and the per-model / per-tool (#2697) rollups are one click away.
  const views = surface.getByTestId("telemetry-views");
  await expect(surface.getByTestId("telemetry-turns")).toBeVisible();
  // The failed turn renders its state pill.
  await expect(surface.getByText("failed")).toBeVisible();

  await views.getByRole("tab", { name: "By model" }).click();
  const byModel = surface.getByTestId("telemetry-by-model");
  await expect(byModel).toBeVisible();
  // SCOPED to the table: `surface.getByText("claude-opus-4-8")` also matches the
  // pinned insights block's flag-model span, which is earlier in the DOM — that
  // assertion passed with zero table rows.
  await expect(byModel.getByText("claude-opus-4-8").first()).toBeVisible();
  // …and switching away takes the previous table off the page — the point of the tabs.
  await expect(surface.getByTestId("telemetry-turns")).toHaveCount(0);

  await views.getByRole("tab", { name: "By tool" }).click();
  await expect(surface.getByTestId("telemetry-by-tool")).toBeVisible();
  await expect(surface.getByText("web_search")).toBeVisible();

  // A single-box install has no Fleet tab at all.
  await expect(views.getByRole("tab", { name: "Fleet" })).toHaveCount(0);

  await views.getByRole("tab", { name: "Recent turns" }).click();
  await expect(surface.getByTestId("telemetry-turns")).toBeVisible();
  // A traced turn deep-links to Langfuse; the untraced one shows no link.
  const traceLink = surface.getByTestId("telemetry-trace-link");
  await expect(traceLink).toHaveCount(1);
  // Trace is the LAST of ten columns in a table wider than the dialog. It has to be
  // reachable, not clipped: scrolling it into view inside its own container is what
  // `overflow-x: auto` on the section buys, and this fails if the panel clips it.
  await traceLink.scrollIntoViewIfNeeded();
  await expect(traceLink).toBeVisible();
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

test("Box ▸ Telemetry exposes every setting without mixing save layers", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Telemetry", exact: true }).click();

  const surface = page.getByTestId("telemetry-surface");
  await surface.getByRole("button", { name: "Telemetry and prompt capture settings" }).click();
  const hostDialog = page.getByRole("dialog", { name: "Telemetry & prompt capture" });
  for (const key of [
    "telemetry.enabled",
    "telemetry.retention_days",
    "prompts.capture",
    "prompts.retention_days",
    "prompts.max_calls",
  ]) {
    await expect(hostDialog.locator(`.setting-row[data-key="${key}"]`)).toBeVisible();
  }
  await expect(hostDialog.locator('.setting-row[data-key="telemetry.fleet_trace_export"]')).toHaveCount(0);

  const hostWrite = page.waitForRequest(
    (request) => request.method() === "POST" && new URL(request.url()).pathname.endsWith("/api/settings"),
  );
  await hostDialog.locator('.setting-row[data-key="telemetry.enabled"] .pl-switch').click();
  await hostDialog.getByRole("button", { name: "Save", exact: true }).click();
  const hostBody = (await hostWrite).postDataJSON();
  expect(hostBody.layer).toBe("host");
  expect(hostBody.updates).toEqual({ "telemetry.enabled": false });

  await surface.getByRole("button", { name: "Fleet trace export setting" }).click();
  const agentDialog = page.getByRole("dialog", { name: "Fleet trace export" });
  await expect(agentDialog.locator('.setting-row[data-key="telemetry.fleet_trace_export"]')).toBeVisible();
  await expect(agentDialog.locator(".pl-badge", { hasText: "box-shared" })).toHaveCount(0);

  const agentWrite = page.waitForRequest(
    (request) => request.method() === "POST" && new URL(request.url()).pathname.endsWith("/api/settings"),
  );
  await agentDialog.locator('.setting-row[data-key="telemetry.fleet_trace_export"] .pl-switch').click();
  await agentDialog.getByRole("button", { name: "Save", exact: true }).click();
  const agentBody = (await agentWrite).postDataJSON();
  expect(agentBody.layer).toBe("agent");
  expect(agentBody.updates).toEqual({ "telemetry.fleet_trace_export": true });
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
  // The title has to name a control that EXISTS. It used to say "Settings ▸ Telemetry",
  // whose gear holds two unrelated fields — an operator following it found no tracing.
  await expect(off.first()).toHaveAttribute("title", /Tracing is disabled/);
  await expect(off.first()).toHaveAttribute("title", /Settings ▸ Tracing/);
  // And no link/copy affordance is fabricated for a turn that has no trace.
  await expect(surface.getByTestId("telemetry-trace-link")).toHaveCount(0);
  await expect(surface.getByTestId("telemetry-trace-copy")).toHaveCount(0);

  // The gear beside this table is the same four fields Settings ▸ Tracing owns, so the fix
  // is one click from the column that reported the problem (ADR 0048 chip-is-a-shortcut).
  await surface.getByRole("button", { name: "Langfuse tracing settings" }).click();
  const dialog = page.getByRole("dialog", { name: "Langfuse tracing" });
  await expect(dialog.locator('.setting-row[data-key="tracing.enabled"]')).toBeVisible();
  // Same depends_on fold as the full section: the keys appear once the toggle is on.
  await expect(dialog.locator('.setting-row[data-key="tracing.public_key"]')).toHaveCount(0);
  await dialog.locator('.setting-row[data-key="tracing.enabled"] .pl-switch').click();
  await expect(dialog.locator('.setting-row[data-key="tracing.host"]')).toBeVisible();
  await expect(dialog.locator('.setting-row[data-key="tracing.public_key"]')).toBeVisible();
  await expect(dialog.locator('.setting-row[data-key="tracing.secret_key"]')).toBeVisible();
  // The client is built once at boot, so every row carries the restart badge — and none
  // carries "box-shared": these are agent-scoped credentials, not a box default (ADR 0047 D5).
  await expect(dialog.locator(".pl-badge", { hasText: "restart" }).first()).toBeVisible();
  await expect(dialog.locator(".pl-badge", { hasText: "box-shared" })).toHaveCount(0);
});
