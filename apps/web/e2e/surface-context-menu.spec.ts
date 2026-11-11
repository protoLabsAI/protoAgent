import { expect, test } from "@playwright/test";

// "Open in window" is off for now (Josh 2026-11-11; see apps/web/src/contextMenu/registrations.tsx):
// widening the rail panel covers it, and it may come back as part of the open-in-new-window work.
// Until then neither the rail item's menu nor the view's background offers it, and the view's
// background opens no (empty) menu.
test("rail item menu keeps its actions but no longer offers Open in window", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const rail = page.locator(".pl-rail--right");
  await rail.getByRole("button", { name: "Work", exact: true }).click({ button: "right" });
  await expect(page.getByRole("menuitem", { name: "Hide", exact: true })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Open in window", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("right-clicking blank space inside Work opens no menu", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const list = page.locator('.work-view[data-frame="rail"] .work-view__list');
  await expect(list).toBeVisible();
  await list.click({ button: "right", position: { x: 2, y: 2 } });
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "Open in window", exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Work view", exact: true })).toHaveCount(0);
});
