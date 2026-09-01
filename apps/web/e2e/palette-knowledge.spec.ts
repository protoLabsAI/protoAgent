import { expect, test } from "@playwright/test";

// Live knowledge search in the command palette (#3293), in a real browser against the built
// dist — the layer the unit tripwire cannot reach.
//
// Two of these guard failures that a green unit suite reported anyway, because both live in
// the root view that RENDERS a provider's rows rather than in the provider (the host's own
// since #3289, the DS's `commandsView` before it — the defects are the same either way):
//
//   * a row whose `Command.id` collides with another row's is dropped by the view's
//     first-wins dedup, with no chip, no count and no error — so the operator sees a
//     shortlist that is quietly missing a match it should contain;
//   * a provider that declares `getCommands` switches the view's "Searching…" affordance on,
//     so registering one where it cannot search puts a busy indicator in front of a search
//     that never happens.

const PALETTE = ".pl-cmdk__panel";
const OPTION = '[role="option"]';

/** Answer the palette's search with a fixed set of rows, whatever it asks for. Scoped to the
 *  page so it never perturbs the shared mock fixture the Knowledge surface specs read. */
async function stubSearch(page: import("@playwright/test").Page, results: unknown[]) {
  await page.route("**/api/knowledge/search**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ enabled: true, query: "release", results, stats: {} }),
    }),
  );
}

/** Boot the console and open the palette. The chord goes through the keybinding layer
 *  (ADR 0063), which registers on mount — pressing before the shell is up is a no-op. */
async function open(page: import("@playwright/test").Page) {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await expect(page.locator(PALETTE)).toBeVisible();
}

const chunk = (over: Record<string, unknown>) => ({
  id: 1,
  heading: "",
  content: "body text",
  preview: "body text",
  domain: "general",
  source: null,
  source_type: null,
  finding_type: null,
  created_at: null,
  ...over,
});

test("a typed query lists knowledge rows, chipped as store entries", async ({ page }) => {
  await stubSearch(page, [
    chunk({ id: 11, heading: "Postgres tuning", source: "runbook.md" }),
    chunk({ id: 12, heading: "Postgres backups", source: "ops/backups.md" }),
  ]);
  await open(page);
  await page.locator(`${PALETTE} input`).fill("postgres");
  await expect(page.locator(OPTION).filter({ hasText: "Postgres tuning" })).toBeVisible();
  await expect(page.locator(OPTION).filter({ hasText: "Postgres backups" })).toBeVisible();
  // The CHIP is what tells the operator these are store entries, not more commands. Not a
  // group header: the ranked root (#3289) prints none on a typed query, because ranking
  // sorts across groups and a header would re-emit every few rows.
  await expect(
    page.locator(`${OPTION} .pl-cmdk-commands__chip`).filter({ hasText: "Knowledge" }).first(),
  ).toBeVisible();
  // …and the trailing hint is where the entry came from, which is what tells them apart.
  await expect(page.locator(OPTION).filter({ hasText: "runbook.md" })).toBeVisible();
});

test("two results that share a chunk id both stay reachable", async ({ page }) => {
  // Chunk ids are per-BACKEND rowids. On a layered store (ADR 0041) the private and commons
  // DBs each autoincrement from 1 and the fused search de-dups on CONTENT, so one response
  // routinely carries two DIFFERENT chunks numbered the same — and the low ids that collide
  // are exactly the ones both tiers have. A row keyed on that number loses one of the two to
  // the root's dedup, silently.
  await stubSearch(page, [
    chunk({ id: 5, heading: "Private release note", tier: "private" }),
    chunk({ id: 5, heading: "Commons release note", tier: "commons" }),
    chunk({ id: 9, heading: "Third release note" }),
  ]);
  await open(page);
  await page.locator(`${PALETTE} input`).fill("release");

  for (const label of ["Private release note", "Commons release note", "Third release note"]) {
    await expect(page.locator(OPTION).filter({ hasText: label })).toBeVisible();
  }
});

test("an instance with no knowledge store neither searches nor spins", async ({ page }) => {
  // The capability gate. `/api/runtime/status` says whether a store exists at all, and the
  // console already fetches it on boot — so the palette can decline to register a provider
  // that could only ever answer `{enabled: false, results: []}`. Without the gate the root
  // raises "Searching…" on every typed query the moment ANY provider exists.
  await page.route("**/api/runtime/status**", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    body.knowledge = { ...(body.knowledge ?? {}), enabled: false, status: "disabled" };
    await route.fulfill({ response: res, json: body });
  });
  let searches = 0;
  await page.route("**/api/knowledge/search**", (route) => {
    searches += 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ enabled: false, query: "", results: [], stats: {} }),
    });
  });

  await open(page);
  // The busy affordance must never appear — not on the empty root, and not on a query.
  await expect(page.locator(".pl-cmdk-commands__spinner")).toHaveCount(0);
  // A query the console's OWN commands answer, so "no rows" can't be what hides the spinner.
  await page.locator(`${PALETTE} input`).fill("settings");
  await expect(page.locator(OPTION).first()).toBeVisible();
  await expect(page.locator(".pl-cmdk-commands__spinner")).toHaveCount(0);
  expect(searches).toBe(0);
});
