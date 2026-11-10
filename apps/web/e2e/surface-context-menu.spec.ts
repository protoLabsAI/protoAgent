import { expect, test } from "@playwright/test";

test("view keyboard menu and collapsed rail menu open the correct surface", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const host = page.getByRole("region", { name: "Work view", exact: true });
  await expect(host).toBeVisible();
  await expect(page.getByRole("button", { name: "Expand", exact: true })).toHaveCount(0);
  await host.focus();
  await page.keyboard.press("Shift+F10");
  const expand = page.getByRole("menuitem", { name: "Open in large window", exact: true });
  await expect(expand).toBeFocused();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Work", exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Dock to rail", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => host.evaluate((el) => el.contains(document.activeElement))).toBe(true);
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

test("an inactive rail item's menu opens that view and docks it back", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const rail = page.locator(".pl-rail--right");
  const work = page.getByRole("region", { name: "Work view", exact: true });
  await expect(work).toBeVisible();
  await rail.getByRole("button", { name: "Scratch", exact: true }).click();
  const scratch = page.getByRole("region", { name: "Scratch view", exact: true });
  await expect(scratch).toBeVisible();
  await expect(work).not.toBeVisible();
  await rail.getByRole("button", { name: "Work", exact: true }).click({ button: "right" });
  await page.getByRole("menuitem", { name: "Open in large window", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Work", exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Dock to rail", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(work).toBeVisible();
  await expect(scratch).not.toBeVisible();
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
