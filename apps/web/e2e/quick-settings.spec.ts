import { expect, test } from "@playwright/test";

// Contextual quick-settings + the topbar Settings overlay (ADR 0048): a gear icon
// opens a dialog editing fields via the same /api/settings path, and the central
// two-home one-stop-shop is also openable as an overlay from the topbar.

test("the topbar gear opens the Settings overlay (the two-home one-stop-shop)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("topbar-settings").click();
  const dialog = page.getByRole("dialog", { name: "Settings" });
  await expect(dialog).toBeVisible();
  // The same two scope homes render inside the overlay (the segmented toggle).
  await expect(dialog.getByRole("button", { name: "Host / App", exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Workspace", exact: true })).toBeVisible();
});

test("the chat composer model picker overrides the model per-tab (no global save)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  // The composer's inline model picker is a PER-TAB override (not a global settings
  // write). It defaults to "Default" (the configured model) and offers the gateway's
  // models; picking one stores it on the chat session and is sent with each turn.
  const model = page.getByRole("combobox", { name: "Model for this chat" });
  await expect(model).toBeVisible();
  await expect(model).toHaveValue(""); // "" → Default (the configured global model)

  // Picking a model must NOT POST /api/settings (that would change it globally).
  let settingsWrite = false;
  page.on("request", (r) => {
    if (r.url().endsWith("/api/settings") && r.method() === "POST") settingsWrite = true;
  });
  await model.selectOption("protolabs/fast"); // throws if the gateway option is absent
  await expect(model).toHaveValue("protolabs/fast");
  await page.waitForTimeout(300);
  expect(settingsWrite).toBe(false);
});
