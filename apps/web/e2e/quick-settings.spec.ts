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

test("the chat composer model picker selects a model and saves", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  // The model picker lives inline in the DS composer's actions slot (a <select>),
  // wired to the `model.name` settings field (host-scoped). Selecting saves immediately.
  const model = page.getByRole("combobox", { name: "Model" });
  await expect(model).toBeVisible();
  await expect(model).toHaveValue("protolabs/reasoning"); // current model from the schema
  const saved = page.waitForRequest(
    (r) => r.url().endsWith("/api/settings") && r.method() === "POST",
  );
  await model.selectOption("protolabs/fast");
  const body = (await saved).postDataJSON();
  expect(body.updates["model.name"]).toBe("protolabs/fast");
  expect(body.layer).toBe("host"); // model.name is host-scoped → saved to the host layer
});
