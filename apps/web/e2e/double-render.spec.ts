import { expect, test, type Page } from "@playwright/test";
import { expandToolCard } from "./toolcard";

// #1938 — a completed long tool-call turn rendered its reply TWICE in the console
// while the server had it once. The live repro shape: two console boots ~1s apart
// (tab + PWA / a fast reload) sharing one localStorage key, then a 20–60s image-tool
// turn. These specs drive the real compiled SPA through that shape against the mock
// backend (SLOW turns stretch the frame gaps so mid-stream interleaving is real):
//
// 1. two live tabs — one streams, the other watches via the cross-tab storage sync;
// 2. a sibling tab that RELOADS mid-turn (the double-boot from the issue's journal),
//    whose self-heal reconciler (GetTask) races the live stream in the first tab.
//
// Acceptance (#1938): the reply renders exactly once in EVERY view, and the persisted
// store holds exactly one assistant entry for the turn.

const ANSWER = "Testing catches bugs before users do.";
const STORAGE_KEY = "protoagent.chat.sessions";

async function sendSlowStream(page: Page) {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("SLOW STREAM the answer");
  await composer.press("Enter");
}

/** How many times the answer text occurs in the page's rendered chat DOM. */
async function renderedAnswerCount(page: Page): Promise<number> {
  const text = await page.locator(".chat-session-slot:not([hidden])").innerText();
  return text.split(ANSWER).length - 1;
}

/** Assistant entries in the persisted store that carry the answer (content or parts). */
async function persistedAnswerCount(page: Page): Promise<number> {
  return page.evaluate(
    ([key, answer]) => {
      const raw = window.localStorage.getItem(key);
      if (!raw) return -1;
      const state = JSON.parse(raw) as { sessions: { messages: { role: string; content: string }[] }[] };
      return state.sessions
        .flatMap((s) => s.messages)
        .filter((m) => m.role === "assistant" && m.content.includes(answer)).length;
    },
    [STORAGE_KEY, ANSWER] as const,
  );
}

/** Boot the sender, run one quick turn so the session persists, then boot the
 *  sibling — which loads the SAME persisted currentSessionId, exactly like the
 *  issue's two boots ~1s apart. Both tabs now view one shared session. */
async function bootSharedSession(page: Page, context: { newPage(): Promise<Page> }) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("CALC 19 * 23");
  await composer.press("Enter");
  await expect(page.getByText("19 × 23 = 437.")).toBeVisible({ timeout: 10_000 });
  const sibling = await context.newPage();
  await sibling.goto("/app/", { waitUntil: "load" });
  await expect(sibling.getByText("19 × 23 = 437.")).toBeVisible({ timeout: 5_000 });
  return sibling;
}

test("two live tabs: a slow streamed turn renders its reply exactly once in each", async ({ context, page }) => {
  const sibling = await bootSharedSession(page, context);

  await sendSlowStream(page);
  // Mid-stream: partial text visible in the sender.
  await expect(page.locator(".pl-message--assistant .markdown").last()).toContainText("Testing");

  // Sender settles to the terminal text.
  await expect(page.getByText(ANSWER)).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(700); // let the debounced persist + storage sync settle

  expect(await renderedAnswerCount(page), "sender tab").toBe(1);
  expect(await persistedAnswerCount(page), "persisted store").toBe(1);

  // The sibling synced the turn via the storage event — once, not twice.
  await expect(sibling.getByText(ANSWER)).toBeVisible({ timeout: 5_000 });
  expect(await renderedAnswerCount(sibling), "sibling tab").toBe(1);
});

test("sibling tab reloading mid-turn (double-boot): reply still renders exactly once everywhere", async ({
  context,
  page,
}) => {
  const sibling = await bootSharedSession(page, context);

  await sendSlowStream(page);
  await expect(page.locator(".pl-message--assistant .markdown").last()).toContainText("Testing");

  // The issue's journal shape: a second boot while the turn streams. The reloaded
  // tab loads the persisted mid-stream state (assistant stuck `streaming` with a
  // taskId, no live controller) and fires its self-heal GetTask against the mock —
  // racing the sender tab's live stream on the shared localStorage key.
  await sibling.reload({ waitUntil: "load" });

  await expect(page.getByText(ANSWER)).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(700);

  expect(await renderedAnswerCount(page), "sender tab").toBe(1);
  expect(await persistedAnswerCount(page), "persisted store").toBe(1);

  // The reloaded sibling settles to exactly one copy too — either the storage sync
  // of the sender's final write or its own reconcile, never both stacked.
  await sibling.waitForTimeout(700);
  const siblingText = await sibling.locator(".chat-session-slot:not([hidden])").innerText();
  const siblingCount = siblingText.split(ANSWER).length - 1;
  const reconciled = siblingText.split("RECONCILED ANSWER").length - 1;
  expect(siblingCount + reconciled, "sibling tab total answer copies").toBeLessThanOrEqual(1);
  expect(await persistedAnswerCount(sibling), "persisted store after sibling settles").toBeLessThanOrEqual(1);
});

