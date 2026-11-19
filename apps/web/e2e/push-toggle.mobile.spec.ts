import { expect, test } from "@playwright/test";

// Settings ▸ Devices ▸ "Notifications on this device" at phone width (ADR 0135 W2e, first
// control). The `mobile` projects are an iPhone in a browser tab — the one place iOS never
// delivers Web Push — so the row must send the operator to the Home Screen instead of
// offering a button that can't work, and it must fit the phone.

const scope = (testInfo) => `push-toggle-mobile-${testInfo.parallelIndex}`;

test.beforeEach(async ({ page }, testInfo) => {
  await page.setExtraHTTPHeaders({ "x-e2e-fleet": scope(testInfo) });
  await page.request.post("/api/__test__/fleet/reset", { headers: { "x-e2e-fleet": scope(testInfo) } });
});

test("mobile: a paired iPhone browser tab is sent to the Home Screen, with no dead button", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem("protoagent.deviceToken", "paired-phone-token"));
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  await page.getByTestId("app-drawer").getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: /Settings sections/ }).tap();
  await page.getByRole("menuitemradio", { name: "Devices", exact: true }).tap();

  const row = page.getByTestId("push-toggle");
  await row.scrollIntoViewIfNeeded();
  await expect(row.getByText("Notifications on this device")).toBeVisible();
  await expect(row.getByTestId("push-note")).toContainText("add this console to your Home Screen");
  await expect(row.getByRole("button")).toHaveCount(0);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
