import { expect, type Locator, type Page } from "@playwright/test";

// Expanding a tool card in an e2e spec has ONE correct moment, and it isn't "the tool
// finished". This helper is that moment.
//
// While a turn is live, `ToolCalls` renders the current card inside a `.tool-spotlight`
// slot under a deliberately stable `key="__spotlight__"` (so a fast fan-out advances in
// place instead of strobing). On settle the same call re-renders as `key={call.id}` with
// no wrapper — different key, different parent — so React **remounts** the DS `ToolCard`.
// Its `open` state is uncontrolled (`useState(defaultOpen)` in `@protolabsai/ui`
// `tool-card.tsx`), so a remount always lands collapsed: an expansion clicked before that
// moment is silently thrown away, and the assertion on the body/children then fails.
//
// The gates the specs reached for first are both WRONG:
//   * `.pl-toolcard__status--done` — that's the TOOL finishing, several frames before the
//     turn does.
//   * the assistant's answer text — it streams in while the turn is still live, so it's
//     visible during exactly the window this race lives in.
// Both leave a window whose width is machine speed; under a parallel suite the click lands
// inside it. Measured on the pre-fix tree: 13/30 failures at `--workers=5`, 3/30 at
// `--workers=1` (`e2e/tool-nesting-explicit.spec.ts`).
//
// `.tool-spotlight` is the exact marker, because it is rendered by BOTH live branches
// (`streaming` in `ToolCalls`, and `WorkBlock`'s spotlight slot, which is itself gated on
// `streaming`) and by no settled one. Zero of them ⇒ the turn has settled and the cards
// are in their final layout, so the next remount that could eat the click doesn't exist.
//
// NOTE: this makes the SPECS deterministic; it does not fix the underlying product
// behaviour, which is that expanding a card mid-turn loses the expansion when the turn
// settles. That needs a controlled `open`/`onOpenChange` on the DS ToolCard so the state
// can outlive the remount — see the PR that added this file.

/** Wait until every tool card is in its settled (non-remounting) layout. */
export async function toolCardsSettled(page: Page): Promise<void> {
  await expect(page.locator(".tool-spotlight")).toHaveCount(0);
}

/** Wait for the settled layout, then expand `card` — the only safe way to click a
 *  toolcard head in a spec that drives a live turn. */
export async function expandToolCard(page: Page, card: Locator): Promise<void> {
  await toolCardsSettled(page);
  await card.locator(".pl-toolcard__head").click();
}
