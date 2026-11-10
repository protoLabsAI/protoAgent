import { expect, test, type Locator, type Page } from "@playwright/test";

async function selectTasks(page: Page, work: Locator) {
  const picker = work.getByRole("button", { name: /Work sections/ });
  await picker.focus();
  await page.keyboard.press("Enter");
  const menu = page.getByRole("menu", { name: "Work sections" });
  await expect(menu.getByRole("menuitemradio")).toHaveCount(9);
  await page.keyboard.press("Escape");
  await expect(picker).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(menu).toBeVisible();
  await page.keyboard.press("Home");
  await expect(menu.getByRole("menuitemradio").first()).toBeFocused();
  for (let i = 1; i <= 4; i++) {
    await page.keyboard.press("ArrowDown");
    await expect(menu.getByRole("menuitemradio").nth(i)).toBeFocused();
  }
  await expect(menu.getByRole("menuitemradio", { name: /Tasks/ })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(picker).toContainText("Tasks");
  await picker.click();
  await expect(menu.getByRole("menuitemradio", { name: "Tasks", exact: true })).toHaveAttribute("aria-checked", "true");
  await expect(menu.locator(".pl-count .pl-count")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(picker).toBeFocused();
}

async function openRow(work: Locator) {
  const row = work.locator(".work-row").nth(25);
  await row.click();
  await expect(work.locator(".work-view__detail h2")).toBeFocused();
}

for (const theme of ["light", "dark"] as const) {
  test.describe(theme, () => {
    test.use({ colorScheme: theme, hasTouch: true });
    test.beforeEach(async ({ page }) => {
      await page.route("**/api/theme", route => route.fulfill({ json: { theme: { mode: theme, overrides: {} } } }));
      await page.route("**/api/tasks/issues**", route => route.fulfill({ json: {
        issues: Array.from({ length: 35 }, (_, i) => ({
          id: `layout-${i}`, title: `Layout task ${i}`, status: "in_progress",
          priority: 1, issue_type: "task", created_at: "2026-06-02T09:00:00Z",
        })),
      } }));
    });

    for (const width of [390, 560]) {
      test(`phone ${width} uses progressive panes`, async ({ page }, info) => {
        await page.setViewportSize({ width, height: 844 });
        await page.goto("/app/");
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
        await page.getByRole("button", { name: "Open Work", exact: true }).click();
        const work = page.locator(".work-view:visible");
        await selectTasks(page, work);
        await expect(work.locator(".work-view__detail")).toBeHidden();
        for (const name of ["This chat", "All"]) {
          const box = await work.getByRole("button", { name, exact: true }).boundingBox();
          expect(box!.width).toBeGreaterThanOrEqual(44);
          expect(box!.height).toBeGreaterThanOrEqual(44);
        }
        await expect(work.locator(".pl-count .pl-count")).toHaveCount(0);
        const listScreenshot = info.outputPath(`phone-list-${width}-${theme}.png`);
        await page.screenshot({ path: listScreenshot });
        await info.attach("Phone list", { path: listScreenshot, contentType: "image/png" });
        await openRow(work);
        await expect(work.locator(".work-view__list")).toBeHidden();
        await expect(work.locator(".work-view__picker")).toBeHidden();
        const screenshot = info.outputPath(`phone-${width}-${theme}.png`);
        await page.screenshot({ path: screenshot });
        await info.attach("Phone", { path: screenshot, contentType: "image/png" });
        await work.getByRole("button", { name: "Back to Tasks" }).click();
        await expect(work.locator(".work-row").nth(25)).toBeFocused();
        await expect(work.locator(".work-view__list")).toBeVisible();
        await work.getByRole("button", { name: "All", exact: true }).click();
        await expect(work.getByRole("button", { name: "All", exact: true })).toHaveAttribute("aria-pressed", "true");
      });
    }

    test("rail, Expand, modal container resize and Dock preserve selection and scroll", async ({ page }, info) => {
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.goto("/app/");
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      let work = page.locator(".work-view:visible");
      await selectTasks(page, work);
      const listScreenshot = info.outputPath(`rail-list-${theme}.png`);
      await page.screenshot({ path: listScreenshot });
      await info.attach("Rail list", { path: listScreenshot, contentType: "image/png" });
      await openRow(work);
      await expect(work.locator(".work-view__list")).toBeHidden();
      const railScreenshot = info.outputPath(`rail-${theme}.png`);
      await page.screenshot({ path: railScreenshot });
      await info.attach("Rail", { path: railScreenshot, contentType: "image/png" });
      await page.getByRole("button", { name: "Expand", exact: true }).last().click();
      work = page.getByRole("dialog", { name: /^Work/ }).locator(".work-view");
      await expect(work.locator(".work-view__list")).toBeVisible();
      await expect(work.locator(".work-view__detail")).toBeVisible();
      await expect(work.locator(".work-row").nth(25)).toHaveAttribute("aria-current", "true");
      const scroll = await work.locator(".work-view__list").evaluate(el => el.scrollTop);
      expect(scroll).toBeGreaterThan(0);
      const modalScreenshot = info.outputPath(`wide-modal-${theme}.png`);
      await page.screenshot({ path: modalScreenshot });
      await info.attach("Wide modal", { path: modalScreenshot, contentType: "image/png" });
      // Change only the Work container, leaving the desktop viewport wide.
      await work.evaluate(el => { el.style.width = "767px"; });
      await expect(work.locator(".work-view__list")).toBeHidden();
      await expect(work.locator(".work-view__detail")).toBeVisible();
      await work.evaluate(el => { el.style.width = "768px"; });
      await expect(work.locator(".work-view__list")).toBeVisible();
      await expect.poll(() => work.locator(".work-view__list").evaluate(el => el.scrollTop)).toBe(scroll);
      await expect(work.getByRole("button", { name: "This chat", exact: true })).toHaveAttribute("aria-pressed", "true");
      await page.getByRole("dialog", { name: /^Work/ }).getByLabel("Close", { exact: true }).click();
      await page.getByRole("button", { name: "Expand", exact: true }).last().click();
      await expect(work.locator(".work-view__detail h2")).toHaveText("Layout task 25");
      await expect.poll(() => work.locator(".work-view__list").evaluate(el => el.scrollTop)).toBe(scroll);
      await page.getByRole("button", { name: "Dock to rail" }).click();
      work = page.locator(".work-view:visible");
      await expect(work.locator(".work-view__detail h2")).toBeFocused();
      await expect(work.locator(".work-view__list")).toBeHidden();
      await work.getByRole("button", { name: "Back to Tasks" }).click();
      await expect(work.locator(".work-row").nth(25)).toBeFocused();
      await expect.poll(() => work.locator(".work-view__list").evaluate(el => el.scrollTop)).toBe(scroll);
    });
  });
}
