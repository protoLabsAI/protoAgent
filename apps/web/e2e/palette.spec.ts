import { expect, test } from "@playwright/test";

// The ranked, host-owned palette root (ADR 0057). Nothing asserted ranking, recents, or
// root MEMBERSHIP before this spec — which is how the console shipped a palette that could
// not find two of its own rail surfaces: typing "memory" or "knowledge" rendered "No
// matches", because those surfaces were registered only inside the `Open` submorph's
// private command list and never at the root.
//
// The contract these pin, end to end:
//   • empty query  -> a SHORT list: recents first, then the curated root. Capped.
//   • typed query  -> the FULL corpus, every surface included, ranked, UNCAPPED.
// Both halves matter: dumping the surfaces at the root would fix the search and flood the
// open palette, which is the trade the split exists to avoid.

const PANEL = ".pl-cmdk__panel";
const ROW = ".pl-cmdk-commands__item";
const EMPTY_CAP = 9;

async function openPalette(page: import("@playwright/test").Page) {
  // Boot readiness is asynchronous; wait until the workspace has mounted its keybindings.
  await expect(page.locator(".app-shell-main")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await expect(page.locator(PANEL)).toBeVisible();
  return page.locator(`${PANEL} .pl-cmdk-commands__input`);
}

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
});

test("empty query: a short root list that does NOT dump every surface into it", async ({ page }) => {
  await openPalette(page);
  const rows = page.locator(`${PANEL} ${ROW}`);
  const count = await rows.count();
  expect(count).toBeGreaterThan(0);
  expect(count).toBeLessThanOrEqual(EMPTY_CAP);

  // The rail surfaces are searchable but NOT root members — `matchCommand` returns true for
  // an empty query, so registering them at the root would list every one of them here.
  await expect(page.getByRole("option", { name: "Memory", exact: true })).toHaveCount(0);
  await expect(page.getByRole("option", { name: "Knowledge", exact: true })).toHaveCount(0);
  // What IS at the root: the command-driven list.
  await expect(page.getByRole("option", { name: "Fleet Room" })).toBeVisible();
  await expect(page.getByRole("option", { name: "Open…" })).toBeVisible();
});

test("typing 'memory' finds the Memory surface — the defect this view exists to fix", async ({ page }) => {
  const input = await openPalette(page);
  await input.fill("memory");
  const row = page.getByRole("option", { name: "Memory", exact: true });
  await expect(row).toBeVisible();
  await row.click();
  // It really navigates — a findable row that doesn't open the surface is no fix at all.
  await expect(page.getByTestId("memory-surface")).toBeVisible();
});

test("running a command teaches the empty list — it leads with recents next time", async ({ page }) => {
  const input = await openPalette(page);
  await input.fill("knowledge");
  await page.getByRole("option", { name: "Knowledge", exact: true }).click();
  await expect(page.locator(PANEL)).toHaveCount(0);

  // Reopen: the surface just used leads the list, under the Recent header. Nothing recorded
  // command usage at all before this PR, so this is the write side as much as the read.
  await openPalette(page);
  const first = page.locator(`${PANEL} ${ROW}`).first();
  await expect(first).toContainText("Knowledge");
  await expect(page.locator(`${PANEL} .pl-cmdk-commands__group`).first()).toHaveText("Recent");
});

test("ranking: a label match leads, and keyword-only rows stay listed under it", async ({ page }) => {
  const input = await openPalette(page);
  await input.fill("chat");
  // Three rows match "chat": the Chat SURFACE by label, and both agent rows by keyword.
  // The surface leads (exact label beats an incidental keyword hit) — before ranking, order
  // was registration order, so whichever happened to be registered first won.
  await expect(page.locator(`${PANEL} ${ROW}`).first()).toContainText("Chat");
  await expect(page.getByRole("option", { name: "Fleet Room" })).toBeVisible();

  // A prefix match sorts under an exact one, and both sort above metadata matches.
  await input.fill("settings");
  await expect(page.locator(`${PANEL} ${ROW}`).first()).toHaveText(/^Settings/);
  await expect(page.getByRole("option", { name: "Settings: Fleet" })).toBeVisible();
});

test("a keyword-only hit still surfaces — ranking reorders, it never filters", async ({ page }) => {
  const input = await openPalette(page);
  // "box" appears in NO label — only on the Box deep-links' keywords. A label-first
  // ranking that dropped keyword matches would lose these rows entirely (and would red
  // fleet.spec.ts, where every member name rides the Fleet Room command's keywords).
  await input.fill("box");
  await expect(page.getByRole("option", { name: "Settings: Fleet" })).toBeVisible();
  await expect(page.getByRole("option", { name: "Settings: Telemetry" })).toBeVisible();

  await input.fill("ava"); // a live fleet member's name, carried as a keyword
  await expect(page.getByRole("option", { name: "Fleet Room" })).toBeVisible();
});

test("the query path is UNCAPPED — the cap belongs to the empty list alone", async ({ page }) => {
  const input = await openPalette(page);
  const rows = page.locator(`${PANEL} ${ROW}`);
  const rootCount = await rows.count();
  // "e" matches nearly everything; the corpus (root commands + every surface) is larger
  // than the empty-list cap, so a cap on the query path would be visible right here.
  await input.fill("e");
  expect(await rows.count()).toBeGreaterThan(rootCount);
});
