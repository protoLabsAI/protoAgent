import { expect, test, type Page } from "@playwright/test";

// Josh 2026-11-11: a rail item's double-click no longer opens the window (and "Open in window"
// is off in the context menu for now). The window still opens from the pills and the Work
// palette rows, and it must clear the app's own header row (it used to cover it and run into
// the macOS traffic lights).
const rail = (page: Page) => page.getByRole("complementary", { name: "Right surfaces", exact: true });

test("double-clicking a rail item does not open the window", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const work = rail(page).getByRole("button", { name: "Work", exact: true });
  await expect(work).toBeVisible();
  await work.dblclick();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator('.work-view[data-frame="modal"]')).toHaveCount(0);
});

test("the Work window clears the app header", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByTestId("header-menu")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await page.locator(".pl-cmdk-commands__input").fill("Open Work");
  await page.getByRole("option", { name: "Open Work", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: /^Work/ });
  await expect(dialog).toBeVisible();
  const header = await page.locator(".app-topbar").boundingBox();
  const box = await dialog.boundingBox();
  expect(header).not.toBeNull();
  expect(box).not.toBeNull();
  expect(box!.y).toBeGreaterThanOrEqual(header!.y + header!.height);
  expect(box!.y + box!.height).toBeLessThanOrEqual(900);
});
