import { expect, test } from "@playwright/test";

// The Tasks panel (Work hub → Tasks) creates a task from a dialog (the always-visible
// inline form was replaced) — title + type + priority + description, gated on a title.

async function openTasks(page) {
  await page.goto("/app/", { waitUntil: "load" });
  const workBtn = page.locator(".pl-rail--right").getByRole("button", { name: "Work", exact: true });
  const cls = (await workBtn.getAttribute("class")) ?? "";
  if (!cls.includes("--active")) await workBtn.click();
  await page.locator(".pl-tabs__select").first().selectOption("tasks");
}

test("New task opens a dialog; submit is gated on a title, then creates", async ({ page }) => {
  await openTasks(page);
  await page.getByTestId("task-new").click();

  const dialog = page.getByRole("dialog", { name: "New task" });
  await expect(dialog).toBeVisible();
  await expect(page.getByTestId("task-create-dialog")).toBeVisible();

  // No title yet → Create is disabled.
  await expect(page.getByTestId("task-create-submit")).toBeDisabled();
  await page.getByTestId("task-create-title").fill("Ship the tasks dialog");
  await expect(page.getByTestId("task-create-submit")).toBeEnabled();

  // Full form (description optional) → create → the dialog closes.
  await page.getByTestId("task-create-description").fill("convert the inline form to a dialog");
  await page.getByTestId("task-create-submit").click();
  await expect(dialog).toBeHidden();
});

test("Cancel and Escape close the New-task dialog without creating", async ({ page }) => {
  await openTasks(page);
  await page.getByTestId("task-new").click();
  const dialog = page.getByRole("dialog", { name: "New task" });
  await expect(dialog).toBeVisible();

  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();

  await page.getByTestId("task-new").click();
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});
