import { expect, test } from "@playwright/test";

// Settings ▸ Secrets (ADR 0080): the schema-driven secrets_manager fields render with
// the status card beneath them; Test connection and Sync now hit /api/secrets/* and
// toast the outcome, and the card reflects the post-sync reconcile.

test("Secrets panel shows manager status and drives test/sync", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();

  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Secrets", exact: true }).click();

  // Schema-driven fields (generic form) — the enable toggle and a dependent field.
  await expect(page.getByText("Pull secrets from a manager")).toBeVisible();
  await expect(page.getByText("Machine identity client secret")).toBeVisible();

  // Status card: connected badge + the manager-owned var names.
  const card = page.getByTestId("secrets-status");
  await expect(card).toBeVisible();
  await expect(card.getByText("connected")).toBeVisible();
  await expect(card.getByText("OPENAI_API_KEY")).toBeVisible();
  await expect(card.getByText("ROTATED_KEY")).toHaveCount(0);

  // Test connection → success toast with the scope count.
  await card.getByRole("button", { name: "Test connection" }).click();
  await expect(page.locator(".pl-toast", { hasText: /3 secret/i })).toBeVisible();

  // Sync now → success toast; the refetched status shows the reconciled var set.
  await card.getByRole("button", { name: "Sync now" }).click();
  await expect(page.locator(".pl-toast", { hasText: /env var/i })).toBeVisible();
  await expect(card.getByText("ROTATED_KEY")).toBeVisible();
});
