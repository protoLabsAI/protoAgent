import { expect, test } from "@playwright/test";

import { routeSnapshot } from "./routeSnapshot";

// The two-step archetype flow at phone width (the `mobile` Playwright project, iPhone 13).
// The set-up dialog must fit the viewport — no sideways scroll — with its Back/Create foot
// reachable, and the picker's cards + Next usable by touch. Same fleet scoping as
// fleet.spec.ts: creating mutates the mock fleet, so claim a private scope and reset it.
const fleetScope = (testInfo) => `new-agent-mobile-${testInfo.parallelIndex}`;

test.beforeEach(async ({ page }, testInfo) => {
  const scope = fleetScope(testInfo);
  await page.setExtraHTTPHeaders({ "x-e2e-fleet": scope });
  await page.request.post("/api/__test__/fleet/reset", { headers: { "x-e2e-fleet": scope } });
});

async function noSidewaysScroll(page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

test("mobile: New agent → pick → set-up dialog fits the phone and creates", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  await page.getByTestId("app-drawer").getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("combobox", { name: "Settings sections" }).selectOption("Fleet"); // the side nav collapses to a <select> on a phone
  await page.getByRole("button", { name: "New agent" }).click();

  await page.locator(".pl-radiocard", { hasText: "Product Manager" }).tap();
  await page.getByRole("button", { name: /^Next/ }).tap();
  const dialog = page.locator(".archetype-setup-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("Agent name")).toHaveValue("product-manager");

  // The dialog stays inside the viewport and its foot (Back / Create) is on screen.
  const vw = page.viewportSize()!.width;
  const box = (await dialog.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(vw + 0.5);
  await expect(dialog.getByRole("button", { name: /^Create/ })).toBeInViewport();
  await expect(dialog.getByRole("button", { name: /^Back/ })).toBeInViewport();
  await noSidewaysScroll(page);

  await dialog.getByLabel("Agent name").fill("phonebot");
  await dialog.getByRole("button", { name: /^Create/ }).tap();
  await expect(page).toHaveURL(/\/app\/agent\/phonebot-ab12\//);
});

test("mobile: Setup Wizard — pick step, then the set-up step with the folder picker", async ({ page }) => {
  await routeSnapshot(page, "/api/runtime/status", (json) => {
    json.setup_complete = false;
  });
  await page.goto("/app/", { waitUntil: "load" });
  const wizard = page.getByRole("dialog", { name: "Setup" });
  await wizard.getByRole("button", { name: "Next" }).tap();
  await wizard.getByRole("button", { name: /^Advanced \(1\)/ }).tap();
  await wizard.locator(".pl-radiocard", { hasText: "Project Manager" }).tap();
  await wizard.getByRole("button", { name: "Next" }).tap();
  await expect(wizard.getByLabel("Agent name")).toHaveValue("project-manager");
  await expect(wizard.getByRole("button", { name: /Browse/ })).toBeVisible();
  await noSidewaysScroll(page);
});
