import { expect, test } from "@playwright/test";

// A background delegation's live progress card must stay on screen for as long as the job
// runs. It used to blink out and come back, over and over (jobCoach, core 0.192.0): every
// member's `/api/events` stream ended after its first idle keepalive (15s), the console
// reconnected, and the reconnect re-hydrated the chat's background-job store from
// `GET /api/background` — whose rows (keyed `id`, no progress) REPLACED the store's entry,
// dropping the delegate's live snapshot until its next `background.progress` frame.
//
// This replays that cadence against the mock: one snapshot, then a bus that keeps dropping
// with nothing new to say, each reconnect followed by the list poll. The card is sampled
// on EVERY animation frame across several reconnects; a single missing frame fails it.

const SESSION = "chat-progress-flicker-e2e";
const JOB = "bg-4109c71161eb"; // the DELEGATE_BG fixture's job
const DESCRIPTION = "delegate → sonnet: Land PR #13 and close #12";

// The REST row exactly as a 0.192.0 member returns it: `id`, not `job_id`, and no progress.
const LISTED = {
  id: JOB,
  agent_name: "lead",
  origin_session: SESSION,
  subagent_type: "delegate",
  description: DESCRIPTION,
  status: "running",
  result: "",
  notified: false,
  created_at: "2026-10-04T19:23:26.230288+00:00",
  completed_at: null,
  a2a_task_id: "",
  origin_incognito: false,
  batch_id: null,
  dismissed: false,
  deterministic: true,
  result_author: "sonnet",
  error: "",
};

const SNAPSHOT = {
  target: "sonnet",
  plan: [
    { content: "Rebase PR #13", status: "completed" },
    { content: "Land it", status: "in_progress" },
  ],
  current_tool: null, // between tools: the last one ended, the next hasn't started
  recent_tools: [{ id: "t1", name: "gh pr checks 13", kind: "execute", status: "completed" }],
  tool_count: 1,
  done: false,
  ok: true,
};

async function until(ready: () => boolean, budgetMs = 15_000): Promise<void> {
  const deadline = Date.now() + budgetMs;
  while (!ready() && Date.now() < deadline) await new Promise((r) => setTimeout(r, 25));
}

function sse(frames: { topic: string; data: Record<string, unknown> }[]) {
  return frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");
}

test("a background delegation's progress card never blinks out while its job runs", async ({ page }) => {
  test.setTimeout(60_000);
  await page.addInitScript(
    ([session]) => {
      window.localStorage.setItem(
        "protoagent.chat.sessions",
        JSON.stringify({
          version: 1,
          currentSessionId: session,
          sessions: [{ id: session, title: "lead", createdAt: 1, updatedAt: 2, messages: [] }],
        }),
      );
    },
    [SESSION],
  );

  let listPolls = 0;
  await page.route("**/api/background", (route) => {
    listPolls++;
    return route.fulfill({ json: { enabled: true, jobs: [LISTED] } });
  });
  await page.route("**/api/background/bg-*", (route) => route.fulfill({ json: LISTED }));

  // The bus: the first connection delivers the start and ONE snapshot, then every
  // connection ends with nothing new — the member's idle-keepalive drop, on a short clock.
  let conn = 0;
  let released = false;
  await page.route("**/api/events**", async (route) => {
    const n = conn++;
    const headers = { "content-type": "text/event-stream", "cache-control": "no-cache" };
    if (n === 0) {
      await until(() => released);
      return route.fulfill({
        status: 200,
        headers,
        body: sse([
          {
            topic: "background.started",
            data: { job_id: JOB, origin_session: SESSION, subagent_type: "delegate", description: DESCRIPTION },
          },
          {
            topic: "background.progress",
            data: { job_id: JOB, origin_session: SESSION, phase: "delegate_progress", progress: SNAPSHOT },
          },
        ]),
      });
    }
    await new Promise((r) => setTimeout(r, 700));
    return route.fulfill({ status: 200, headers, body: ": connected\n\n: keepalive\n\n" });
  });

  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("DELEGATE_BG hand the portfolio work to sonnet");
  await composer.press("Enter");
  await expect(page.locator(".chat-delegation-row")).toHaveCount(1);

  released = true;
  const card = page.locator(".chat-delegation .delegate-progress");
  await expect(card).toBeVisible();
  await expect(card).toContainText("gh pr checks 13");

  // Sample every animation frame from here on.
  await page.evaluate(() => {
    const w = window as unknown as { __cardFrames: { total: number; missing: number } };
    w.__cardFrames = { total: 0, missing: 0 };
    const tick = () => {
      w.__cardFrames.total++;
      if (!document.querySelector(".chat-delegation .delegate-progress")) w.__cardFrames.missing++;
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });

  // Several reconnect → re-hydrate cycles.
  const pollsAtStart = listPolls;
  const connsAtStart = conn;
  await until(() => conn >= connsAtStart + 4 && listPolls >= pollsAtStart + 4, 30_000);
  await page.waitForTimeout(500); // the last hydrate lands and renders

  const frames = await page.evaluate(
    () => (window as unknown as { __cardFrames: { total: number; missing: number } }).__cardFrames,
  );
  expect(conn - connsAtStart, "the bus reconnected several times").toBeGreaterThanOrEqual(4);
  expect(listPolls - pollsAtStart, "each reconnect re-polled the list").toBeGreaterThanOrEqual(4);
  expect(frames.total).toBeGreaterThan(60);
  expect(frames.missing, `card missing in ${frames.missing} of ${frames.total} frames`).toBe(0);
  await expect(card).toContainText("gh pr checks 13");
  await expect(page.getByTestId("chat-bgwork")).toContainText("1 background job running");
});
