import { expect, test } from "@playwright/test";

// A server-fired turn is watchable while it runs (#2361).
//
// Scheduled fires, watch reactions and background push-resumes self-POST into a chat
// session; the server holds that A2A stream, so the browser used to show a typing
// indicator and nothing else for the whole turn — a multi-minute blackout indistinguishable
// from a hang. The backend now republishes the turn's frames as `chat.progress`.
//
// Harness: seed an open chat session, then feed the bus a realistic server-turn sequence —
// turn.started, interleaved narration + tool frames, then chat.resumed. Asserts the live
// content appears DURING the turn, and that the terminal event replaces the preview rather
// than leaving the operator with the same answer twice.

const SESSION = "chat-serverturn-e2e";
const TASK = "task-serverturn-1";
const NARRATION_A = "Three dice and all Push Back.";
const NARRATION_B = "Ball secured at (8,17).";
const FINAL = "Turn complete: the ball is secured and the cage is set.";

function sse(frames: { topic: string; data: Record<string, unknown> }[]) {
  return frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");
}

test("a server-fired turn streams its narration and tools, then settles into one answer", async ({
  page,
}) => {
  await page.addInitScript(
    ([session]) => {
      window.localStorage.setItem(
        "protoagent.chat.sessions",
        JSON.stringify({
          version: 1,
          currentSessionId: session,
          sessions: [{ id: session, title: "spawner", createdAt: 1, updatedAt: 2, messages: [] }],
        }),
      );
    },
    [SESSION],
  );

  // Phase 1 — the turn is running: indicator + interleaved progress, no terminal event yet.
  const live = [
    { topic: "turn.started", data: { session_id: SESSION, origin: "scheduler", trigger: "job-1" } },
    { topic: "chat.progress", data: { session_id: SESSION, task_id: TASK, phase: "text", text: NARRATION_A } },
    {
      topic: "chat.progress",
      data: { session_id: SESSION, task_id: TASK, phase: "tool_start", tool: "roll_block", tool_call_id: "tc1" },
    },
    {
      topic: "chat.progress",
      data: {
        session_id: SESSION,
        task_id: TASK,
        phase: "tool_end",
        tool: "roll_block",
        tool_call_id: "tc1",
        output: "Push Back ×3",
      },
    },
    { topic: "chat.progress", data: { session_id: SESSION, task_id: TASK, phase: "text", text: NARRATION_B } },
  ];

  let phase = 0;
  await page.route("**/api/events**", (route) => {
    // First connection streams the in-flight turn; the EventSource reconnect then delivers
    // the terminal event, which is also how a real long turn lands.
    const body = phase++ === 0 ? sse(live) : sse([
      {
        topic: "chat.resumed",
        data: { session_id: SESSION, task_id: TASK, text: FINAL, state: "completed" },
      },
      { topic: "turn.finished", data: { session_id: SESSION, origin: "scheduler" } },
    ]);
    route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      body,
    });
  });

  await page.goto("/app/", { waitUntil: "load" });

  // THE BUG: this content was invisible until the turn ended. It must be on screen now.
  await expect(page.getByText(NARRATION_A)).toBeVisible();
  await expect(page.getByText(NARRATION_B)).toBeVisible();
  // …and the tool it ran between them.
  await expect(page.locator(".pl-toolcard").filter({ hasText: "roll_block" }).first()).toBeVisible();

  // Phase 2 — the terminal event replaces the preview in place.
  await expect(page.getByText(FINAL)).toBeVisible();
  // Exactly one assistant bubble for the turn: the live preview became the final answer
  // rather than being joined by a duplicate carrying the same work.
  await expect(page.getByText(FINAL)).toHaveCount(1);
  // The preview's streamed narration is superseded by the authoritative answer.
  await expect(page.getByText(NARRATION_A)).toHaveCount(0);
  // …but the tool card the live view rendered survives — it is the turn's visible work.
  await expect(page.locator(".pl-toolcard").filter({ hasText: "roll_block" }).first()).toBeVisible();
});
