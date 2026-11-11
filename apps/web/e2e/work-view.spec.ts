import { mkdir } from "node:fs/promises";
import { join } from "node:path";
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

async function expectScroll(view: Locator, value: number) {
  await expect.poll(() => list(view).evaluate((el) => el.scrollTop)).toBe(value);
}

test("window → item → Dock preserves the section, scroll and item", async ({ page }) => {
  await openWorkWindow(page, "Work: Tasks");
  await expect(modalView(page).getByRole("button", { name: /Work sections/ })).toHaveAccessibleName("Tasks Work sections");
  await expect(modalView(page).locator(".work-row")).toHaveCount(40);
  await list(modalView(page)).evaluate((el) => { el.scrollTop = 300; });
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
  await expect(row(railView(page))).toBeFocused();
});

test("modal traps keyboard focus in both directions and Escape closes it", async ({ page }) => {
  await openWorkWindow(page);
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
  await openWorkWindow(page, "Work: Tasks");
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


test("selection-only inspector closes before its Dialog and returns focus", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openWorkWindow(page, "Work: Tasks");
  const work = modalView(page);
  const dialog = page.getByRole("dialog", { name: /^Work/ });
  await expect(work.locator(".work-view__detail")).toHaveCount(0);
  const opener = row(work);
  await opener.click();
  await expect(list(work)).toBeVisible();
  await expect(opener).toHaveAttribute("aria-current", "true");
  await expect(work.getByRole("button", { name: "Close details" })).toBeVisible();
  await work.getByRole("button", { name: /Work sections/ }).click();
  await page.keyboard.press("Escape");
  await expect(work.locator(".work-view__detail")).toBeVisible();
  await expect(page.getByRole("menu", { name: "Work sections" })).toHaveCount(0);
  await expect(work.getByRole("button", { name: /Work sections/ })).toBeFocused();
  // Header controls are outside WorkView, but Escape still closes details first.
  await dialog.locator('button[aria-label="Close"]').focus();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await expect(work.locator(".work-view__detail")).toHaveCount(0);
  await expect(opener).toBeFocused();
  await opener.click();
  await work.getByRole("button", { name: "Delete…", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(work.locator(".work-view__detail")).toBeVisible();
  await expect(work.getByRole("button", { name: "Delete…", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(opener).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});


test("approved layout evidence: rail, window, empty section and phone", async ({ page }, info) => {
  const directory = process.env.WORK_SCREENSHOT_DIR ?? info.outputPath("work-layout");
  await mkdir(directory, { recursive: true });
  const capture = async (name: string) => {
    // Dismiss mock-backend notification toasts so they do not obscure the Work header.
    for (const dismiss of await page.getByRole("button", { name: "Dismiss", exact: true }).all()) {
      await dismiss.click();
    }
    const path = join(directory, `${name}.png`);
    await page.screenshot({ path });
    await info.attach(name, { path, contentType: "image/png" });
  };
  await page.setViewportSize({ width: 1440, height: 900 });
  await openWorkWindow(page);
  await expect(modalView(page).getByRole("heading", { name: /^Doing, \d+$/ })).toBeVisible();
  await expect(modalView(page).locator(".work-view__detail")).toHaveCount(0);
  await capture("02-window-overview-1440x900");
  await page.getByRole("button", { name: "Dock to rail" }).click();
  await expect(railView(page).locator(".work-row").first()).toBeVisible();
  await capture("01-rail-overview-1440x900");
  await openWorkWindow(page);
  await modalView(page).locator(".work-row").filter({ hasText: "Work item 00" }).click();
  await expect(modalView(page).getByRole("button", { name: "Close details" })).toBeVisible();
  await capture("03-window-inspector-1440x900");
  await modalView(page).getByRole("button", { name: "Close details" }).click();
  await page.route("**/api/decisions?*", route => route.fulfill({ json: { pending: 0, decisions: [] } }));
  await page.reload();
  await openWorkWindow(page, "Work: Decisions");
  await expect(modalView(page).getByText("No decisions to show")).toBeVisible();
  await expect(modalView(page).locator(".work-empty")).toHaveCSS("align-items", "flex-start");
  await expect(modalView(page).locator(".work-empty")).toHaveCSS("text-align", "start");
  await capture("04-window-empty-decisions-1440x900");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  // Dock above selected Work as the persistent mobile destination. Return to chat.
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page.getByRole("button", { name: "Open Work", exact: true }).click();
  const phone = page.locator('.work-view[data-frame="sheet"]');
  await phone.getByRole("button", { name: /Work sections/ }).click();
  await page.getByRole("menuitemradio", { name: "Tasks", exact: true }).click();
  await expect(phone.locator(".work-row")).toHaveCount(40);
  expect(await phone.locator(".work-row").first().evaluate(el => el.getBoundingClientRect().height)).toBeGreaterThanOrEqual(48);
  await expect.poll(() => phone.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await capture("05-phone-list-390x844");
  await phone.locator(".work-row").first().click();
  await expect(phone.locator(".work-detail__title")).toBeVisible();
  await expect(phone.locator(".work-detail button.pl-btn--sm")).toHaveCount(0);
  await capture("06-phone-detail-390x844");
  // 1440px at 400% zoom has a 360 CSS-pixel layout viewport.
  await page.setViewportSize({ width: 360, height: 225 });
  await expect.poll(() => phone.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
});