// A SECOND way one turn rendered twice, on a layer #1938's id-dedupe cannot see.
// Interjecting mid-turn makes the console SPLIT its live assistant bubble to place
// the interjection where the agent consumed it (#3150): the prose so far freezes
// into a bubble with a FRESH id, and an emptied continuation keeps the original.
// The A2A terminal frame then re-sends the WHOLE turn's canonical text (#1717) —
// which used to land on that continuation in full, drawing the frozen prose a
// second time. Two different ids, so `dedupeMessages` never collapsed them and the
// duplicate persisted to localStorage for good.
/** Every persisted bubble as `role:content`, for polling the store past its trailing
 *  300ms persist timer. */
async function persistedBubbles(page: Page): Promise<string[]> {
  return page.evaluate(
    ([key]) => {
      const raw = window.localStorage.getItem(key);
      if (!raw) return [];
      const state = JSON.parse(raw) as { sessions: { messages: { role: string; content: string }[] }[] };
      return state.sessions.flatMap((s) => s.messages).map((m) => `${m.role}:${m.content}`);
    },
    [STORAGE_KEY] as const,
  );
}

const PREAMBLE = "Let me look that up.";
const PREAMBLE_ANSWER = "Found it — Agent Client Protocol.";
const STEER = "also check the version";

test("interjecting mid-turn: the answer renders once, with the steer inline", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  // NOT getByPlaceholder: mid-turn the composer's placeholder flips to "Steer the
  // agent…", and a placeholder-bound locator would sit waiting for the very turn it
  // is supposed to interrupt to finish first.
  const composer = page.locator("textarea").first();
  await composer.waitFor({ state: "visible" });
  // PREAMBLE streams narration BEFORE the tool, so there is prose to freeze; STEER ME
  // parks the turn there until the interjection lands, then folds it in and finishes
  // — split and terminal frame in one turn, with no keystroke to land in a frame gap.
  await composer.fill("PREAMBLE, STEER ME: look it up");
  await composer.press("Enter");
  await expect(page.getByText(PREAMBLE)).toBeVisible({ timeout: 15_000 });

  await composer.fill(STEER);
  await composer.press("Enter");

  await expect(page.getByText(PREAMBLE_ANSWER)).toBeVisible({ timeout: 15_000 });

  // The split really happened: two assistant bubbles for the one turn, the operator's
  // interjection between them — and the narration in exactly one of them.
  //
  // POLLED, not slept on: persistence is a trailing 300ms timer, so the answer can be on
  // screen before the store has it. The ORDER is what proves the steer was consumed
  // INLINE — a steer the agent never folded in settles through the turn-end fallback,
  // which places it BEFORE the assistant bubble, so this assertion cannot pass on a
  // dropped or merely-queued steer (verified by making the mock drop it).
  await expect.poll(() => persistedBubbles(page), { timeout: 10_000 }).toEqual([
    "user:PREAMBLE, STEER ME: look it up",
    `assistant:${PREAMBLE}`,
    `user:${STEER}`,
    `assistant:${PREAMBLE_ANSWER}`,
  ]);

  // …and once on screen, in that order.
  const rendered = await page.locator(".chat-session-slot:not([hidden])").innerText();
  expect(rendered.split(PREAMBLE).length - 1, "narration copies on screen").toBe(1);
  expect(rendered.split(PREAMBLE_ANSWER).length - 1, "answer copies on screen").toBe(1);
  expect(rendered.indexOf(PREAMBLE)).toBeLessThan(rendered.indexOf(STEER));
});

// A THIRD way one turn rendered twice — the one Josh hit on the released v0.164.0
// desktop, addressing `@protoEngineer` from jobCoach: "duplicate output from response at
// delegated agent". An `@`-addressed turn short-circuits the lead, so the answer text on
// the wire is ONE answer published TWICE over: once as the participant's own room-v1
// authorship frame (which the console draws as their bubble, under their byline) and
// once as the turn's canonical answer artifact — the whole for A2A / `/v1` consumers,
// which get no room frames. #3051 dropped the live bubble for exactly this; #3115 gated
// that drop on the bubble being empty, which it never is by `done`: the canonical replace
// has already landed there (and since #3151 the address's own work card sits in it too).
// So the guard went dead and the answer rendered twice, verbatim, one copy under the other.
//
// The fix states the fact rather than inferring it: `in_answer` per exchange on the wire,
// stamped onto the turn's bubbles as `answeredByParticipants`, which the ONE function
// every producer of canonical text passes through then honours (#3449). The three specs
// below cover the claim, the part of the answer no bubble carries, and the shape where
// nothing is claimed at all.
const MENTION_ANSWER = "The current bundled Artifact plugin version is";
const MENTION_ROOM_NOTE = "Older messages were left out of the catch-up for @protoEngineer";
const MENTION_FAILURE_LINE = "Delegate @protoEngineer failed: connection refused";

