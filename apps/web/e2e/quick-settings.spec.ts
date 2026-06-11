import { expect, test } from "@playwright/test";

// Contextual quick-settings + the topbar Settings overlay (ADR 0048): a gear icon
// opens a dialog editing fields via the same /api/settings path, and the central
// two-home one-stop-shop is also openable as an overlay from the topbar.

test("the topbar gear opens the Settings overlay (the two-home one-stop-shop)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("topbar-settings").click();
  const dialog = page.getByRole("dialog", { name: "Settings" });
  await expect(dialog).toBeVisible();
  // The same two scope homes render inside the overlay.
  await expect(dialog.getByRole("tab", { name: "Host / App", exact: true })).toBeVisible();
  await expect(dialog.getByRole("tab", { name: "Workspace", exact: true })).toBeVisible();
});

test("the chat composer model chip edits a field in a dialog and saves", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  // The model control lives on the chat composer (the default surface), by the input.
  await page.getByRole("button", { name: "Model settings" }).click();
  const dialog = page.getByRole("dialog", { name: "Model" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("Primary model")).toBeVisible(); // model.name field
  await dialog.locator('.setting-row[data-key="model.temperature"] input').fill("0.5");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toBeHidden(); // saved → closes
});
