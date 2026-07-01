import { expect, test } from "@playwright/test";

// Developer flags panel (ADR 0068). The mock serves channel "dev", so the Developer section
// appears under Settings ▸ This console; toggling a flag persists a device-local override that
// a Reset clears.

test("Developer panel lists flags and a toggle persists as an override", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();

  // Off prod (mock channel = dev) the Developer section is present.
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Developer", exact: true }).click();
  const panel = page.getByTestId("developer-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByText("chat.new_dashboard")).toBeVisible();
  await expect(panel.getByText("chat.experimental_widget")).toBeVisible();

  // Toggle the beta flag → an "overridden" badge + a Reset appear.
  const row = panel.locator('[data-key="flag.chat.new_dashboard"]');
  await expect(row.getByText("overridden")).toHaveCount(0);
  await row.locator(".pl-switch").click();
  await expect(row.getByText("overridden")).toBeVisible();

  // Reset → the override clears.
  await row.getByRole("button", { name: "Reset" }).click();
  await expect(row.getByText("overridden")).toHaveCount(0);
});
