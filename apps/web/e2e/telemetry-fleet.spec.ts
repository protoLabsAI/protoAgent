import { expect, test } from "@playwright/test";

// Fleet telemetry section (ADR 0006 fleet extension, Slice 2) — the hub-side rollup
// rendered in the core Telemetry surface. Opt into the multi-box mock via the
// x-e2e-fleet-telemetry header; a single-box install shows NO fleet section (that
// path is covered by telemetry.spec.ts, which never sets the header).

async function openTelemetry(page: import("@playwright/test").Page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await page
    .locator(".settings-overlay .pl-sidenav")
    .getByRole("tab", { name: "Telemetry", exact: true })
    .click();
  const surface = page.getByTestId("telemetry-surface");
  await expect(surface).toBeVisible();
  return surface;
}

test("Telemetry ▸ Fleet lists members with rollup metrics + reachability", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-fleet-telemetry": "multi" });
  const surface = await openTelemetry(page);

  const fleet = surface.getByTestId("fleet-telemetry");
  await expect(fleet).toBeVisible();

  // The host renders its DISPLAY label ("main"), not its routing slug ("host").
  await expect(fleet.getByTestId("fleet-member-name-host")).toHaveText("main");
  // A peer whose id (slug) differs from its display label — the real live shape:
  // keyed "protoEngineer-ba4c", displayed "protoEngineer".
  await expect(fleet.getByTestId("fleet-member-name-protoEngineer-ba4c")).toHaveText("protoEngineer");

  // Host row: tagged this instance, running, with its rollup metrics.
  const hostRow = fleet.getByRole("row").filter({ hasText: "main" });
  await expect(hostRow.getByText("this instance")).toBeVisible();
  await expect(hostRow.getByText("running")).toBeVisible();

  // Peer row: rollup metrics render (success_rate 0.9167 → 92%).
  const peerRow = fleet.getByRole("row").filter({ hasText: "protoEngineer" });
  await expect(peerRow.getByText("92%", { exact: true })).toBeVisible();

  // The unreachable member is INFORMATIONAL — a status + "did not respond", no
  // rollup numbers, and crucially no action controls.
  const downRow = fleet.getByRole("row").filter({ hasText: "roxy" });
  await expect(downRow.getByText("unreachable")).toBeVisible();
  await expect(downRow.getByText("did not respond")).toBeVisible();

  // READ-ONLY: the entire Fleet section has zero buttons (no start/stop/restart).
  await expect(fleet.getByRole("button")).toHaveCount(0);
});

test("Telemetry ▸ Fleet flags carry evidence and render the member LABEL, not the slug", async ({ page }) => {
  await page.setExtraHTTPHeaders({ "x-e2e-fleet-telemetry": "multi" });
  const surface = await openTelemetry(page);

  const flags = surface.getByTestId("fleet-telemetry").getByTestId("fleet-flags");
  await expect(flags).toBeVisible();

  // --- ROUND-2 REGRESSION GUARD ------------------------------------------------
  // The host's flagged turn is keyed under slug "host" but its member label is
  // "main". The flag row MUST show "main" and MUST NOT surface the raw slug "host".
  // This assertion fails if the component is reverted to rendering flag.member /
  // the slug instead of the resolved member.label — slug ("host") != label ("main")
  // is exactly the mismatch this guard exists to catch.
  const hostFlag = flags.getByTestId("fleet-flag").filter({ hasText: "main" });
  await expect(hostFlag).toHaveCount(1);
  await expect(hostFlag.getByTestId("fleet-flag-member")).toHaveText("main");
  // Nowhere in the flags list does the raw slug "host" appear.
  await expect(flags.getByText("host", { exact: true })).toHaveCount(0);

  // The host flag's evidence: model, its per-turn cost/latency, and its reasons.
  await expect(hostFlag).toContainText("claude-opus-4-8");
  await expect(hostFlag).toContainText("$0.12"); // per-turn cost evidence
  await expect(hostFlag).toContainText("8.1s"); // per-turn latency evidence
  await expect(hostFlag).toContainText("5× median"); // the flag reason

  // A peer flag also resolves label from the members map: slug
  // "protoEngineer-ba4c" → label "protoEngineer".
  const peerFlag = flags.getByTestId("fleet-flag").filter({ hasText: "protoEngineer" });
  await expect(peerFlag.getByTestId("fleet-flag-member")).toHaveText("protoEngineer");
  await expect(flags.getByText("protoEngineer-ba4c", { exact: true })).toHaveCount(0);

  // ava is the slug == label control — a valid SECOND case, but it can't stand
  // alone (identical strings pass whether the component renders slug or label).
  const avaFlag = flags.getByTestId("fleet-flag").filter({ hasText: "ava" });
  await expect(avaFlag.getByTestId("fleet-flag-member")).toHaveText("ava");

  // Trace evidence deep-links to Langfuse via the resolved template.
  const traceLink = flags.getByTestId("fleet-trace-link").first();
  await expect(traceLink).toHaveAttribute(
    "href",
    "https://langfuse.example.com/project/p1/traces/0f9c1d2e3a4b5c6d7e8f90a1b2c3d4e5",
  );
});
