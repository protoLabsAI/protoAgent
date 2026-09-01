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
  // …including the LAST-registered group. This is a fresh context, so there is no recency at
  // all — the list is pure registration order, Agents → Plugins → Commands. A plain
  // `slice(0, cap)` hands it to whoever registered first, and every plugin view installed
  // pushes one more Commands row off the bottom. The per-group quota is what keeps the
  // Commands group here at all, and this is the run where losing it would hurt most — it is
  // what absorbed the 22 generated `Settings: <Section>` rows (#3291) without moving anything
  // above it.
  //
  // Asserted on `Open…` rather than `Settings`: with SEVEN groups the quota guarantees each
  // exactly one row, and Commands registers `Open…` first. That is the right row to guarantee
  // — it is the doorway to every surface, where `Settings` is one destination among the 22
  // that follow it, and both are one keystroke away once you type. What must never regress is
  // that the group is REPRESENTED; which member leads it is registration order, asserted in
  // the group-header case below.
  //
  // Matched on the row's LABEL span, not on the option's accessible name: the Settings row
  // advertises its shortcut now (#3295 gave it `keybinding: "settings.open"`), and an
  // advertised combo renders as a trailing hint INSIDE the same button — so the accessible
  // name is "Settings ⌘," and `{ name: "Settings", exact: true }` finds nothing. The span
  // still holds the bare label, which is what has to stay exact: a loose "Settings" match
  // would be satisfied by any of the 22 `Settings: …` rows and stop testing the quota at all.
  // It is also the platform-independent half — `formatCombo` renders ⌘ on macOS, Ctrl on CI.
  // A retrying locator assertion rather than a one-shot `allInnerTexts()` read, because the
  // empty-query list is settled by an async provider read.
  await expect(
    page.locator(`${PANEL} ${ROW} .pl-cmdk-commands__label`, { hasText: /^Open…$/ }),
  ).toBeVisible();
});

