import { expect, test, type Page } from "@playwright/test";

// The consumer always supplies onRailReorder, so these exercise draggable rails.
// DS owns the equivalent raw-hook coverage for plain SurfaceRail rendering.
const rail = (page: Page) => page.getByRole("complementary", { name: "Right surfaces", exact: true });
const workView = (page: Page, frame: "rail" | "modal") =>
  page.locator(`.work-view[data-frame="${frame}"]`);

for (const initial of ["active", "inactive", "collapsed"] as const) {
  test(`double-clicking ${initial} Work opens one modal and docks saved section, item and scroll`, async ({ page }) => {
    const now = new Date().toISOString();
    await page.route("**/api/tasks/issues*", (route) => route.fulfill({ json: {
      issues: Array.from({ length: 40 }, (_, i) => ({
        id: `work-${i}`, title: `Work item ${String(i).padStart(2, "0")}`,
        status: "open", issue_type: "task", priority: 2, created_at: now,
      })),
    } }));
    await page.goto("/app/", { waitUntil: "load" });
    const docked = workView(page, "rail");
    await docked.getByRole("button", { name: "Overview Work sections", exact: true }).click();
    await page.getByRole("menuitemradio", { name: "Tasks", exact: true }).click();
    await expect(docked.locator(".work-row")).toHaveCount(40);
    await docked.locator(".work-view__list").evaluate((el) => { el.scrollTop = 300; });
    await expect.poll(() => docked.locator(".work-view__list").evaluate((el) => el.scrollTop)).toBe(300);
    const scroll = await docked.locator(".work-view__list").evaluate((el) => el.scrollTop);
    expect(scroll).toBeGreaterThan(0);
    await docked.locator(".work-row", { hasText: "Work item 12" }).click();
    await expect(docked.getByRole("heading", { name: "Work item 12", exact: true })).toBeVisible();
    const work = rail(page).getByRole("button", { name: "Work", exact: true });
    if (initial === "inactive") {
      await rail(page).getByRole("button", { name: "Scratch", exact: true }).click();
      await expect(page.getByRole("region", { name: "Scratch view", exact: true })).toBeVisible();
    } else if (initial === "collapsed") {
      await work.click();
      await expect(docked).not.toBeVisible();
    }
    // Native browser dblclick includes both preceding clicks; do not dispatch a lone event.
    await work.dblclick();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toHaveCount(1);
    await expect(dialog.getByRole("heading", { name: "Work", exact: true })).toBeVisible();
    await expect(workView(page, "modal").getByRole("tab", { name: /Tasks/ })).toHaveAttribute("aria-selected", "true");
    await expect(dialog.getByRole("heading", { name: "Work item 12", exact: true })).toBeFocused();
    await dialog.getByRole("button", { name: "Dock to rail", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(docked.getByRole("heading", { name: "Work item 12", exact: true })).toBeFocused();
    await docked.getByRole("button", { name: "Back to Tasks", exact: true }).click();
    await expect.poll(() => docked.locator(".work-view__list").evaluate((el) => el.scrollTop)).toBe(scroll);
    await expect(docked.locator(".work-row", { hasText: "Work item 12" })).toBeVisible();
  });
}

for (const active of [false, true]) {
  test(`double-clicking ${active ? "active" : "inactive"} plugin opens the clicked plugin only`, async ({ page }) => {
    await page.goto("/app/", { waitUntil: "load" });
    await expect(page.getByRole("region", { name: "Work view", exact: true })).toBeVisible();
    const scratch = rail(page).getByRole("button", { name: "Scratch", exact: true });
    if (active) await scratch.click();
    await scratch.dblclick();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toHaveCount(1);
    await expect(dialog.getByRole("heading", { name: "Scratch", exact: true })).toBeVisible();
    await expect(dialog.locator(".plugin-view-frame")).toHaveAttribute("src", /\/plugins\/boardy\/scratch/);
    await expect(page.locator(".plugin-view-frame")).toHaveCount(1);
    await dialog.getByRole("button", { name: "Dock to rail", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("region", { name: "Scratch view", exact: true })).toBeVisible();
    await expect(page.locator(".plugin-view-frame")).toHaveCount(1);
  });
}

test("single-click toggles and keyboard drag cancellation and Expand remain available", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const host = page.getByRole("region", { name: "Work view", exact: true });
  const work = rail(page).getByRole("button", { name: "Work", exact: true });
  await expect(host).toBeVisible();
  await work.click();
  await expect(host).not.toBeVisible();
  await work.click();
  await expect(host).toBeVisible();
  await work.focus();
  await page.keyboard.press("Enter");
  // Enter starts the existing keyboard drag on sortable rails; Escape cancels it.
  await page.keyboard.press("Escape");
  await expect(host).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await host.focus();
  await page.keyboard.press("Shift+F10");
  await expect(page.getByRole("menuitem", { name: "Open in large window", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await expect(page.getByRole("dialog").getByRole("heading", { name: "Work", exact: true })).toBeVisible();
});

for (const side of ["left", "right", "bottom"] as const) {
  test(`double-click leaves ${side === "right" ? "pinned Chat" : `${side} rail`} semantics unchanged`, async ({ page }) => {
    await page.addInitScript((side) => {
      const railOrder: Record<string, string[]> = { left: [], right: [], bottom: [], hidden: [] };
      railOrder[side].push(side === "right" ? "chat" : "work");
      if (side !== "right") railOrder.left.push("chat");
      localStorage.setItem("protoagent.ui", JSON.stringify({ version: 14, state: {
        railOrder, surface: side === "left" ? "work" : "chat", rightPanel: "chat",
        bottomPanel: "work", leftCollapsed: false, rightCollapsed: false,
        bottomCollapsed: false, bottomHeight: 400,
      } }));
    }, side);
    await page.goto("/app/", { waitUntil: "load" });
    const item = page.getByRole("complementary", { name: `${side[0].toUpperCase()}${side.slice(1)} surfaces`, exact: true })
      .getByRole("button", { name: side === "right" ? "Chat" : "Work", exact: true });
    await item.dblclick();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    if (side === "right") await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
    else await expect(workView(page, "rail")).toBeVisible();
  });
}
