import { expect, test, type Page } from "@playwright/test";

// A server-started turn that finishes while the operator is away must not strand its live
// preview (bd-wk4ti). The bus grew a streaming placeholder for the turn (`turn.started` +
// `chat.progress`); the operator switched away; the turn completed, but nothing this window
// received settled the preview — and the reattach self-heal is held up (the #5161 desktop
// shape). On return, durable hydration must draw EXACTLY ONE bubble for that task: the
// server's finished copy replaces the spinning placeholder instead of landing beside it.
//
// The bus is fully scripted (one phase per connection, chat-stuck-streaming.spec's pattern)
// so the "missed while away" frames are deterministic, and the turn's `/a2a` resubscribe is
// held open so the reattach can never be the healer — the hydration reconcile is what this
// spec pins.

const SLOT = ".chat-session-slot:not([hidden])";
const SESSION = "chat-away";
const TASK = "task-away-1";
const PREVIEW = "Reviewing the brandLaunch result…";
const ANSWER = "brandLaunch came back, and I agree with its plan.";

type Frame = { topic: string; data: Record<string, unknown> };

async function until(ready: () => boolean, budgetMs = 15_000): Promise<void> {
  const deadline = Date.now() + budgetMs;
  while (!ready() && Date.now() < deadline) await new Promise((r) => setTimeout(r, 25));
}

/** Serve the bus one released phase per connection, in order; nothing is replayed. */
async function scriptBus(page: Page): Promise<(frames: Frame[]) => void> {
  const phases: Frame[][] = [];
  let served = 0;
  await page.route("**/api/events**", async (route) => {
    await until(() => phases.length > served, 20_000);
    const frames = phases.length > served ? phases[served++] : [];
    await route
      .fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
        body: frames.length ? frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("") : ": idle\n\n",
      })
      .catch(() => {});
  });
  return (frames) => {
    phases.push(frames);
  };
}

test("a server turn finishing while away leaves one bubble, not a spinner beside the answer", async ({ page }) => {
  const release = await scriptBus(page);
  // Chat A as this browser last rendered it: the kickoff turn, settled.
  await page.addInitScript(([id]) => {
    window.localStorage.setItem(
      "protoagent.chat.sessions",
      JSON.stringify({
        version: 1,
        currentSessionId: id,
        sessions: [{
          id,
          title: "Kick off brandLaunch",
          createdAt: 1,
          updatedAt: 2,
          messages: [
            { id: "u-away-0", role: "user", content: "Kick off brandLaunch", status: "done" },
            { id: "a-away-0", role: "assistant", content: "Delegated to brandLaunch in the background.", status: "done", taskId: "task-away-0" },
          ],
        }],
      }),
    );
  }, [SESSION]);

  // The durable store, before and after the turn ran: the index and the session's turns.
  let turnFinished = false;
  let turnReads = 0;
  const row = (over: Record<string, unknown>) => ({
    state: "TASK_STATE_COMPLETED", last_updated: "2026-11-12T17:00:00Z", status: { state: "TASK_STATE_COMPLETED" }, ...over,
  });
  await page.route("**/api/chat/sessions**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/chat/sessions") {
      return route.fulfill({ json: { sessions: [{
        session_id: SESSION, last_updated: "2026-11-12T17:00:00Z",
        turn_count: turnFinished ? 2 : 1, last_task_id: turnFinished ? TASK : "task-away-0",
      }] } });
    }
    if (path !== `/api/chat/sessions/${SESSION}/turns`) return route.fallback();
    turnReads += 1;
    return route.fulfill({ json: {
      live_task_id: null,
      turns: [
        row({ task_id: "task-away-0", text: "Delegated to brandLaunch in the background.",
          artifacts: [{ parts: [{ text: "Delegated to brandLaunch in the background." }] }],
          history: [{ role: "ROLE_USER", parts: [{ text: "Kick off brandLaunch" }] }] }),
        ...(turnFinished ? [row({ task_id: TASK, text: ANSWER, artifacts: [{ parts: [{ text: ANSWER }] }],
          history: [{ role: "ROLE_USER", parts: [{ text: "[background report] brandLaunch finished" }], metadata: { origin: "background-resume" } }] })] : []),
      ],
    } });
  });
  // The reattach's resubscribe to this task is HELD OPEN (the #5161 desktop shape): the
  // self-heal cannot answer for the preview, so only the hydration reconcile can.
  await page.route("**/a2a", async (route) => {
    const body = route.request().postDataJSON() as { method?: string; params?: { id?: string } } | null;
    if (body?.method === "SubscribeToTask" && body.params?.id === TASK) {
      await until(() => false, 60_000);
      return route.abort().catch(() => {});
    }
    return route.fallback();
  });

  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(`${SLOT} .pl-prompt__field`).waitFor({ state: "visible" });

  // The server starts a turn in this chat and streams its first words: the live preview.
  release([
    { topic: "turn.started", data: { session_id: SESSION, task_id: TASK, origin: "background-resume" } },
    { topic: "chat.progress", data: { session_id: SESSION, task_id: TASK, phase: "text", text: PREVIEW } },
  ]);
  await expect(page.locator(SLOT).getByText(PREVIEW)).toBeVisible();

  // Away: the turn completes. This window sees the turn end but never the settling answer
  // (`chat.resumed` was missed), and the durable store now records the finished turn.
  turnFinished = true;
  release([{ topic: "turn.finished", data: { session_id: SESSION, task_id: TASK } }]);

  // Return: the window regains focus and durable hydration re-reads the session.
  await expect
    .poll(
      async () => {
        await page.evaluate(() => window.dispatchEvent(new Event("focus")));
        return turnReads;
      },
      { timeout: 20_000 },
    )
    .toBeGreaterThan(0);

  // Exactly one bubble for the task — the answer — and no spinner anywhere in the transcript.
  await expect(page.locator(SLOT).getByText(ANSWER)).toBeVisible();
  await expect(page.locator(SLOT).getByText(PREVIEW)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .pl-message--assistant`)).toHaveCount(2);
  await expect(page.locator(`${SLOT} .pl-message--assistant .pl-spinner`)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .chat-server-turn`)).toHaveCount(0);
});