test("the active row is announced — aria-activedescendant, not just a highlight", async ({ page }) => {
  const input = await openPalette(page);
  // Focus never leaves the input (arrows move a class, not focus), so this pointer is the
  // ONLY thing that tells a screen reader which row is live. The DS's own view ships the
  // combobox role without it, which is silence from the first ArrowDown onward.
  //
  // Read BOTH values inside one `expect.poll` evaluation and let it retry. Two separate
  // `getAttribute` round trips can straddle a provider read settling: the signature changes,
  // the selection recomputes, and the two reads disagree about a state that was never
  // actually inconsistent. Polling one combined read asserts the invariant instead of racing it.
  const pair = () =>
    page.evaluate((panel) => {
      const input = document.querySelector(`${panel} .pl-cmdk-commands__input`);
      const row = document.querySelector(`${panel} [data-sel="true"]`);
      return {
        active: input?.getAttribute("aria-activedescendant") ?? null,
        selected: row?.getAttribute("id") ?? null,
      };
    }, PANEL);

  await expect.poll(pair).toEqual(expect.objectContaining({ active: expect.any(String) }));
  await expect.poll(async () => {
    const { active, selected } = await pair();
    return active === selected && active !== null;
  }).toBe(true);

  await input.press("ArrowDown");
  await expect.poll(async () => {
    const { active, selected } = await pair();
    return active === selected && active !== null;
  }).toBe(true);
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

test("BROWSING teaches the empty list too — not just typing", async ({ page }) => {
  // `Open ▸` is a DS `commandsView`; the root view's single `run()` — where the frecency
  // write lives — does not reach inside another view. So the palette learned from typing and
  // learned nothing from the path this guide sends operators down ("the built-in surfaces
  // live one hop in, behind Open…"): the only thing recorded was `Open…` itself, and the
  // surface the operator actually opened never became a recent.
  await openPalette(page);
  await page.getByRole("option", { name: "Open…" }).click();
  await page.getByRole("option", { name: "Knowledge", exact: true }).click();
  await expect(page.locator(PANEL)).toHaveCount(0);

  await openPalette(page);
  await expect(page.locator(`${PANEL} .pl-cmdk-commands__group`).first()).toHaveText("Recent");
  await expect(page.getByRole("option", { name: "Knowledge", exact: true })).toBeVisible();
});

test("the empty list keeps every group, even once recents have taken most of it", async ({ page }) => {
  // The steady state after day one, and the case every OTHER assertion in this file misses:
  // they all run in a fresh context with no recency at all, which is the one situation where
  // a per-group CEILING happens to work. Recents are subtracted from the cap before the
  // curated fill runs, so a full block leaves five slots for Agents -> Plugins -> Commands —
  // and a ceiling of four never fires against five slots. The first two groups took all of
  // them and the whole Commands group (`Open…`, `Settings`, every registered deep link) went
  // off the bottom. Only a guaranteed first row per group survives that squeeze.
  //
  // The block is filled through `Open ▸` rather than by typing, so this also pins that
  // BROWSING feeds the recents list at all — the submorph is a DS view the root's `run()`
  // does not reach into.
  for (let i = 0; i < 4; i += 1) {
    await openPalette(page);
    await page.getByRole("option", { name: "Open…" }).click();
    // Wait for the morph to FINISH. Both bodies are mounted while it animates, so a bare
    // `nth(i)` can address the root list that is on its way out.
    await expect(page.getByPlaceholder("Open a surface…")).toBeVisible();
    await expect(page.getByPlaceholder(/Search commands/)).toHaveCount(0);
    await page.locator(`${PANEL} ${ROW}`).nth(i).click();
    await expect(page.locator(PANEL)).toHaveCount(0);
  }
  await openPalette(page);
  // Recents lead, and EVERY group still contributes — Commands, and Commands is the one that
  // vanished. Asserting the whole header list, not just the first: "recents are on top" was
  // already true when the bug was live.
  //
  // Chats is the fifth group and it is the reason this list is worth re-asserting rather than
  // relaxing: #3290 registers a row per open chat tab, so a group joined the root AFTER the
  // guarantee was written. Five groups against five post-recents slots is the tightest the
  // quota has ever been squeezed — every group is down to exactly its guaranteed first row
  // plus one — which makes this the strongest form of the assertion, not a weakened one.
  await expect(page.locator(`${PANEL} .pl-cmdk-commands__group`)).toHaveText([
    "Recent",
    "Agents",
    "Plugins",
    "Commands",
    // Chats (#3290) registers from a module side-effect import, so it lands BEFORE the chat
    // verbs, which register inside the adapter's effect — group order is registration order.
    "Chats",
    // Chat + Skills (#3292) — the chat's slash commands and the server's user-facing skills.
    // They are what the guarantee is FOR: ~80 commands land across the sibling command PRs,
    // each in its own group, and a per-group ceiling would have let the newest ones push the
    // oldest off the bottom. SEVEN groups against nine slots is the tightest the quota has
    // ever been squeezed — every group is down to exactly its guaranteed first row, and the
    // recents block gave up two of its four to make that possible (see RECENT_MIN).
    "Chat",
    "Skills",
  ]);
  // What the guarantee is worth is ONE row per group, not a named row: with four recents the
  // Commands group is down to its first member. `Open…` is that member and it is the row the
  // whole browse path hangs off — with it gone, every surface would be reachable only by
  // typing its name. `Settings` and the deep links are a keystroke away, and this list holds
  // all of them on a first run (the assertion at the top of this file).
  await expect(page.getByRole("option", { name: "Open…" })).toBeVisible();
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

test("the ranked list renders no group header twice", async ({ page }) => {
  // Headers are a CONTIGUITY marker, which equals grouping only in registration order.
  // Ranking sorts across groups, so the DS's inherited rule re-emitted the same header at
  // every transition — 8 headers over 16 rows on this console, "Commands" three times.
  const input = await openPalette(page);
  const groups = page.locator(`${PANEL} .pl-cmdk-commands__group`);
  await expect(groups.first()).toBeVisible(); // the untyped list IS grouped, and keeps them
  for (const q of ["s", "o", "t"]) {
    await input.fill(q);
    await expect(page.locator(`${PANEL} .pl-cmdk-commands__item`).first()).toBeVisible();
    expect(await groups.count()).toBe(0);
  }
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

// ── The visible way in (ADR 0057 findings 03/04) ──────────────────────────────────────
// Everything above is reachable ONLY by a chord until these exist. The pair of cases is the
// pair of shells: the desktop utility bar, and the chat-first mobile header (the mobile half
// lives in mobile.spec.ts — the `mobile` project is the only one that runs a device profile).

test("the utility bar offers the palette, and teaches its chord", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const btn = page.getByTestId("palette-widget");
  await expect(btn).toBeVisible();

  // The chord is READ FROM THE BINDING, so this asserts the rendered text against the same
  // source Settings ▸ Keyboard renders from rather than against a literal. `formatCombo`
  // emits ⌘ on macOS and Ctrl elsewhere, so match the SHAPE (a shift-modified K) instead of
  // a platform string — a literal here would be green on one CI runner and red on another.
  await expect(btn).toHaveText(/K$/);
  await expect(btn).toHaveAttribute("aria-keyshortcuts", /K$/);

  await expect(page.locator(PANEL)).toHaveCount(0);
  await btn.click();
  await expect(page.locator(PANEL)).toBeVisible();
});

test("the button's label never collides with the Settings ▸ Keyboard row", async ({ page }) => {
  // A REGRESSION PIN, not a style check. "Command palette" is the `palette.toggle` binding's
  // label, which Settings ▸ Keyboard renders; the dialog PORTALS over the shell rather than
  // unmounting it, and Playwright visibility ignores occlusion. So a button that put those
  // words on screen would make `getByText("Command palette", { exact: true })` resolve to two
  // nodes and break `keybindings.spec.ts` — a spec with nothing to do with this button. The
  // words live in `aria-label`, which getByText does not match.
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByText("Command palette", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("palette-widget")).toHaveAttribute("aria-label", "Search commands");
});
