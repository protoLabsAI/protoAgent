import { expect, test } from "@playwright/test";

test("Work search lists one overview entry and opens every section in the modal", async ({ page }) => {
  await page.goto("/app/");
  await expect(page.getByTestId("header-menu")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await page.locator(".pl-cmdk-commands__input").fill("work");
  await expect(page.getByRole("option", { name: "Open Work", exact: true })).toHaveCount(1);
  await expect(page.getByRole("option", { name: "Work", exact: true })).toHaveCount(0);
  // Activity is not a Work section any more (S4c) — no palette row can navigate to it.
  await page.locator(".pl-cmdk-commands__input").fill("Work: Activity");
  await expect(page.getByRole("option", { name: "Work: Activity", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");

  // Legacy palette commands survive as deep links: each resolves into its canonical group
  // (work-consolidation.md v2 §7.2). The Activity row itself is gone (S4c, asserted above).
  for (const [section, groupLabel] of [
    ["Overview", "Overview"], ["Decisions", "Needs you"], ["Goals", "Doing now"], ["Watches", "Coming up"],
    ["Tasks", "Doing now"], ["Schedules", "Coming up"], ["Background agents", "Doing now"],
    ["Directives", "Your instructions"],
  ] as const) {
    const label = section === "Overview" ? "Open Work" : `Work: ${section}`;
    await page.keyboard.press("ControlOrMeta+Shift+k");
    await page.locator(".pl-cmdk-commands__input").fill(label);
    await page.getByRole("option", { name: label, exact: true }).click();
    await expect(page.getByRole("button", { name: "Dock to rail", exact: true })).toBeVisible();
    const picker = page.locator('.work-view[data-frame="modal"]').getByRole("button", { name: /Work sections/ });
    await expect(picker).toHaveAccessibleName(`${groupLabel} Work sections`);
    await picker.click();
    await expect(page.getByRole("menuitemradio", { name: groupLabel, exact: true })).toHaveAttribute("aria-checked", "true");
    await page.keyboard.press("Escape");
    await expect(picker).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "Dock to rail", exact: true })).toHaveCount(0);
  }

  // The Open picker keeps Work even though root search merges its generated row.
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await page.locator(".pl-cmdk-commands__input").fill("Open…");
  await page.getByRole("option", { name: "Open…" }).click();
  await expect(page.getByPlaceholder("Open a surface…")).toBeVisible();
  await expect(page.getByPlaceholder(/Search commands/)).toHaveCount(0);
  await page.getByRole("option", { name: "Open Work", exact: true }).click();
  await expect(page.getByRole("button", { name: "Dock to rail", exact: true })).toBeVisible();
  await expect(page.locator('.work-view[data-frame="modal"]').getByRole("button", { name: /Work sections/ })).toHaveAccessibleName("Overview Work sections");
});

for (const dock of ["left", "right", "bottom"] as const) {
  test(`New goal has one creation dialog when Work is docked ${dock}`, async ({ page }) => {
    await page.addInitScript((dock) => {
      const railOrder = { left: ["chat"], right: [], bottom: [], hidden: [] } as Record<string, string[]>;
      railOrder[dock].push("work");
      localStorage.setItem("protoagent.ui", JSON.stringify({
        version: 14,
        state: {
          railOrder,
          surface: dock === "left" ? "work" : "chat",
          rightPanel: "work",
          bottomPanel: "work",
          leftCollapsed: false,
          rightCollapsed: dock !== "right",
          bottomCollapsed: dock !== "bottom",
          bottomHeight: 400,
        },
      }));
    }, dock);
    await page.goto("/app/");
    await expect(page.locator(".work-view__panes")).toBeVisible();

    await page.keyboard.press("ControlOrMeta+Shift+k");
    await page.locator(".pl-cmdk-commands__input").fill("Work: New goal");
    await page.getByRole("option", { name: "Work: New goal…", exact: true }).click();

    // Count DOM mounts, including any hidden dock copy behind the modal.
    await expect(page.getByTestId("goal-create-dialog")).toHaveCount(1);
    await expect(page.getByTestId("goal-create-dialog")).toBeVisible();
    await expect(page.locator(".work-view__panes")).toHaveCount(1);
    await expect(page.getByRole("button", { name: "Dock to rail", exact: true })).toBeVisible();

    await page.getByTestId("goal-create-dialog").getByRole("button", { name: "Dismiss", exact: true }).click();
    await expect(page.getByTestId("goal-create-dialog")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "Dock to rail", exact: true })).toHaveCount(0);
    // Closing restores the original dock and saved section without reopening creation.
    await expect(page.locator(".work-view__panes")).toBeVisible();
    await expect(page.getByRole("button", { name: "Doing now Work sections", exact: true })).toBeVisible();
    await expect(page.getByTestId("goal-create-dialog")).toHaveCount(0);
  });
}

for (const placement of ["left", "right", "bottom", "hidden"] as const) {
  test(`Dock to rail reveals palette Work when ${placement}`, async ({ page }) => {
    await page.addInitScript((placement) => {
      const railOrder: Record<string, string[]> = {
        left: ["chat"], right: ["knowledge"], bottom: ["memory"], hidden: [],
      };
      railOrder[placement].push("work");
      localStorage.setItem("protoagent.ui", JSON.stringify({
        version: 14,
        state: {
          railOrder, surface: "chat", rightPanel: "knowledge", bottomPanel: "memory",
          leftCollapsed: false, rightCollapsed: true, bottomCollapsed: true,
          rightWidth: 0, bottomHeight: 0,
        },
      }));
    }, placement);
    await page.goto("/app/");
    await expect(page.getByTestId("header-menu")).toBeVisible();
    await page.keyboard.press("ControlOrMeta+Shift+k");
    await page.locator(".pl-cmdk-commands__input").fill("Work: Goals");
    await page.getByRole("option", { name: "Work: Goals", exact: true }).click();
    await expect(page.locator('.work-view[data-frame="modal"]')).toBeVisible();
    await page.getByRole("button", { name: "Dock to rail", exact: true }).click();
    await expect(page.locator('.work-view[data-frame="modal"]')).toHaveCount(0);
    await expect(page.locator(".work-view__panes")).toBeVisible();
    await expect(page.getByRole("button", { name: "Doing now Work sections", exact: true })).toBeVisible();
  });
}
