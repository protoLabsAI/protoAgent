import { expect, test } from "@playwright/test";

test("view keyboard menu and collapsed rail menu open the correct surface", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const host = page.getByRole("region", { name: "Work view", exact: true });
  await expect(host).toBeVisible();
  await expect(page.getByRole("button", { name: "Expand", exact: true })).toHaveCount(0);
  await host.focus();
  await page.keyboard.press("Shift+F10");
  await page.getByRole("menuitem", { name: "Open in large window", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Work", exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Dock to rail", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const rail = page.locator(".pl-rail--right");
  await rail.getByRole("button", { name: "Work", exact: true }).click();
  await expect(host).not.toBeVisible();
  await rail.getByRole("button", { name: "Work", exact: true }).click({ button: "right" });
  await expect(page.getByRole("menuitem", { name: "Hide", exact: true })).toBeVisible();
  await page.getByRole("menuitem", { name: "Open in large window", exact: true }).click();
  await expect(dialog.getByRole("heading", { name: "Work", exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await expect(dialog).toHaveCount(0);
});

test("right-clicking blank space inside Work opens its view menu", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const list = page.locator('.work-view[data-frame="rail"] .work-view__list');
  await expect(list).toBeVisible();
  // Click the pane's padding: the full-height child, not SurfaceHost, receives this event.
  await list.click({ button: "right", position: { x: 2, y: 2 } });
  await page.getByRole("menuitem", { name: "Open in large window", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Work", exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Dock to rail", exact: true }).click();
  await expect(list).toBeVisible();
});
