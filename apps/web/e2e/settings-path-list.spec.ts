import { expect, test } from "@playwright/test";

// A `type: path` setting with `multiple: true` (the data plugin's "Data folders") renders as
// a LIST — one row per folder, each with its own Browse…, plus Remove and Add folder — and
// saves ONE "\n"-joined string, so older cores and the plugin's own comma/newline parser keep
// reading it. Before, Browse… replaced the whole comma-separated value.

async function openDemoConfig(page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("settings-widget").click();
  await page.locator(".pl-sidenav").getByRole("tab", { name: "Plugins", exact: true }).click();
  await page.locator(".plugin-table tbody tr", { hasText: "Demo Plugin" }).getByRole("button", { name: "Configure" }).click();
  const dialog = page.getByRole("dialog", { name: "Demo Plugin" });
  await expect(dialog.locator('.setting-row[data-key="demo.data_dirs"]')).toBeVisible();
  return dialog;
}

test("Data folders: a legacy comma value loads as rows; Add folder browses; save posts a \\n-joined string", async ({ page }) => {
  const dialog = await openDemoConfig(page);
  const row = dialog.locator('.setting-row[data-key="demo.data_dirs"]');
  await expect(row.getByLabel("Data folders 1")).toHaveValue("/home/op/Documents");
  await expect(row.getByLabel("Data folders 2")).toHaveValue("/srv/data");

  // Remove the second folder, then Add one — Browse… opens for the new row straight away.
  await row.getByRole("button", { name: /^Remove folder 2/ }).click();
  await expect(row.getByLabel("Data folders 2")).toHaveCount(0);
  await row.getByRole("button", { name: "Add folder" }).click();
  const picker = page.getByRole("dialog", { name: "Choose a folder" });
  await expect(picker).toBeVisible();
  await picker.getByRole("option", { name: "dev" }).click();
  await expect(picker.locator(".path-browser-cwd")).toHaveText("/home/op/dev");
  await picker.getByRole("button", { name: "Use this folder" }).click();
  await expect(picker).toBeHidden();
  await expect(row.getByLabel("Data folders 2")).toHaveValue("/home/op/dev");

  // A duplicate typed into a third row is dropped on save.
  await row.getByRole("button", { name: "Add folder" }).click();
  await page.getByRole("dialog", { name: "Choose a folder" }).getByRole("button", { name: "Cancel" }).click();
  await row.getByLabel("Data folders 3").fill("/home/op/dev");

  const saved = page.waitForRequest(
    (r) => r.method() === "POST" && new URL(r.url()).pathname.endsWith("/api/settings"),
  );
  await dialog.getByRole("button", { name: /Save & apply/ }).click();
  const req = await saved;
  expect(req.postDataJSON()?.updates?.["demo.data_dirs"]).toBe("/home/op/Documents\n/home/op/dev");
});
