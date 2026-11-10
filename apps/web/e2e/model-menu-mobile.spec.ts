import { expect, test } from "@playwright/test";

// The composer's model menu becomes a full-screen sheet on a phone (the `mobile` Playwright
// project, iPhone 13): a floating 180px dropdown over a keyboard-shrunk viewport is unusable
// for a list you're meant to read. As of @protolabsai/ui 0.63 `<Menu>` takes a `className`
// that DS lands on the Radix Content next to `pl-menu`, so the sheet is scoped by
// `.pl-menu.composer-model-menu` — there is no hidden marker child anymore. This asserts the
// scoping holds: the composer menu is the sheet, and a plain `.pl-menu` stays a dropdown.

test("mobile: the composer model menu is a full-screen sheet, other menus are not", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });

  const trigger = page.getByRole("button", { name: "Model for this chat" });
  await expect(trigger).toBeVisible();
  await trigger.tap();

  // The class rides on the menu Content itself (Menu className) — not a marker child — so the
  // scoped rule can match `.pl-menu.composer-model-menu`.
  const sheet = page.locator(".pl-menu.composer-model-menu");
  await sheet.waitFor();

  // Full-screen sheet: fixed, pinned to the top-left, filling the viewport width — not a
  // floating dropdown offset from the trigger.
  const vw = page.viewportSize()!.width;
  const vh = page.viewportSize()!.height;
  const position = await sheet.evaluate((el) => getComputedStyle(el).position);
  expect(position).toBe("fixed");
  const box = (await sheet.boundingBox())!;
  expect(box.x).toBeLessThanOrEqual(0.5);
  expect(box.y).toBeLessThanOrEqual(0.5);
  expect(box.width).toBeGreaterThanOrEqual(vw - 0.5);
  expect(box.height).toBeGreaterThanOrEqual(vh - 1);

  // The sheet must not spill sideways.
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);

  // Scoping proof: a `.pl-menu` WITHOUT the composer-model-menu class (any context/overflow
  // menu) stays a dropdown — the sheet rule keys on the class, so it must not apply here.
  const bareMenuPosition = await page.evaluate(() => {
    const el = document.createElement("div");
    el.className = "pl-menu";
    document.body.appendChild(el);
    const pos = getComputedStyle(el).position;
    el.remove();
    return pos;
  });
  expect(bareMenuPosition).not.toBe("fixed");
});

test("mobile: composer model menu retains touch focus, selects, and reopens", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  const trigger = page.getByRole("button", { name: "Model for this chat" });
  const sheet = page.locator(".pl-menu.composer-model-menu");
  await composer.tap();
  await composer.fill("Keep this draft");
  await expect(composer).toBeFocused();
  await trigger.tap();
  await expect(sheet).toBeVisible();
  // Let the entire touch sequence and any delayed focus/dismissal settle.
  await page.waitForTimeout(500);
  await expect(sheet).toBeVisible();
  await expect(composer).not.toBeFocused();
  await expect.poll(() => sheet.evaluate((el) => el.contains(document.activeElement))).toBe(true);
  await sheet.getByRole("menuitem", { name: "protolabs/fast", exact: true }).tap();
  await expect(sheet).toBeHidden();
  await expect(trigger).toHaveText("protolabs/fast");
  await expect(trigger).toBeFocused();
  await expect(composer).toHaveValue("Keep this draft");

  await trigger.tap();
  await expect(sheet).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await expect(trigger).toBeFocused();
  // Keyboard activation must continue to work after the touch path.
  await trigger.press("ArrowDown");
  await expect(sheet).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await composer.tap();
  await expect(composer).toBeFocused();
});
