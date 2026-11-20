import { expect, test } from "@playwright/test";

// Settings ▸ Devices ▸ "Notifications on this device" at phone width (ADR 0135 W2e, first
// card). The `mobile` projects are an iPhone in a browser tab, where iOS has no Web Push, so
// the card must send the operator to the Home Screen instead of offering a button that can't
// work, keep its "What leaves this machine" disclosure closed, and fit the phone.

const scope = (testInfo) => `push-card-mobile-${testInfo.parallelIndex}`;

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

  const card = page.getByTestId("push-card");
  await card.scrollIntoViewIfNeeded();
  await expect(card.getByRole("heading", { name: "Notifications on this device" })).toBeVisible();
  await expect(card.getByTestId("push-state")).toContainText("On iPhone, notifications work only from the Home Screen app.");
  await expect(card.getByRole("listitem")).toHaveCount(4);
  await expect(card.getByRole("button")).toHaveCount(0);
  await expect(card.getByText("What leaves this machine")).toBeVisible();
  await expect(card.getByText("No protoAgent server sits in between")).toBeHidden();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
