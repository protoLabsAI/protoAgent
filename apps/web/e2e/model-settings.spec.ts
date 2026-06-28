import { expect, test } from "@playwright/test";

// #1386 — switching the main model provider was a dead-end: the Primary model dropdown only
// offered the SAVED gateway's models, so after changing the base URL/key you couldn't pick a
// model the new key actually allows, and Test connection failed against the stale model. The
// "Get models" action probes the form's gateway and refreshes the dropdown with its models.

async function openModelSettings(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Model", exact: true }).click();
  // Field groups start collapsed — open them so the model field + actions are visible.
  const triggers = page.locator(".pl-accordion__trigger");
  await expect(triggers.first()).toBeVisible();
  for (let i = 0; i < (await triggers.count()); i++) {
    const t = triggers.nth(i);
    if ((await t.getAttribute("aria-expanded")) !== "true") await t.click();
  }
}

test("Get models refreshes the Primary model dropdown with the gateway's models (#1386)", async ({ page }) => {
  await openModelSettings(page);
  const model = page.locator("#set-model\\.name");
  // The saved dropdown offers only protolabs/reasoning + protolabs/fast (the fixture).
  await expect(model).toContainText("protolabs/reasoning");

  // Pull the gateway's models (POST /api/config/models → the mock's GATEWAY_MODELS).
  await page.getByRole("button", { name: "Get models" }).click();
  await expect(page.locator(".pl-toast", { hasText: /found 3 models/i })).toBeVisible();

  // The freshly-probed models are now selectable — picking protolabs/smart (which was NOT in the
  // saved options) proves the dropdown refreshed, so switching gateway is no longer a dead-end.
  await model.click();
  await page.getByRole("menuitemradio", { name: "protolabs/smart" }).click();
  await expect(model).toContainText("protolabs/smart");
});
