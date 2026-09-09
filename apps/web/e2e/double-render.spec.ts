import { expect, test, type Page } from "@playwright/test";

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
  await page.waitForTimeout(700); // debounced persist

  // The split really happened: two assistant bubbles for the one turn, the operator's
  // interjection between them — and the narration in exactly one of them.
  const bubbles = await page.evaluate(
    ([key]) => {
      const raw = window.localStorage.getItem(key);
      if (!raw) return [];
      const state = JSON.parse(raw) as { sessions: { messages: { role: string; content: string }[] }[] };
      return state.sessions.flatMap((s) => s.messages).map((m) => `${m.role}:${m.content}`);
    },
    [STORAGE_KEY] as const,
  );
  expect(bubbles).toEqual([
    "user:PREAMBLE, STEER ME: look it up",
    `assistant:${PREAMBLE} `,
    `user:${STEER}`,
    `assistant:${PREAMBLE_ANSWER}`,
  ]);

  // …and once on screen, in that order.
  const rendered = await page.locator(".chat-session-slot:not([hidden])").innerText();
  expect(rendered.split(PREAMBLE).length - 1, "narration copies on screen").toBe(1);
  expect(rendered.split(PREAMBLE_ANSWER).length - 1, "answer copies on screen").toBe(1);
  expect(rendered.indexOf(PREAMBLE)).toBeLessThan(rendered.indexOf(STEER));
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
  await page.waitForTimeout(1500); // the turn finishes, then the debounced persist

  const bubbles = await page.evaluate(
    ([key]) => {
      const raw = window.localStorage.getItem(key);
      if (!raw) return [];
      const state = JSON.parse(raw) as { sessions: { messages: { role: string; content: string }[] }[] };
      return state.sessions.flatMap((s) => s.messages).map((m) => `${m.role}:${m.content}`);
    },
    [STORAGE_KEY] as const,
  );
  // The whole answer in ONE bubble, the interjection after it — and crucially no
  // third, empty assistant bubble left holding the turn open.
  expect(bubbles).toEqual([
    "user:PREAMBLE, STEER LATE: look it up",
    `assistant:${PREAMBLE} ${PREAMBLE_ANSWER}`,
    `user:${STEER}`,
  ]);
  const rendered = await page.locator(".chat-session-slot:not([hidden])").innerText();
  expect(rendered.split(PREAMBLE_ANSWER).length - 1, "answer copies on screen").toBe(1);
});
