import { expect, test, type Request } from "@playwright/test";

// B4: answering a request_user_input form.
//
// 1. The operator's answer bubble showed the raw JSON the graph resumes with — `{"plan":"C"}`.
//    It renders as a labelled card (#4259); the agent still receives the JSON, and the card's
//    text rides as the send's `display` so a chat rebuilt from the server reads the same.
// 2. A HITL answer continues the task that parked (#3930) as a NEW rendered turn: the question
//    bubble, the answer, then the reply — all on one task id. On reload, boot hydration matched
//    the server's recovered answer by task id and wrote it onto the prose-less question bubble
//    too, so the reply rendered under BOTH turns.

const SLOT = ".chat-session-slot:not([hidden])";

test("a submitted form reads as the picked labels, and the agent gets the JSON", async ({ page }) => {
  const sends: Array<{ text: string; display?: string; hitlResume?: boolean }> = [];
  page.on("request", (req: Request) => {
    if (!req.url().endsWith("/a2a") || req.method() !== "POST") return;
    try {
      const body = JSON.parse(req.postData() || "{}");
      const message = body.params?.message;
      if (!message) return;
      sends.push({
        text: (message.parts ?? []).map((p: { text?: string }) => p.text ?? "").join(""),
        display: message.metadata?.display,
        hitlResume: message.metadata?.hitl_resume,
      });
    } catch {
      /* not JSON */
    }
  });
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("HITL_PLAN pick one");
  await composer.press("Enter");

  const card = page.locator(`${SLOT} .hitl-card`);
  await expect(card).toContainText("Pick a plan");
  await card.locator(".hitl-card-option").filter({ hasText: "Plan C" }).click();
  await card.getByRole("button", { name: "Submit" }).click();

  const answer = page.locator(`${SLOT} .pl-message--user`).last();
  await expect(answer).toContainText("Plan C");
  await expect(answer).not.toContainText("{");
  await expect.poll(() => sends.find((s) => s.hitlResume)).toBeTruthy();
  const resume = sends.find((s) => s.hitlResume)!;
  expect(JSON.parse(resume.text)).toEqual({ plan: "C" });
  expect(resume.display).toBe("Plan: Plan C");
});

test("after a reload the reply to a form answer renders once, under the answer", async ({ page }) => {
  const REPLY = "You chose Plan C — locking it in.";
  const SESSION = "chat-hitl-answered";
  const TASK = "task-hitl-answered";
  // The local copy exactly as the live turn left it: the question bubble (the form's tool
  // card, no prose), the operator's answer, then the reply — one task id across both turns.
  await page.addInitScript(
    ({ reply, session, task }) => {
      if (sessionStorage.getItem("seeded-hitl")) return;
      sessionStorage.setItem("seeded-hitl", "1");
      const tool = { id: "c1", name: "request_user_input", status: "done", input: "{}" };
      localStorage.setItem(
        "protoagent.chat.sessions",
        JSON.stringify({
          version: 1,
          currentSessionId: session,
          sessions: [{
            id: session,
            title: "Ask me which plan",
            createdAt: 1,
            updatedAt: 1,
            messages: [
              { id: "u1", role: "user", content: "Ask me which plan", status: "done" },
              { id: "q1", role: "assistant", content: "", status: "done", taskId: task, parts: [{ kind: "tools", ids: ["c1"] }], toolCalls: [tool] },
              {
                id: "u2",
                role: "user",
                content: '{"plan":"C"}',
                status: "done",
                formResponse: { title: "Pick a plan", groups: [{ fields: [{ label: "Plan", value: "Plan C" }] }] },
              },
              {
                id: "a1",
                role: "assistant",
                content: reply,
                status: "done",
                taskId: task,
                parts: [{ kind: "tools", ids: ["c2"] }, { kind: "text", text: reply }],
                toolCalls: [{ ...tool, id: "c2" }],
              },
            ],
          }],
        }),
      );
    },
    { reply: REPLY, session: SESSION, task: TASK },
  );
  // The server's durable copy of that one task (ADR 0104): the prompt, the form call, the
  // operator's answer continuing the same task (its `display` is the bubble), the reply.
  const row = {
    task_id: TASK,
    state: "TASK_STATE_COMPLETED",
    last_updated: "2026-10-19T12:00:00Z",
    text: REPLY,
    status: { state: "TASK_STATE_COMPLETED" },
    artifacts: [{ parts: [{ text: REPLY }] }],
    history: [
      { role: "ROLE_USER", parts: [{ text: "Ask me which plan" }] },
      {
        role: "ROLE_AGENT",
        parts: [],
        metadata: {
          "https://proto-labs.ai/a2a/ext/tool-call-v1": { toolCallId: "c1", name: "request_user_input", phase: "started", args: "{}" },
        },
      },
      { role: "ROLE_USER", parts: [{ text: "Plan: Plan C" }], metadata: { hitl_resume: true, display: "Plan: Plan C" } },
    ],
  };
  await page.route("**/api/chat/sessions?*", (route) =>
    route.fulfill({ json: { sessions: [{ session_id: SESSION, last_updated: row.last_updated, turn_count: 1 }] } }),
  );
  await page.route("**/api/chat/sessions", (route) =>
    route.fulfill({ json: { sessions: [{ session_id: SESSION, last_updated: row.last_updated, turn_count: 1 }] } }),
  );
  const turnReads: string[] = [];
  await page.route(`**/api/chat/sessions/${SESSION}/turns*`, (route) => {
    turnReads.push(route.request().url());
    return route.fulfill({ json: { turns: [row], live_task_id: null } });
  });

  await page.goto("/app/", { waitUntil: "load" });
  const slot = page.locator(SLOT);
  await expect(slot.locator(".pl-message--user")).toHaveText([/Ask me which plan/, /Plan C/]);
  // Give boot hydration its chance to (wrongly) repair the question bubble, then reload once
  // more: the reply stays under the answer only.
  await page.waitForTimeout(1000);
  await page.reload({ waitUntil: "load" });
  await expect(slot.locator(".pl-message--user")).toHaveText([/Ask me which plan/, /Plan C/]);
  await page.waitForTimeout(1000);
  await expect(slot.locator(".pl-message--assistant").filter({ hasText: REPLY })).toHaveCount(1);
  await expect(slot.locator(".pl-message--assistant").last()).toContainText(REPLY);
  // Nothing about this healthy session asks for a durable repair, so boot never re-reads its
  // turns (on main the question bubble flagged it on every boot).
  expect(turnReads).toHaveLength(0);
});
