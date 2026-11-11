import { expect, test } from "@playwright/test";

// A parked turn answered ELSEWHERE continues live in the open chat (bd-nfd6g.17).
//
// A `request_decision` turn parks, and the decision is then answered from its card, from
// `POST /api/decisions/{id}/answer`, or by expiry. None of those stream: the server resumes
// the parked checkpoint over its own loopback A2A call, continuing the SAME task id. The
// console had already settled its reattach as paused and let go of the stream, so nothing
// told it the task was running again — the tool row stayed "waiting for you" and the resume
// bubble + final reply only appeared after a reload. The bus's `turn.resumed` {task_id,
// context_id} is that signal: the open chat holding the paused bubble re-attaches to it.
//
// Seeded like chat-reattach-paused.spec.ts (a "paused-ask_human" task id the mock parks);
// the answered task is served by a route override once the spec says the answer landed.

const SLOT = ".chat-session-slot:not([hidden])";
const TAP = "decision-resume-live";
const TASK = "task-stuck-paused-ask_human-d1";
test.use({ extraHTTPHeaders: { "x-e2e-bus-tap": TAP } });

const completedTask = {
  id: TASK,
  contextId: "s-stuck",
  status: { state: "TASK_STATE_COMPLETED" },
  artifacts: [{ parts: [{ text: "Result D1: blue" }] }],
  history: [
    { role: "ROLE_USER", parts: [{ text: "pick a fruit" }] },
    {
      role: "ROLE_AGENT",
      parts: [],
      metadata: {
        "https://proto-labs.ai/a2a/ext/tool-call-v1": {
          toolCallId: "ask-1", name: "ask_human", phase: "started",
          args: JSON.stringify({ question: "Which fruit should I pick?" }),
        },
      },
    },
    { role: "ROLE_USER", parts: [{ text: "Operator chose: blue (blue)" }], metadata: { hitl_resume: true } },
    {
      role: "ROLE_AGENT",
      parts: [],
      metadata: {
        "https://proto-labs.ai/a2a/ext/tool-call-v1": {
          toolCallId: "ask-1", name: "ask_human", phase: "completed", result: "Operator chose: blue (blue)",
        },
      },
    },
  ],
};

test("a parked turn answered elsewhere continues live, without a reload", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "protoagent.chat.sessions",
      JSON.stringify({
        version: 1,
        currentSessionId: "s-stuck",
        sessions: [
          {
            id: "s-stuck",
            title: "Parked turn",
            createdAt: Date.now(),
            updatedAt: Date.now(),
            messages: [
              { id: "u1", role: "user", content: "pick a fruit", status: "done" },
              { id: "a1", role: "assistant", content: "", status: "streaming", taskId: "task-stuck-paused-ask_human-d1" },
            ],
          },
        ],
      }),
    );
  });

  // Until the answer lands the mock serves the parked task; afterwards the task is over —
  // a resubscribe is refused as for any terminal task, and GetTask returns the whole turn.
  let answered = false;
  await page.route("**/a2a", async (route) => {
    const body = route.request().postDataJSON() as { id?: unknown; method?: string; params?: { id?: string } };
    if (!answered || body?.params?.id !== TASK) return route.fallback();
    if (body.method === "SubscribeToTask") {
      return route.fulfill({
        json: { jsonrpc: "2.0", id: body.id, error: { code: -32004, message: "task is not running" } },
      });
    }
    if (body.method === "GetTask") return route.fulfill({ json: { jsonrpc: "2.0", id: body.id, result: completedTask } });
    return route.fallback();
  });

  await page.goto("/app/", { waitUntil: "load" });

  // Parked: the card reads "waiting for you" and the session is idle.
  const card = page.locator(`${SLOT} .pl-toolcard`).filter({ hasText: "ask_human" });
  await expect(card.locator(".tool-waiting")).toContainText("waiting for you");
  await expect(page.locator(`${SLOT} .chat-paused-indicator`)).toBeVisible();

  // The decision is answered somewhere else; the server resumes the same task and says so.
  answered = true;
  const pushed = await page.request.post("/api/__test__/bus", {
    data: { tap: TAP, frames: [{ topic: "turn.resumed", data: { task_id: TASK, context_id: "s-stuck" } }] },
  });
  expect((await pushed.json()).streams).toBeGreaterThan(0);

  // The rest of the turn lands live: the operator's answer, and the agent's reply.
  const slot = page.locator(SLOT);
  await expect(slot).toContainText("Operator chose: blue (blue)");
  await expect(slot).toContainText("Result D1: blue");
  await expect(card.locator(".tool-waiting")).toHaveCount(0);
  await expect(page.locator(`${SLOT} .chat-paused-indicator`)).toHaveCount(0);
  // The question was answered elsewhere, so its form is gone too.
  await expect(page.locator(`${SLOT} .hitl-float`)).toHaveCount(0);
});