/** Every persisted assistant bubble of the addressed turn, with the fields the answer's
 *  identity and its footer/actions depend on. */
async function persistedAssistants(page: Page) {
  return page.evaluate(
    ([key]) => {
      const raw = window.localStorage.getItem(key);
      if (!raw) return [];
      const state = JSON.parse(raw) as {
        sessions: {
          messages: {
            role: string;
            content: string;
            author?: { name: string };
            taskId?: string;
            usage?: unknown;
            contextWindow?: unknown;
            answeredByParticipants?: boolean;
          }[];
        }[];
      };
      return state.sessions
        .flatMap((s) => s.messages)
        .filter((m) => m.role === "assistant")
        .map((m) => ({
          author: m.author?.name ?? null,
          hasAnswer: m.content.includes("The current bundled Artifact plugin version is"),
          taskId: Boolean(m.taskId),
          footer: Boolean(m.usage || m.contextWindow),
          stamped: Boolean(m.answeredByParticipants),
        }));
    },
    [STORAGE_KEY] as const,
  );
}

test("an @-addressed delegate's answer renders once, under its author's byline", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("@protoEngineer what ver is the latest artifact plugin?");
  await composer.press("Enter");

  await expect(page.getByText(MENTION_ANSWER).first()).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(700); // the debounced persist

  const rendered = await page.locator(".chat-session-slot:not([hidden])").innerText();
  expect(rendered.split(MENTION_ANSWER).length - 1, "answer copies on screen").toBe(1);

  // The surviving copy is the PARTICIPANT's, not a lead bubble that happens to hold the
  // same words: the answer is protoEngineer's own, and the byline is the whole point of
  // rendering it as a room bubble (#3042). Asserting this rules out the mirror-image
  // regression — dropping the authored bubble and keeping the unattributed canonical one.
  const authored = page.locator(".pl-message--assistant", { has: page.locator(".chat-author-name") });
  await expect(authored.filter({ hasText: MENTION_ANSWER })).toHaveCount(1);
  await expect(page.locator(".chat-author-name").filter({ hasText: "protoEngineer" }).first()).toBeVisible();

  // …and the address's own work card survives, so the turn still records what was
  // dispatched and what came back (#3151). Dropping the whole live bubble — #3051's
  // original move, before the card existed — would take it with it.
  const card = page.locator(".pl-toolcard").filter({ hasText: "@protoEngineer" });
  await expect(card).toHaveCount(1);
  await expandToolCard(page, card);
  await expect(card).toContainText("1 replied");

  // The turn's spend/context footer and its per-message actions still have a home
  // (#3449 C): the spent continuation is FOLDED into the surviving half rather than
  // deleted, so `usage`/`contextWindow` move with it — and the bubble that shows the
  // answer carries the task id, so Copy / Fork / Rewind / View prompt work on it.
  const before = await persistedAssistants(page);
  expect(before.filter((m) => m.hasAnswer).length, "one persisted copy").toBe(1);
  expect(before.some((m) => m.footer), "the turn's footer survived the fold").toBe(true);
  expect(before.find((m) => m.hasAnswer)?.taskId, "the answer bubble carries the task id").toBe(true);
  // The stamp is on the TURN's own bubbles — not on the participants', which are their
  // messages, not the turn's. That is what the boot-hydration repair resolves a turn to,
  // and what persists the fact across the reload below.
  const own = before.filter((m) => !m.author);
  expect(own.length, "the turn kept a bubble of its own").toBeGreaterThan(0);
  expect(own.every((m) => m.stamped), "every bubble of the turn is stamped").toBe(true);

  // THE NEXT PAGE LOAD (#3449 A). The settled shape — a task-bearing bubble holding the
  // work card and no text — is exactly what ADR 0104 boot hydration repairs, and the
  // repair cannot see that a PARTICIPANT's bubble holds the answer. Landing it there
  // would have made the duplicate permanent and inverted the old workaround, which was
  // "reload and it's fine".
  await page.reload({ waitUntil: "load" });
  await expect(page.getByText(MENTION_ANSWER).first()).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(900); // hydration + the debounced persist
  const afterRender = await page.locator(".chat-session-slot:not([hidden])").innerText();
  expect(afterRender.split(MENTION_ANSWER).length - 1, "answer copies after a reload").toBe(1);
  expect((await persistedAssistants(page)).filter((m) => m.hasAnswer).length, "persisted after a reload").toBe(1);
});

