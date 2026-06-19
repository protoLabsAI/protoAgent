import { expect, test } from "@playwright/test";

// The Inbox is a utility-bar WIDGET (2026-06 IA pass) — a bottom-left pill with an
// unread badge that opens the inbox in a dialog; live updates on `inbox.item`, dismiss.

test("inbox widget: badge appears, dialog lists items, dismiss removes one", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  // A live inbox.item event bumps the widget's unread badge.
  await expect(page.getByTestId("inbox-badge")).toBeVisible();

  // Click the widget → the inbox opens in a dialog (not the Activity surface).
  await page.getByTestId("inbox-widget").click();
  const dialog = page.getByRole("dialog", { name: "Inbox" });
  await expect(dialog).toBeVisible();

  // Items from GET /api/inbox render with priority + source.
  await expect(dialog.getByText("build failed on main")).toBeVisible();
  await expect(dialog.getByText("new signup: acme.co")).toBeVisible();
  await expect(dialog.locator(".inbox-pri-now")).toBeVisible();

  // Opening the dialog marks the inbox read — the badge clears and stays clear while open.
  await expect(page.getByTestId("inbox-badge")).toHaveCount(0);

  // Dismiss the first item → it leaves the list.
  const firstItem = dialog.locator(".inbox-item", { hasText: "build failed on main" });
  await firstItem.locator(".inbox-dismiss").click();
  await expect(dialog.getByText("build failed on main")).toHaveCount(0);
});
