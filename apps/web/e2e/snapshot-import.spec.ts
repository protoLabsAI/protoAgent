import { expect, test } from "@playwright/test";

// Duplicate an agent from a snapshot (ADR 0091 D3, #2106) — Settings ▸ Fleet ▸ New agent.
//
// What matters is the ORDER: pick a file → see the plan → consent → create. Applying a
// snapshot clones the repos it names and runs their code in-process, and the file came from
// somewhere else, so the control that does it must never be the first thing reachable and
// must SAY what it will run.

async function openNewAgent(page) {
  // Fleet lives in the Box group of the settings dialog, reached from the header hamburger
  // → app drawer → Settings (mirrors e2e/fleet.spec.ts's openFleet).
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  await page.getByTestId("app-drawer").getByRole("button", { name: "Settings", exact: true }).click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Fleet", exact: true }).click();
  await page.getByRole("button", { name: "New agent" }).click();
  await page.getByRole("tab", { name: "From a snapshot" }).click();
}

async function attachSnapshot(page) {
  await page.setInputFiles('.snapshot-import input[type="file"]', {
    name: "vera-snapshot.zip",
    mimeType: "application/zip",
    buffer: Buffer.from("PK\x05\x06" + "\0".repeat(18)),
  });
}

test("nothing can be created before the plan is shown", async ({ page }) => {
  await openNewAgent(page);
  // No file yet → no create control at all. The consent surface can't be skipped because
  // there is nothing to press until the snapshot has been read.
  await expect(page.getByRole("button", { name: /create agent/i })).toHaveCount(0);
});

test("the plan names every plugin it will run and flags unfamiliar sources", async ({ page }) => {
  await openNewAgent(page);
  await attachSnapshot(page);

  const warn = page.locator(".snapshot-section--warn");
  await expect(warn).toBeVisible();
  await expect(warn).toContainText("This will install and run code");
  await expect(warn).toContainText("github.com/protoLabsAI/github-plugin");
  await expect(warn).toContainText("gitlab.example/acme/acme-plugin");
  // Only the unrecognized one is called out.
  const rows = warn.locator(".snapshot-row");
  await expect(rows.filter({ hasText: "acme" })).toContainText("unfamiliar source");
  await expect(rows.filter({ hasText: "protoLabsAI" })).not.toContainText("unfamiliar source");
});

test("surfaces capability-granting config rather than hiding it", async ({ page }) => {
  await openNewAgent(page);
  await attachSnapshot(page);
  await expect(page.getByText("filesystem.allow_run")).toBeVisible();
  await expect(page.getByText(/shell command execution/)).toBeVisible();
});

test("the create button states what it will run", async ({ page }) => {
  // Consent lives on the control that performs the action, not only in a paragraph above
  // it that can be scrolled past.
  await openNewAgent(page);
  await attachSnapshot(page);
  await expect(page.getByRole("button", { name: "Install 2 plugins and create agent" })).toBeVisible();
});

test("asks only for credentials the source agent actually had", async ({ page }) => {
  await openNewAgent(page);
  await attachSnapshot(page);
  // model.api_key was set on the source; discord.bot_token was merely declared.
  await expect(page.getByText("model.api_key")).toBeVisible();
  await expect(page.getByText("discord.bot_token")).toHaveCount(0);
});

test("imports and reports an agent that still needs setup as such", async ({ page }) => {
  await openNewAgent(page);
  await attachSnapshot(page);
  await page.getByRole("button", { name: "Install 2 plugins and create agent" }).click();
  // No credential supplied → imported but NOT ready. Saying "ready" would send the operator
  // to an agent that can't reach its gateway — and NAVIGATING now would unload the page
  // before this could be read. So the incomplete path stays put: the toast, plus a durable
  // in-page summary naming exactly which secrets are still missing.
  await expect(page.locator(".pl-toast", { hasText: "needs setup" })).toBeVisible();
  const summary = page.locator(".snapshot-import .snapshot-section", { hasText: "needs setup" });
  await expect(summary).toContainText("model.api_key");
  // The operator then moves into the duplicate deliberately, detail in hand — its
  // workspace_id is the URL slug (ADR 0042), where the remaining setup happens.
  await page.getByRole("button", { name: "Open vera" }).click();
  await expect(page).toHaveURL(/\/app\/agent\/vera-ab12\//);
});

test("a complete import navigates straight into the duplicate's own console", async ({ page }) => {
  await openNewAgent(page);
  await attachSnapshot(page);
  // Supply the one credential the source agent had set → the import comes back complete.
  await page
    .locator(".snapshot-import label", { hasText: "model.api_key" })
    .locator("input")
    .first()
    .fill("sk-live-key");
  await page.getByRole("button", { name: "Install 2 plugins and create agent" }).click();
  // A ready duplicate lands the operator IN it (workspace_id is the URL slug, ADR 0042) —
  // the same navigation the archetype create flow does — not back on the fleet list.
  await expect(page).toHaveURL(/\/app\/agent\/vera-ab12\//);
});
