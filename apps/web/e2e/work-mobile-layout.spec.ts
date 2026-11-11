import { expect, test, type Locator, type Page } from "@playwright/test";

// Opens Work in the window from the palette ("Open in window" is off in the context menu for
// now — see apps/web/src/contextMenu/registrations.tsx). `label` picks the row: "Open Work"
// (Overview) or "Work: <Section>".
async function openWorkWindow(page: Page, label = "Open Work") {
  await expect(page.getByTestId("header-menu")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await page.locator(".pl-cmdk-commands__input").fill(label);
  await page.getByRole("option", { name: label, exact: true }).click();
  await expect(page.getByRole("dialog", { name: /^Work/ })).toBeVisible();
}

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
  await expect(picker).toBeFocused();
  await picker.click();
  await expect(menu.getByRole("menuitemradio", { name: "Tasks", exact: true })).toHaveAttribute("aria-checked", "true");
  await expect(menu.locator(".pl-count .pl-count")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(picker).toBeFocused();
}

/** scrollTop of whatever actually scrolls the Work list: the list in the rail and modal, the
 *  Drawer body in the phone sheet (where the list grows to its content). */
function scrollerTop(work: Locator) {
  return work.locator(".work-row").nth(25).evaluate((row) => {
    for (let n: HTMLElement | null = row.parentElement; n; n = n.parentElement) {
      const o = getComputedStyle(n).overflowY;
      if ((o === "auto" || o === "scroll") && n.scrollHeight > n.clientHeight + 1) return n.scrollTop;
    }
    return 0;
  });
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

    // One test per width: as a single test the three widths x nine sections ran ~300 keyboard
    // steps and blew the 30 s timeout under WebKit about half the time.
    for (const width of [390, 768, 1000]) {
      test(`collapsed picker selects every section without a width cap at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.goto("/app/");
        await openWorkWindow(page);
        const work = page.getByRole("dialog", { name: /^Work/ }).locator(".work-view");
        const slot = work.locator(".work-view__picker");
        const picker = slot.getByRole("button", { name: /Work sections/ });
        const menu = page.getByRole("menu", { name: "Work sections" });
        await work.evaluate((el, value) => { el.style.width = `${value}px`; }, width);
        await expect(slot).toHaveCSS("max-width", "none");
        if (width >= 768) expect((await slot.boundingBox())!.width).toBeGreaterThan(240);
        await expect(picker).toBeVisible();
        await expect(work.getByRole("tablist", { name: "Work sections" })).toHaveCount(0);
        await expect.poll(() => work.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
        await selectTasks(page, work);
        for (const label of ["Overview", "Decisions", "Goals", "Watches", "Tasks", "Schedules", "Background agents", "Activity", "Directives"]) {
          await picker.press("Space");
          const item = menu.getByRole("menuitemradio", { name: label, exact: true });
          await expect(item.locator(".pl-menu__badge")).toHaveCount(1);
          await expect(item.locator(".pl-menu__badge .pl-count")).toHaveCount(0);
          await expect(item.locator(".pl-menu__badge")).toHaveText(/^\d+$/);
          if (label === "Tasks") await expect(item.locator(".pl-menu__badge")).toHaveText("35");
          await item.focus();
          await page.keyboard.press("Enter");
          await expect(picker).toHaveAccessibleName(`${label} Work sections`);
          await expect(picker).toBeFocused();
          await picker.press("Enter");
          await expect(item).toHaveAttribute("aria-checked", "true");
          await page.keyboard.press("Escape");
          await expect(picker).toBeFocused();
          // Switching sections leaves the scope alone (This chat, the default in every frame).
          await expect(work.getByRole("button", { name: "This chat", exact: true })).toHaveAttribute("aria-pressed", "true");
        }
      });
    }

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
        await work.locator(".work-row").nth(25).scrollIntoViewIfNeeded();
        const listScroll = await scrollerTop(work);
        expect(listScroll).toBeGreaterThan(0);
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
        // Back keeps the list where it was: the row is on screen, not just focused off-screen.
        await expect.poll(() => scrollerTop(work)).toBe(listScroll);
        await expect(work.locator(".work-row").nth(25)).toBeInViewport();
        await work.getByRole("button", { name: "All", exact: true }).click();
        await expect(work.getByRole("button", { name: "All", exact: true })).toHaveAttribute("aria-pressed", "true");
      });
    }

    test("window, container resize and Dock preserve selection and scroll", async ({ page }, info) => {
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.goto("/app/");
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await openWorkWindow(page, "Work: Tasks");
      let work = page.getByRole("dialog", { name: /^Work/ }).locator(".work-view");
      await expect(work.locator(".work-row")).toHaveCount(35);
      await work.locator(".work-row").nth(25).click();
      await expect(work.locator(".work-view__detail h2")).toHaveText("Layout task 25");
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
