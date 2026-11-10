import { expect, test, type Locator, type Page } from "@playwright/test";

// Per-browser operator API fixtures, like the other mock-server specs. Mutations
// change the next GET, so the Undo assertion requires a real query refresh.
test.beforeEach(async ({ page }) => {
  const now = new Date().toISOString();
  const tasks = Array.from({ length: 40 }, (_, i) => ({
    id: `work-${i}`, title: `Work item ${String(i).padStart(2, "0")}`,
    status: ["in_progress", "blocked", "open", "closed"][i % 4],
    issue_type: "task", priority: 2, created_at: now, closed_at: now,
  }));
  let deleted: (typeof tasks)[number] | undefined;
  await page.route("**/api/tasks/issues*", (route) => route.fulfill({ json: { issues: tasks } }));
  await page.route("**/api/tasks/issues/*", async (route) => {
    expect(route.request().method()).toBe("DELETE");
    const id = new URL(route.request().url()).pathname.split("/").pop();
    const index = tasks.findIndex((task) => task.id === id);
    expect(index).toBeGreaterThanOrEqual(0);
    [deleted] = tasks.splice(index, 1);
    await route.fulfill({ json: { deleted: id, undo_token: "work-undo" } });
  });
  await page.route("**/api/undo/work-undo", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(deleted).toBeDefined();
    tasks.push(deleted!);
    deleted = undefined;
    await route.fulfill({ json: { restored: true, kind: "task" } });
  });
  await page.route("**/api/decisions?*", (route) => route.fulfill({ json: {
    pending: 1, decisions: [{
      id: "work-decision", question: "Ship the Work view?", state: "pending",
      session_id: "", agent: "protoAgent", created_at: now,
      options: [{ id: "yes", label: "Ship it" }], context: "", default_action_text: "Wait",
    }],
  } }));
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

const railView = (page: Page) => page.locator('.work-view[data-frame="rail"]');
const modalView = (page: Page) => page.locator('.work-view[data-frame="modal"]');
const list = (view: Locator) => view.locator(".work-view__list");
const row = (view: Locator) => view.locator(".work-row").filter({ hasText: "Work item 12" });

async function expandWork(page: Page) {
  await page.getByRole("region", { name: "Work view", exact: true }).focus();
  await page.keyboard.press("Shift+F10");
  await page.getByRole("menuitem", { name: "Open in large window", exact: true }).click();
}

async function openTasks(page: Page) {
  await railView(page).getByRole("button", { name: "Overview Work sections", exact: true }).click();
  await page.getByRole("menuitemradio", { name: "Tasks", exact: true }).click();
  await expect(railView(page).locator(".work-row")).toHaveCount(40);
}

async function expectScroll(view: Locator, value: number) {
  await expect.poll(() => list(view).evaluate((el) => el.scrollTop)).toBe(value);
}

test("rail → Expand → item → Dock preserves the section, scroll and item", async ({ page }) => {
  // Work starts open; close it first to exercise opening it through the rail.
  const work = page.locator(".pl-rail--right").getByRole("button", { name: "Work", exact: true });
  await work.click();
  await expect(railView(page)).not.toBeVisible();
  await work.click();
  for (const lane of ["Needs you", "Doing", "Waiting on", "Next", "Done"]) {
    await expect(railView(page).getByRole("region", { name: lane, exact: true })).toBeVisible();
  }
  await openTasks(page);
  await list(railView(page)).evaluate((el) => { el.scrollTop = 300; });
  await expectScroll(railView(page), 300);
  await expandWork(page);
  await expect(modalView(page).getByRole("button", { name: /Work sections/ })).toHaveAccessibleName("Tasks Work sections");
  await expectScroll(modalView(page), 300);
  await row(modalView(page)).click();
  await expect(modalView(page).getByRole("heading", { name: "Work item 12", exact: true })).toBeVisible();
  const scroll = await list(modalView(page)).evaluate((el) => el.scrollTop);
  expect(scroll).toBeGreaterThan(0);
  await page.getByRole("button", { name: "Dock to rail", exact: true }).click();
  await expect(modalView(page)).toHaveCount(0);
  await expect(railView(page).getByRole("heading", { name: "Work item 12", exact: true })).toBeFocused();
  await railView(page).getByRole("button", { name: "Back to Tasks", exact: true }).click();
  await expectScroll(railView(page), scroll);
  await expect(row(railView(page))).toBeVisible();
  await row(railView(page)).click();
  await expandWork(page);
  await expect(modalView(page).getByRole("heading", { name: "Work item 12", exact: true })).toBeFocused();
  await expect(modalView(page).getByRole("button", { name: /Work sections/ })).toHaveAccessibleName("Tasks Work sections");
});

test("modal traps keyboard focus in both directions and Escape closes it", async ({ page }) => {
  await expandWork(page);
  const dialog = page.getByRole("dialog").filter({ has: modalView(page) });
  const first = dialog.getByRole("button", { name: "Dock to rail", exact: true });
  await first.focus();
  // Walk a complete cycle, checking every intermediate stop (an endpoint alone
  // misses focus escaping into the chat before eventually returning).
  let wrapped = false;
  for (let i = 0; i < 100; i++) {
    await page.keyboard.press("Tab");
    expect(await dialog.evaluate((el) => el.contains(document.activeElement))).toBe(true);
    if (await first.evaluate((el) => el === document.activeElement)) { wrapped = true; break; }
  }
  expect(wrapped).toBe(true);
  await page.keyboard.press("Shift+Tab");
  expect(await dialog.evaluate((el) => el.contains(document.activeElement))).toBe(true);
  await expect(first).not.toBeFocused();
  await page.keyboard.press("Tab");
  await expect(first).toBeFocused();
  // Move off Dock's tooltip and wait for its exit animation to unmount it.
  // Focus moving to Close alone does not mean Radix has removed the tooltip's
  // Escape listener: that transient layer can still consume the next Escape.
  await page.mouse.move(0, 0);
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close", exact: true })).toBeFocused();
  await expect(page.getByRole("tooltip", { includeHidden: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(railView(page)).toBeVisible();
});

test("task delete confirms inline and Undo restores the row", async ({ page }) => {
  await openTasks(page);
  await expandWork(page);
  await row(modalView(page)).click();
  await modalView(page).getByRole("button", { name: "Delete…", exact: true }).click();
  const confirm = modalView(page).getByRole("group", { name: "Delete this task?" });
  await expect(confirm).toBeVisible();
  await expect(confirm.getByRole("button", { name: "Keep it" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await expect(row(modalView(page))).toHaveCount(1);
  await confirm.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(row(modalView(page))).toHaveCount(0);
  const toast = page.locator(".pl-toast", { hasText: "Task deleted." });
  await expect(toast).toBeVisible();
  await toast.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(row(modalView(page))).toHaveCount(1);
  await expect(modalView(page).getByRole("heading", { name: "Work item 12", exact: true })).toBeVisible();
});

test("Decisions pill opens Decisions; Open Work resets to Overview", async ({ page }) => {
  await page.getByTestId("decisions-widget").click();
  await expect(modalView(page).getByRole("button", { name: /Work sections/ })).toHaveAccessibleName("Decisions Work sections");
  await expect(modalView(page).locator(".work-row", { hasText: "Ship the Work view?" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await page.locator(".pl-cmdk-commands__input").fill("Open Work");
  await page.getByRole("option", { name: "Open Work", exact: true }).click();
  await expect(modalView(page).getByRole("button", { name: /Work sections/ })).toHaveAccessibleName("Overview Work sections");
  await expect(modalView(page).getByRole("region", { name: "Needs you", exact: true })).toBeVisible();
});
