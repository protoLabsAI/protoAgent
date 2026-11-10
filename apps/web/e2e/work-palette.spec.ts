import { expect, test } from "@playwright/test";

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
    await expect(page.getByRole("button", { name: "Goals Work sections", exact: true })).toBeVisible();
    await expect(page.getByTestId("goal-create-dialog")).toHaveCount(0);
  });
}
