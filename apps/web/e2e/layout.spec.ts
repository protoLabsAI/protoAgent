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

// #1234: each rail toggle is gated on whether its rail holds any views — the same
// gate the bottom toggle already had, extended to left/right. In the default layout
// the left rail (chat/knowledge) and right rail (work + the boardy "scratch" right
// panel) are populated, so their toggles stay ENABLED and interactive; only the
// empty bottom dock is disabled. This pins that the gate doesn't over-disable a
// populated rail. (An empty left/right rail can't be seeded in this harness — the
// on-mount core self-heal re-adds chat/work and the mock's right-placed plugin view
// re-seeds the right rail — so the disabled state is covered for the bottom dock
// above and verified visually for left/right per the PR.)
test("populated left/right rail toggles stay enabled and interactive (#1234)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const toggleLeft = page.getByTestId("toggle-left");
  const toggleRight = page.getByTestId("toggle-right");
  await expect(toggleLeft).toBeEnabled();
  await expect(toggleRight).toBeEnabled();
  // Still toggles the right column (proves the gate left an enabled rail interactive).
  const right = page.locator(".pl-appshell__col--right");
  await expect(right).toBeVisible();
  await toggleRight.click();
  await expect(right).toHaveCount(0);
  await toggleRight.click();
  await expect(right).toBeVisible();
});

// #3684 (part 1a): the panel toggles are now DS <Button icon size="xs" variant="ghost">
// and expose their shown/collapsed state as aria-pressed (true = panel shown) instead
// of the old `is-off` class. Pin that all three carry the attribute and that it flips.
test("panel toggles expose aria-pressed (true = shown) and it flips on click (#3684)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  const toggleRight = page.getByTestId("toggle-right");
  const toggleLeft = page.getByTestId("toggle-left");
  const toggleBottom = page.getByTestId("toggle-bottom");

  // All three carry aria-pressed reflecting whether their panel is shown.
  for (const t of [toggleRight, toggleLeft, toggleBottom]) {
    await expect(t).toHaveAttribute("aria-pressed", /^(true|false)$/);
  }

  // Left + right rails are populated and shown by default → pressed.
  await expect(toggleRight).toHaveAttribute("aria-pressed", "true");
  await expect(toggleLeft).toHaveAttribute("aria-pressed", "true");

  // Clicking an enabled toggle flips aria-pressed and unmounts/restores the column.
  const right = page.locator(".pl-appshell__col--right");
  await expect(right).toBeVisible();
  await toggleRight.click();
  await expect(toggleRight).toHaveAttribute("aria-pressed", "false");
  await expect(right).toHaveCount(0);
  await toggleRight.click();
  await expect(toggleRight).toHaveAttribute("aria-pressed", "true");
  await expect(right).toBeVisible();

  // Left flips too (attribute-level — deterministic regardless of column DOM).
  await toggleLeft.click();
  await expect(toggleLeft).toHaveAttribute("aria-pressed", "false");
  await toggleLeft.click();
  await expect(toggleLeft).toHaveAttribute("aria-pressed", "true");

  // The bottom toggle is disabled by default (nothing docked) so it can't flip,
  // but it still reports its pressed state.
  await expect(toggleBottom).toBeDisabled();
  await expect(toggleBottom).toHaveAttribute("aria-pressed", /^(true|false)$/);
});

// The Settings pill + three toggles must keep a WCAG 2.5.8 (>=24x24) pointer target.
// DS xs icon buttons render a 20px visual square with a 24x24 hit-area via the
// `.pl-btn--icon.pl-btn--xs::after` overlay. That pseudo-element isn't reportable via
// boundingBox, so we assert the DS xs icon classes as the documented proxy (see PR note).
test("converted utility-bar buttons are DS xs icon buttons (>=24x24 target proxy) (#3684)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  for (const id of ["settings-widget", "toggle-bottom", "toggle-left", "toggle-right"]) {
    const btn = page.getByTestId(id);
    await expect(btn).toBeVisible();
    await expect(btn).toHaveClass(/pl-btn--xs/);
    await expect(btn).toHaveClass(/pl-btn--icon/);
  }
});
