import { expect, test } from "@playwright/test";

// The right panel is collapsible (bottom utility-bar toggle) and resizable
// (drag its left edge); state persists to localStorage. (The rail is fixed.)

test("right panel collapses + restores via the utility-bar toggle", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  const right = page.locator(".right-panel");
  // A zero-width (collapsed) element reports a null boundingBox → treat as 0.
  const widthOf = async (loc) => (await loc.boundingBox())?.width ?? 0;
  await expect(right).toBeVisible();

  await page.getByTestId("toggle-right").click();
  await expect.poll(() => widthOf(right)).toBe(0);
  await page.getByTestId("toggle-right").click();
  await expect.poll(() => widthOf(right)).toBeGreaterThan(0);
});

test("right panel resizes by dragging its handle and the width persists", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const right = page.locator(".right-panel");
  const before = (await right.boundingBox())!.width;

  const handle = page.getByTestId("right-resize");
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
  const reloaded = (await page.locator(".right-panel").boundingBox())!.width;
  expect(Math.abs(reloaded - after)).toBeLessThan(8);
});

test("right panel is keyboard-resizable + double-click resets (ADR 0035 S3)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const right = page.locator(".right-panel");
  const handle = page.getByTestId("right-resize");
  const before = (await right.boundingBox())!.width;

  // ArrowLeft widens the panel (handle is on its left edge).
  await handle.focus();
  for (let i = 0; i < 6; i++) await page.keyboard.press("ArrowLeft");
  const wider = (await right.boundingBox())!.width;
  expect(wider).toBeGreaterThan(before);

  // Double-click resets toward the default width.
  await handle.dblclick();
  const reset = (await right.boundingBox())!.width;
  expect(reset).toBeLessThan(wider);
});
