import { expect, test } from "@playwright/test";

// Settings ▸ Agent ▸ Snapshot (ADR 0091 Slice 1) — export this agent's secret-free definition.
//
// The behaviour that matters is REVIEW-BEFORE-DOWNLOAD: the artifact is meant to leave the
// machine, so the panel opens on the dry-run review and only then offers the zip. And the
// review must split its findings by what the operator should DO — rotate a leaked credential
// vs re-point a machine path — because filing a scrubbed home path under "credential found"
// sends someone hunting a breach that never happened.

async function openSnapshot(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Snapshot", exact: true }).click();
}

test("opens on the review: what must be re-supplied, and what was scrubbed", async ({ page }) => {
  await openSnapshot(page);
  await expect(page.getByRole("heading", { name: "Snapshot" })).toBeVisible();

  // Credentials the target needs — names only, and set-here vs merely-declared distinguished.
  await expect(page.getByText("model.api_key")).toBeVisible();
  await expect(page.getByText("mcp.github.env.GITHUB_TOKEN")).toBeVisible();
  const setHere = page.locator(".snapshot-row", { hasText: "model.api_key" });
  await expect(setHere.getByText("set here")).toBeVisible();
  const declared = page.locator(".snapshot-row", { hasText: "discord.bot_token" });
  await expect(declared.getByText("declared, unset")).toBeVisible();

  await expect(page.getByText("skipped unreadable skill asset: instance/logo.png")).toBeVisible();
});

test("splits a scrubbed credential from a scrubbed machine path", async ({ page }) => {
  await openSnapshot(page);

  // SOUL.md held something credential-shaped → the rotate-it section, which is the one
  // styled as a warning because it is a call to action, not a readout.
  const warn = page.locator(".snapshot-section--warn");
  await expect(warn).toBeVisible();
  await expect(warn.getByText("SOUL.md")).toBeVisible();
  await expect(warn.getByText(/still in this agent/)).toBeVisible();

  // The home path is NOT in that section — it lands under re-point instead.
  await expect(warn.getByText("operator.project_dir")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /Machine-local paths to re-point/ })).toBeVisible();
  await expect(page.getByText("operator.project_dir")).toBeVisible();
});

test("downloads the zip under the server's filename", async ({ page }) => {
  await openSnapshot(page);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: /Download snapshot/ }).click(),
  ]);
  // The server names the artifact (agent + timestamp); re-deriving it client-side would drift.
  expect(download.suggestedFilename()).toBe("vera-snapshot-20260804-120000.zip");
  // Scope to OUR toast: toasts are app-global, so a bare `.pl-toast` also matches one left
  // over from another spec running concurrently and trips strict mode (it did, in CI).
  await expect(page.locator(".pl-toast", { hasText: "Snapshot downloaded" })).toBeVisible();
});

test("states plainly that scrubbing is not a guarantee", async ({ page }) => {
  // Over-claiming is the real danger: an operator who believes the filter is exhaustive
  // stops reading the artifact before publishing it.
  await openSnapshot(page);
  await expect(page.getByText(/safety net, not a guarantee/)).toBeVisible();
});