test("a truncated catch-up: the reply once, and the room's note once", async ({ page }) => {
  // #3449 B — DEFAULT config. A moderately long chat clips its catch-up window, so the
  // answer carries the room's note as well as the reply. Dropping the claim for the whole
  // turn (the first cut of this fix) left Josh's exact symptom alive here; the note gets
  // its own frame instead, so every word of the answer is on screen exactly once.
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("@LONGROOM what ver is the latest artifact plugin?");
  await composer.press("Enter");

  await expect(page.getByText(MENTION_ROOM_NOTE).first()).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(700);

  const rendered = await page.locator(".chat-session-slot:not([hidden])").innerText();
  expect(rendered.split(MENTION_ANSWER).length - 1, "answer copies on screen").toBe(1);
  expect(rendered.split(MENTION_ROOM_NOTE).length - 1, "room-note copies on screen").toBe(1);
  // The note is the ROOM speaking about its own bounds, so it carries no byline — and it
  // reads BELOW the reply it annotates.
  const noted = page.locator(".pl-message--assistant").filter({ hasText: MENTION_ROOM_NOTE });
  await expect(noted).toHaveCount(1);
  await expect(noted.locator(".chat-author-name")).toHaveCount(0);
  expect(rendered.indexOf(MENTION_ANSWER)).toBeLessThan(rendered.indexOf(MENTION_ROOM_NOTE));
});

test("an addressed turn with nothing claimed still renders its answer", async ({ page }) => {
  // The other half of the contract, and the half a fix like this gets wrong: when the
  // server claims NOTHING (a failed address — the participant has no words, so the
  // answer's failure line lives only in the answer), the console must land the canonical
  // text exactly as it did before. This is the test that would catch "the answer
  // vanished" if the refusal were ever applied too eagerly.
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("@DEADROOM what ver is the latest artifact plugin?");
  await composer.press("Enter");

  await expect(page.getByText(MENTION_FAILURE_LINE).first()).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(700);
  const rendered = await page.locator(".chat-session-slot:not([hidden])").innerText();
  expect(rendered.split(MENTION_FAILURE_LINE).length - 1, "failure line copies on screen").toBe(1);
  // …and it is attributed to the member the operator addressed: the byline-only frame
  // stamped the live bubble, which is where the answer then landed.
  await expect(page.locator(".chat-author-name").filter({ hasText: "protoEngineer" }).first()).toBeVisible();
  // It survives a reload too — nothing was stamped, so hydration behaves as it always has.
  await page.reload({ waitUntil: "load" });
  await expect(page.getByText(MENTION_FAILURE_LINE).first()).toBeVisible({ timeout: 15_000 });
  await page.waitForTimeout(900);
  const after = await page.locator(".chat-session-slot:not([hidden])").innerText();
  expect(after.split(MENTION_FAILURE_LINE).length - 1, "failure line after a reload").toBe(1);
});

test("interjecting after the agent has finished: no blank bubble under the answer", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.locator("textarea").first();
  await composer.waitFor({ state: "visible" });
  // STEER LATE parks the turn after its whole answer has streamed, so the split
  // freezes everything and the continuation is opened for text that never arrives.
  await composer.fill("PREAMBLE, STEER LATE: look it up");
  await composer.press("Enter");
  await expect(page.getByText(PREAMBLE_ANSWER)).toBeVisible({ timeout: 15_000 });

  await composer.fill(STEER);
  await composer.press("Enter");

  // The whole answer in ONE bubble, the interjection after it — and crucially no third,
  // empty assistant bubble left holding the turn open. Same ordering proof as above: the
  // steer sits AFTER the answer only because the agent consumed it inline, so this cannot
  // pass on a dropped steer.
  await expect.poll(() => persistedBubbles(page), { timeout: 15_000 }).toEqual([
    "user:PREAMBLE, STEER LATE: look it up",
    // Narration either side of the tool is two model calls: one paragraph each.
    `assistant:${PREAMBLE}\n\n${PREAMBLE_ANSWER}`,
    `user:${STEER}`,
  ]);
  const rendered = await page.locator(".chat-session-slot:not([hidden])").innerText();
  expect(rendered.split(PREAMBLE_ANSWER).length - 1, "answer copies on screen").toBe(1);
});
