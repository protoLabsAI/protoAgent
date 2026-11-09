import { expect, test, type Page } from "@playwright/test";

// Retain the chunk curation regressions inside the new document accordion.
test.describe.configure({ mode: "serial" });
test.beforeEach(async ({ page }) => {
  await page.request.post("/api/__test__/knowledge/reset");
  await page.route("**/api/knowledge/collections", route => route.fulfill({ json: { enabled: true, collections: [
    { collection: "general", chunks: 5, sources: 3, last_ingested_at: "2026-06-04" },
  ] } }));
});
async function open(page: Page) {
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Knowledge", exact: true }).click();
  const surface = page.getByTestId("knowledge-store");
  await surface.getByRole("link", { name: "general", exact: true }).click();
  return surface;
}
test("Knowledge opens on collections and documents replace source groups", async ({ page }) => {
  const surface = await open(page);
  await expect(surface.getByRole("heading", { name: "general" })).toBeVisible();
  const header = surface.getByRole("button", { name: /^Hiking with Kevin/ });
  await expect(header).toContainText("3 chunks");
  await expect(header).toHaveAttribute("aria-expanded", "false");
  await header.click();
  await expect(surface.getByText("Switchbacks keep the grade walkable.")).toBeVisible();
  await header.click();
  await expect(surface.getByText("Switchbacks keep the grade walkable.")).toHaveCount(0);
  await expect(surface.getByPlaceholder("Search in general…")).toBeVisible();
  await expect(surface.getByLabel("pending review filter")).toHaveCount(0);
});
test("layered chunks retain share and unshare actions", async ({ page }) => {
  const surface = await open(page);
  await surface.getByRole("button", { name: /^daily-log/ }).click();
  await surface.getByRole("button", { name: /^Entries without a source/ }).click();
  await expect(surface.getByLabel("share entry 12", { exact: true })).toBeVisible();
  await expect(surface.getByLabel("unshare entry 11", { exact: true })).toBeVisible();
  await expect(surface.getByLabel("share entry 11", { exact: true })).toHaveCount(0);
  await surface.getByLabel("unshare entry 11", { exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Unshare from the commons?" });
  await dialog.getByRole("button", { name: "Unshare", exact: true }).click();
  await expect(surface.getByLabel("unshare entry 11", { exact: true })).toHaveCount(0);
});
test("plain chunk delete confirms and Shift+click still deletes immediately", async ({ page }) => {
  const surface = await open(page);
  await surface.getByRole("button", { name: /^Entries without a source/ }).click();
  const del = surface.getByLabel("delete entry 12", { exact: true });
  await del.click();
  const dialog = page.getByRole("dialog", { name: "Delete this knowledge entry?" });
  await expect(dialog).toBeVisible(); await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0); await expect(del).toBeVisible();
  await del.click({ modifiers: ["Shift"] });
  await expect(del).toHaveCount(0); await expect(dialog).toHaveCount(0);
});
test("add-entry collection picker is prefilled and accepts a new collection", async ({ page }) => {
  const surface = await open(page);
  await surface.getByTitle("Add a knowledge entry", { exact: true }).click();
  const picker = page.getByRole("combobox", { name: "Collection", exact: true });
  await expect(picker).toHaveValue("general");
  await picker.fill("New collection"); await expect(picker).toHaveValue("New collection");
});
