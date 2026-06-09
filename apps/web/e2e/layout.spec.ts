import { expect, test } from "@playwright/test";

// The right panel (DS AppShell right column) is collapsible (bottom utility-bar
// toggle) and resizable (drag its left edge / arrow keys); state persists to
// localStorage. Double-clicking the handle collapses it (DS behavior).

test("right panel collapses + restores via the utility-bar toggle", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  const right = page.locator(".pl-appshell__col--right");
  await expect(right).toBeVisible();

  // The DS collapses by UNMOUNTING the column (the old shell kept it at width 0).
  await page.getByTestId("toggle-right").click();
  await expect(right).toHaveCount(0);
  await page.getByTestId("toggle-right").click();
  await expect(right).toBeVisible();
});

test("right panel resizes by dragging its handle and the width persists", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const right = page.locator(".pl-appshell__col--right");
  const before = (await right.boundingBox())!.width;

  const handle = page.getByRole("separator", { name: "Resize right panel" });
  const hb = (await handle.boundingBox())!;
  // Drag the handle left ~120px → the panel grows.
  await page.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2);
  await page.mouse.down();
  await page.mouse.move(hb.x - 120, hb.y + hb.height / 2, { steps: 8 });
  await page.mouse.up();

  const after = (await right.boundingBox())!.width;
  expect(after).toBeGreaterThan(before + 50);

  // Persists across a reload.
  await page.reload({ waitUntil: "load" });
  const reloaded = (await page.locator(".pl-appshell__col--right").boundingBox())!.width;
  expect(Math.abs(reloaded - after)).toBeLessThan(8);
});

test("right panel is keyboard-resizable + double-click collapses (ADR 0035 S3)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const right = page.locator(".pl-appshell__col--right");
  const handle = page.getByRole("separator", { name: "Resize right panel" });
  const before = (await right.boundingBox())!.width;

  // ArrowLeft widens the panel (handle is on its left edge).
  await handle.focus();
  for (let i = 0; i < 6; i++) await page.keyboard.press("ArrowLeft");
  const wider = (await right.boundingBox())!.width;
  expect(wider).toBeGreaterThan(before);

  // Double-click the handle collapses the panel (DS AppShell behavior — unmounts it).
  await handle.dblclick();
  await expect(right).toHaveCount(0);
});
