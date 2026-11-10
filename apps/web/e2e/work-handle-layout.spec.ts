import { devices, expect, test, type Page } from "@playwright/test";

// Both engine projects discover this spec; keep their browser selection while
// applying the phone profile (including touch and mobile viewport semantics).
const { defaultBrowserType: _engine, ...phone } = devices["iPhone 13"];
test.use(phone);

for (const theme of ["light", "dark"] as const) {
  test.describe(theme, () => {
    test.use({ colorScheme: theme });

    test("Work handle stays centered and full width after a phone resize", async ({ page }) => {
      await page.route("**/api/theme", (route) => route.fulfill({
        json: { theme: { mode: theme, overrides: {} } },
      }));
      await page.goto("/app/");
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expectHandleLayout(page);
      await page.setViewportSize({ width: 430, height: 932 });
      await expectHandleLayout(page);

      const handle = page.getByRole("button", { name: "Open Work", exact: true });
      const sheet = page.getByRole("dialog", { name: "Work", exact: true });
      // Tap away from the glyph to exercise the full-width touch target.
      await handle.tap({ position: { x: 8, y: 8 } });
      await expect(sheet).toBeVisible();
      await expect(page.locator(".mshell-work-handle")).toBeDisabled();
      await sheet.getByRole("button", { name: "Close", exact: true }).tap();
      await expect(sheet).toHaveCount(0);
      await expect(handle).toBeEnabled();
      await handle.focus();
      await page.keyboard.press("Enter");
      await expect(sheet).toBeVisible();
      await sheet.getByRole("button", { name: "Close", exact: true }).tap();
      await expect(sheet).toHaveCount(0);

      // Real pointer capture, with release above the button, as in mobile.spec.ts.
      const box = (await handle.boundingBox())!;
      const x = box.x + box.width / 2;
      const y = box.y + box.height / 2;
      await page.mouse.move(x, y);
      await page.mouse.down();
      await page.mouse.move(x, y - 60, { steps: 6 });
      await page.mouse.up();
      await expect(sheet).toBeVisible();
    });
  });
}

async function expectHandleLayout(page: Page) {
  const handle = page.getByRole("button", { name: "Open Work", exact: true });
  await expect(handle).toBeVisible();
  // Allow 2 CSS px for fractional layout / device-scale rounding in both engines.
  const tolerance = 2;
  await expect(async () => {
    const button = (await handle.boundingBox())!;
    const glyph = (await handle.locator(".mshell-work-glyph").boundingBox())!;
    const composer = (await page.locator(".pl-prompt").boundingBox())!;
    expect(glyph.width).toBeGreaterThan(0);
    expect(glyph.height).toBeGreaterThan(0);
    expect(Math.abs(glyph.x + glyph.width / 2 - (button.x + button.width / 2))).toBeLessThanOrEqual(tolerance);
    expect(Math.abs(glyph.y + glyph.height / 2 - (button.y + button.height / 2))).toBeLessThanOrEqual(tolerance);
    expect(Math.abs(button.x - composer.x)).toBeLessThanOrEqual(tolerance);
    expect(Math.abs(button.width - composer.width)).toBeLessThanOrEqual(tolerance);
    expect(button.width).toBeGreaterThan(page.viewportSize()!.width * 0.8);
    expect(button.y + button.height).toBeLessThanOrEqual(composer.y + tolerance);
  }).toPass();
}
