import { expect, test } from "@playwright/test";

// The rail and right panel are collapsible (topbar toggles) and the right
// panel is resizable (drag its left edge); state persists to localStorage.

test("rail and right panel collapse + restore via the topbar toggles", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  const rail = page.locator(".rail");
  const right = page.locator(".right-panel");
  // A zero-width (collapsed) element reports a null boundingBox → treat as 0.
  const widthOf = async (loc) => (await loc.boundingBox())?.width ?? 0;
  await expect(rail).toBeVisible();

  // Collapse the rail → its grid column goes to 0 width.
  await page.getByTestId("toggle-rail").click();
  await expect.poll(() => widthOf(rail)).toBe(0);
  // Restore.
  await page.getByTestId("toggle-rail").click();
  await expect.poll(() => widthOf(rail)).toBeGreaterThan(0);

  // Collapse the right panel.
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
