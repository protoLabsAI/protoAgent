import { expect, test, devices, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";

const collections = [
  { collection: "claude-import", chunks: 153, sources: 12, last_ingested_at: "2026-07-08T12:00:00Z" },
  { collection: "release-runbooks", chunks: 64, sources: 9, last_ingested_at: "2026-11-06T12:00:00Z" },
  { collection: "atlas-plugin docs", chunks: 210, sources: 31, last_ingested_at: "2026-11-02T12:00:00Z" },
  { collection: "adr-index", chunks: 402, sources: 128, last_ingested_at: "2026-11-07T12:00:00Z" },
  { collection: "old-ci-notes", chunks: 18, sources: 1, last_ingested_at: "2026-04-08T12:00:00Z" },
];
async function mock(page: Page) {
  await page.route("**/api/events**", route => route.fulfill({ contentType: "text/event-stream", body: ": idle\n\n" }));
  const state = { enabled: true, rows: [...collections], failDelete: false };
  await page.route("**/api/knowledge/collections", route => route.fulfill({ json: { enabled: state.enabled, collections: state.rows } }));
  await page.route("**/api/knowledge/search?*", route => {
    const params = new URL(route.request().url()).searchParams;
    const name = params.get("collection") ?? "claude-import";
    const q = params.get("q") ?? "";
    const row = collections.find(c => c.collection === name)!;
    const results = !state.rows.length || q && !"release safety".includes(q.toLowerCase()) ? [] : Array.from({ length: q ? 2 : row.chunks }, (_, i) => ({
      id: i + 1, heading: `Release safety ${i + 1}`, content: `Check the release checklist before publishing. Verify the build and review the release notes. This is document guidance from ${name}.`,
      preview: "Check the release checklist before publishing.", domain: "legacy-domain", source: `${name}/release-guide-${i % row.sources + 1}.md`,
      source_type: "file", finding_type: null, created_at: row.last_ingested_at, tier: "private", score: 1 / (i + 1),
    }));
    return route.fulfill({ json: { enabled: true, query: q, results, stats: {} } });
  });
  await page.route("**/api/knowledge/delete-by-collection", route => {
    const name = route.request().postDataJSON().collection;
    if (state.failDelete) return route.fulfill({ json: { enabled: true, deleted: 0, error: "Store busy" } });
    state.rows = state.rows.filter(c => c.collection !== name);
    return route.fulfill({ json: { enabled: true, deleted: collections.find(c => c.collection === name)!.chunks } });
  });
  await page.route("**/api/knowledge/restore-by-collection", route => {
    const row = collections.find(c => c.collection === route.request().postDataJSON().collection)!;
    state.rows.push(row);
    return route.fulfill({ json: { enabled: true, restored: row.chunks } });
  });
  await page.route("**/api/knowledge/delete-by-source", route => route.fulfill({ json: { enabled: true, deleted: 13 } }));
  await page.route("**/api/knowledge/restore-by-source", route => route.fulfill({ json: { enabled: true, restored: 13 } }));
  await page.addInitScript(() => localStorage.setItem("protoagent.ui", JSON.stringify({ state: {
    surface: "knowledge", mobileActive: "knowledge", rightCollapsed: true, leftCollapsed: false,
  }, version: 14 })));
  return state;
}
async function openCollection(page: Page, name: string) {
  const surface = page.getByTestId("knowledge-store");
  const link = surface.getByRole("link", { name, exact: true });
  if ((page.viewportSize()?.width ?? 1440) >= 768) await link.click();
  else await surface.locator(".kc-phone-row").filter({ hasText: name }).click();
  await expect(surface.getByRole("heading", { name, exact: true })).toBeVisible();
}
for (const width of [1440, 390]) {
  test.describe(`${width}px`, () => {
    if (width === 390) {
      const { defaultBrowserType: _browser, ...device } = devices["iPhone 13"];
      test.use({ ...device, viewport: { width, height: 844 } });
    } else test.use({ viewport: { width, height: 1000 } });
    for (const mode of ["light", "dark"] as const) {
      test(`${mode}: collection states, search, delete and undo`, async ({ page }) => {
        const state = await mock(page);
        await page.route("**/api/theme", route => route.fulfill({ json: { theme: { mode, overrides: {} } } }));
        await page.goto("/app/");
        const surface = page.getByTestId("knowledge-store");
        await expect(surface).toBeVisible();
        await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
        const directory = path.resolve("e2e/screenshots/knowledge-collections");
        await mkdir(directory, { recursive: true });
        const shot = async (name: string) => {
          await page.screenshot({ path: path.join(directory, `${width}-${mode}-${name}.png`), animations: "disabled", scale: "css" });
        };
        await expect(surface).toContainText("5 collections · 847 chunks");
        await expect(surface.getByRole("progressbar")).toHaveCount(0);
        if (width === 1440) await expect(surface.getByRole("table")).toBeVisible();
        else await expect(surface.getByRole("list", { name: "Knowledge collections" })).toBeVisible();
        await shot("list");
        const listHistoryLength = await page.evaluate(() => history.length);
        await openCollection(page, "claude-import");
        await surface.getByRole("button", { name: "claude-import/release-guide-1.md · 13 chunks", exact: true }).click();
        await expect(surface.getByText("Release safety 1", { exact: true })).toBeVisible();
        await shot("detail");
        // Agent-written rows can still be reviewed from this surface: the filter and the
        // per-row confirm/reject verdict controls stay here until the Memory inspector owns them.
        await expect(surface.getByLabel("pending review filter")).toBeVisible();
        await expect(surface.getByLabel("confirm entry 1", { exact: true })).toBeVisible();
        await surface.getByLabel("edit entry 1", { exact: true }).click();
        // A content edit must keep the chunk's OWN domain, not relabel it to the open collection.
        await expect(surface.getByRole("combobox", { name: "Collection", exact: true })).toHaveValue("legacy-domain");
        await surface.getByRole("button", { name: "Cancel", exact: true }).click();
        // Background App updates used to add duplicate detail entries during this dwell.
        await page.waitForTimeout(1500);
        expect(await page.evaluate(() => history.length)).toBe(listHistoryLength + 1);
        await page.goBack(); await expect(surface).toContainText("5 collections · 847 chunks");
        await page.goForward(); await expect(surface.getByRole("heading", { name: "claude-import", exact: true })).toBeVisible();
        await page.reload(); await expect(surface.getByRole("heading", { name: "claude-import", exact: true })).toBeVisible();
        expect(await page.evaluate(() => history.length)).toBe(listHistoryLength + 1);
        await page.goBack();
        await expect(surface).toContainText("5 collections · 847 chunks");
        await expect(page).not.toHaveURL(/collection=/);
        await page.goForward();
        await expect(surface.getByRole("heading", { name: "claude-import", exact: true })).toBeVisible();
        await expect(page).toHaveURL(/collection=claude-import/);
        await surface.getByRole("button", { name: "claude-import/release-guide-1.md · 13 chunks", exact: true }).click();
        await surface.getByRole("searchbox").fill("release");
        await expect(surface.getByRole("status")).toContainText("2 matches in claude-import");
        await expect(surface.locator("mark").first()).toHaveText(/release/i);
        const contrast = await surface.locator("mark").first().evaluate(el => {
          const style = getComputedStyle(el);
          const luminance = (color: string) => {
            const channels = color.match(/[\d.]+/g)!.slice(0, 3).map(Number).map(n => {
              const v = n / 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4;
            });
            return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
          };
          const a = luminance(style.color), b = luminance(style.backgroundColor);
          return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
        });
        expect(contrast).toBeGreaterThanOrEqual(4.5);
        await shot("search");
        await surface.getByRole("button", { name: "All collections", exact: true }).click();
        await expect(surface.getByRole("status")).toContainText("10 matches in 5 collections");
        await surface.getByRole("searchbox").fill("");
        await expect(surface.getByRole("heading", { name: "Documents", exact: true })).toBeVisible();
        await expect(surface.getByRole("button", { name: "claude-import/release-guide-1.md · 13 chunks", exact: true })).toHaveAttribute("aria-expanded", "true");
        // A whole ingested document can be removed in one action within its collection.
        const docDelete = surface.getByRole("button", { name: "Delete document claude-import/release-guide-1.md", exact: true });
        await expect(docDelete).toBeVisible();
        await docDelete.click();
        const docDialog = page.getByRole("dialog", { name: "Delete this document?" });
        await expect(docDialog).toContainText("claude-import/release-guide-1.md");
        await expect(docDialog).toContainText("from “claude-import”");
        await docDialog.getByRole("button", { name: "Keep it", exact: true }).click();
        await expect(docDialog).toHaveCount(0);
        await surface.getByRole("button", { name: "Delete collection…", exact: true }).click();
        const dialog = page.getByRole("dialog", { name: "Delete claude-import?" });
        await expect(dialog).toBeVisible(); await shot("delete-confirm");
        state.failDelete = true;
        await dialog.getByRole("button", { name: "Delete collection", exact: true }).click();
        await expect(dialog).toContainText("Couldn't delete claude-import. Nothing was removed.");
        state.failDelete = false; await dialog.getByRole("button", { name: "Try again" }).click();
        await expect(dialog).toHaveCount(0);
        await expect(page.getByRole("region", { name: "Notifications" })).toContainText("Deleted claude-import (153 chunks).");
        const nextRow = width === 1440 ? surface.getByRole("link", { name: "release-runbooks", exact: true })
          : surface.locator('.kc-phone-row[data-collection-name="release-runbooks"]');
        await expect(nextRow).toBeFocused();
        await shot("undo");
        await page.getByRole("button", { name: "Undo", exact: true }).click();
        await expect(surface).toContainText("5 collections · 847 chunks");
        state.rows = []; await page.reload(); await expect(surface).toContainText("No knowledge yet"); await shot("none");
        state.enabled = false; await page.reload(); await expect(surface).toContainText("Knowledge is off for this agent"); await shot("off");
      });
    }
  });
}

test("document delete and Undo send the original collection after navigation", async ({ page }) => {
  await mock(page);
  await page.goto("/app/");
  await openCollection(page, "claude-import");
  const surface = page.getByTestId("knowledge-store");
  await surface.getByRole("button", { name: "claude-import/release-guide-1.md · 13 chunks", exact: true }).click();
  await surface.getByRole("button", { name: "Delete document claude-import/release-guide-1.md", exact: true }).click();
  const deletion = page.waitForRequest("**/api/knowledge/delete-by-source");
  await page.getByRole("dialog", { name: "Delete this document?" }).getByRole("button", { name: "Delete document", exact: true }).click();
  expect((await deletion).postDataJSON()).toEqual({
    source: "claude-import/release-guide-1.md", collection: "claude-import",
  });
  await surface.getByRole("link", { name: "Back to all collections" }).click();
  await openCollection(page, "release-runbooks");
  const restoration = page.waitForRequest("**/api/knowledge/restore-by-source");
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  expect((await restoration).postDataJSON()).toEqual({
    source: "claude-import/release-guide-1.md", collection: "claude-import",
  });
});

test("a backend without collection listing retains browse, search, edit and delete", async ({ page }) => {
  await mock(page);
  await page.route("**/api/knowledge/collections", route => route.fulfill({ json: { enabled: true, collections: [] } }));
  let entries = [{ id: 701, heading: "Plugin guide", content: "Original plugin guidance", preview: "Original plugin guidance",
    domain: "general", source: "plugin-guide.md", source_type: "file", finding_type: null, created_at: null }];
  const scopes: (string | null)[] = [];
  await page.route("**/api/knowledge/search?*", route => {
    const params = new URL(route.request().url()).searchParams;
    scopes.push(params.get("collection"));
    const q = params.get("q") ?? "";
    return route.fulfill({ json: { enabled: true, query: q, results: entries.filter(c => c.content.includes(q)), stats: {} } });
  });
  await page.route("**/api/knowledge/chunks/701", route => {
    if (route.request().method() === "PUT") {
      entries = [{ ...entries[0], ...route.request().postDataJSON() }];
      return route.fulfill({ json: { enabled: true, id: 701, replaced: true } });
    }
    entries = [];
    return route.fulfill({ json: { enabled: true, deleted: true } });
  });
  await page.goto("/app/?collection=stale");
  const surface = page.getByTestId("knowledge-store");
  await expect(surface.getByText("Original plugin guidance", { exact: true })).toBeVisible();
  await expect(surface.getByText("No knowledge yet")).toHaveCount(0);
  const search = surface.getByRole("searchbox", { name: "Search knowledge" });
  await search.fill("Original");
  await expect(surface.getByRole("status")).toContainText("1 matches");
  await search.fill("missing");
  await expect(surface).toContainText(`Nothing in this agent's knowledge matches "missing"`);
  await search.fill("");
  await surface.getByLabel("edit entry 701", { exact: true }).click();
  await surface.getByLabel("content", { exact: true }).fill("Updated plugin guidance");
  await surface.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(surface.getByText("Updated plugin guidance", { exact: true })).toBeVisible();
  await surface.getByLabel("delete entry 701", { exact: true }).click();
  await page.getByRole("dialog", { name: "Delete this knowledge entry?" }).getByRole("button", { name: "Delete entry", exact: true }).click();
  await expect(surface.getByText("No knowledge yet")).toBeVisible();
  expect(scopes.length).toBeGreaterThan(3);
  expect(scopes.every(scope => scope === null)).toBe(true);
});
