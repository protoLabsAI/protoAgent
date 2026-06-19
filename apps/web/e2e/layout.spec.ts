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

  const handle = page.getByRole("separator", { name: "Resize panels" });
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

test("right panel is keyboard-resizable (ADR 0035 S3)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const right = page.locator(".pl-appshell__col--right");
  const handle = page.getByRole("separator", { name: "Resize panels" });
  const before = (await right.boundingBox())!.width;

  // ArrowLeft widens the panel (handle is on its left edge).
  await handle.focus();
  for (let i = 0; i < 6; i++) await page.keyboard.press("ArrowLeft");
  const wider = (await right.boundingBox())!.width;
  expect(wider).toBeGreaterThan(before);

  // Collapse is no longer a handle double-click — the DS made handles grab-and-drag
  // only (protoContent #223); panel collapse/restore is covered by the
  // utility-bar-toggle test above. The handle's job here is resize.
});

test("left panel shrinks to minLeftWidth, past the old maxRightWidth floor (protoContent #236)", async ({ page }) => {
  // Regression: the DS divider is zero-sum and the right column was capped at
  // maxRightWidth (720), which double-acted as a FLOOR on the left — on a wide
  // span the left couldn't go below span−720 (~50%) and sprang back when dragged
  // smaller. The DS now lets a user resize shrink the left all the way to
  // minLeftWidth (host sets 200). Drive it via the keyboard (deterministic; the
  // handle's ArrowLeft grows the right / shrinks the left, and never collapses).
  await page.goto("/app/", { waitUntil: "load" });
  const left = page.locator(".pl-appshell__col--left");
  await expect(left).toBeVisible();
  const before = (await left.boundingBox())!.width;

  const handle = page.getByRole("separator", { name: "Resize panels" });
  await handle.focus();
  for (let i = 0; i < 60; i++) await page.keyboard.press("ArrowLeft");

  const after = (await left.boundingBox())!.width;
  await expect(left).toBeVisible();        // shrank, did NOT collapse
  expect(after).toBeLessThan(before);
  // Reached ~minLeftWidth(200) — well under the old span−720 floor (~50%).
  expect(after).toBeLessThan(280);
});

test("the bottom-panel toggle sits with the layout buttons, gated until a surface is docked", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const toggleBottom = page.getByTestId("toggle-bottom");
  // Present alongside the left/right layout toggles (the bottom-right cluster).
  await expect(toggleBottom).toBeVisible();
  await expect(page.getByTestId("toggle-left")).toBeVisible();
  await expect(page.getByTestId("toggle-right")).toBeVisible();
  // Disabled by default — nothing is docked at the bottom (railOrder.bottom is empty).
  await expect(toggleBottom).toBeDisabled();
});
