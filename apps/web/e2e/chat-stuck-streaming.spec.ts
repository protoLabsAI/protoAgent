import { expect, test, type Page, type Route } from "@playwright/test";

// A session reading "streaming" locks its composer: Stop shows and Send is disabled. These
// specs drive the session-status reconciler (src/chat/sessionLiveness.ts) through the real
// compiled console. The reconciler has to hand back a session nothing is still running, and
// must never idle one that a turn still holds.

const SLOT = ".chat-session-slot:not([hidden])";

type Frame = { topic: string; data: Record<string, unknown> };

async function until(ready: () => boolean, budgetMs = 15_000): Promise<void> {
  const deadline = Date.now() + budgetMs;
  while (!ready() && Date.now() < deadline) await new Promise((r) => setTimeout(r, 25));
}

/** Serve the bus one released phase per connection, in order. A connection with nothing
 *  released idles out and the console reconnects. Nothing is replayed. */
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

function tab(page: Page, title: string) {
  return page.locator(".pl-tabbar__tab").filter({ hasText: title });
}

test("the sixth live session is handed back when its turn ends, and opens usable", async ({ page }) => {
  const release = await scriptBus(page);
  // Seven sessions, each with a turn in flight at boot, so each boots reading "streaming".
  // Only MAX_ACTIVE_SESSIONS (5) slots mount: s1..s5 reattach at once, s6 and s7 do not.
  // s6's turn is a server-fired one, so its bubble is the bus preview. s7's is an operator
  // turn that was cut off.
  await page.addInitScript(() => {
    const session = (n: number, assistant: Record<string, unknown>) => ({
      id: `s${n}`,
      title: `Live turn ${n}`,
      createdAt: n,
      updatedAt: n,
      messages: [{ id: `u${n}`, role: "user", content: `question ${n}`, status: "done" }, assistant],
    });
    const cutOff = (n: number) => ({ id: `a${n}`, role: "assistant", content: "", status: "streaming", taskId: `task-stuck-${n}` });
    window.localStorage.setItem(
      "protoagent.chat.sessions",
      JSON.stringify({
        version: 1,
        currentSessionId: "s1",
        sessions: [
          ...[1, 2, 3, 4, 5].map((n) => session(n, cutOff(n))),
          session(6, { id: "server-turn-task-sixth", role: "assistant", content: "Reading the report…", status: "streaming", taskId: "task-sixth" }),
          session(7, cutOff(7)),
        ],
      }),
    );
  });
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(`${SLOT} .pl-prompt__field`).waitFor({ state: "visible" });

  // The mounted five reattach and settle. The two unmounted ones stay busy.
  await expect(page.locator(".pl-tabbar__tab .session-dot.streaming")).toHaveCount(2);
  await expect(tab(page, "Live turn 6").locator(".session-dot.streaming")).toHaveCount(1);

  // s6's turn ends while its slot is not mounted: `chat.resumed` settles its preview. No slot
  // and no reattach exist to hand the session back. THE BUG: it read "streaming" for good.
  release([
    { topic: "chat.resumed", data: { session_id: "s6", task_id: "task-sixth", text: "The report is clean.", state: "completed" } },
  ]);
  await expect(tab(page, "Live turn 6").locator(".session-dot.streaming")).toHaveCount(0);

  await tab(page, "Live turn 6").click();
  await expect(page.locator(SLOT).getByText("The report is clean.")).toBeVisible();
  await expect(page.locator(SLOT).getByRole("button", { name: "Stop", exact: true })).toHaveCount(0);
  await expect(page.locator(SLOT).getByPlaceholder(/Message protoAgent/i)).toBeVisible();

  // s7's turn was never settled: opening it reattaches, and the reattach settles it.
  await tab(page, "Live turn 7").click();
  await expect(page.locator(SLOT).getByText("RECONCILED ANSWER")).toBeVisible();
  await expect(page.locator(SLOT).getByRole("button", { name: "Stop", exact: true })).toHaveCount(0);
  await expect(page.locator(SLOT).getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("a local turn awaiting its post-stream reconcile is never idled, even on a visibility regain", async ({ page }) => {
  const SESSION = "s-local";
  const TASK = "task-post-stream";
  const PROMPT = "summarize the release notes";
  await page.addInitScript(
    ([id]) => {
      window.localStorage.setItem(
        "protoagent.chat.sessions",
        JSON.stringify({ version: 1, currentSessionId: id, sessions: [{ id, title: "Release notes", createdAt: 1, updatedAt: 2, messages: [] }] }),
      );
    },
    [SESSION],
  );

  // The turn streams its answer as deltas only, then the stream closes WITHOUT the terminal
  // canonical text. That is the #1938 shape: runTurn settles the bubble (onDone) and then
  // reconciles it against the durable task (GetTask), which is held here. For that whole
  // window the turn is live, but no bubble reads "streaming".
  let heldTask: Route | null = null;
  let releaseTask = false;
  await page.route("**/a2a", async (route) => {
    const body = route.request().postDataJSON() as { id?: unknown; method?: string; params?: Record<string, any> } | null;
    if (body?.method === "GetTask" && body.params?.id === TASK) {
      heldTask = route;
      await until(() => releaseTask, 20_000);
      return route
        .fulfill({
          json: {
            jsonrpc: "2.0",
            id: body.id,
            result: { id: TASK, contextId: SESSION, status: { state: "TASK_STATE_COMPLETED" }, artifacts: [{ parts: [{ text: "The notes are ready." }] }] },
          },
        })
        .catch(() => {});
    }
    const prompt = JSON.stringify(body?.params ?? {});
    if (body?.method !== "SendStreamingMessage" || !prompt.includes(PROMPT)) return route.fallback();
    const frame = (result: Record<string, unknown>) => `data: ${JSON.stringify({ jsonrpc: "2.0", id: body.id, result })}\n\n`;
    return route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      body:
        frame({ kind: "task", id: TASK, contextId: SESSION, status: { state: "submitted" }, artifacts: [] }) +
        frame({
          kind: "artifact-update",
          taskId: TASK,
          contextId: SESSION,
          artifact: { artifactId: TASK, parts: [{ kind: "text", text: "The notes are ready." }] },
          append: true,
          lastChunk: false,
        }),
    });
  });

  await page.goto("/app/", { waitUntil: "load" });
  const field = page.locator(`${SLOT} .pl-prompt__field`);
  await field.fill(PROMPT);
  await field.press("Enter");

  // The stream closed and the reconcile is out: the bubble shows its answer, settled.
  await expect.poll(() => heldTask !== null, { timeout: 15_000 }).toBe(true);
  await expect(page.locator(`${SLOT} .pl-message--assistant`).getByText("The notes are ready.")).toBeVisible();
  const stop = page.locator(SLOT).getByRole("button", { name: "Stop", exact: true });
  await expect(stop).toBeVisible();

  // The tab comes back into view mid-window, and the reconciler runs over every session.
  // The turn still holds this one, so Send must stay locked. Idling here would let a second
  // turn start in this slot while the first is still running.
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await page.waitForTimeout(500);
  await expect(stop).toBeVisible();

  releaseTask = true;
  await expect(stop).toHaveCount(0);
  await expect(page.locator(SLOT).getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});
