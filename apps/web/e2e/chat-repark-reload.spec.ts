import { expect, test, type APIRequestContext, type Page, type Request } from "@playwright/test";

// #3963: a pause RE-PARKED by a plain message. A session is parked on an `ask_human`
// question; a message with no hitl_resume reaches it (another surface, or a console that
// had lost the form). The server holds it and parks the SAME question on a NEW task, then
// completes the old task with a pointer ("Continued in task …") just AFTER — so the old
// task changed last. Main ordered durable turns by last change: a fresh profile drew the
// completed turn as the latest one, and a warm tab reattached to the old task and settled
// it. Either way no form came back, and a composer reply was held and re-asked on yet
// another task — the answer was lost.
//
// The mock's re-park is the real server's shape (e2e/mock-server.mjs, supersededTasks), and
// its GET …/turns serves rows the way main did: by last change, no live marker. The console
// must bring the form back from the task that holds the pause, and the answer must resume
// THAT task.

const SLOT = ".chat-session-slot:not([hidden])";
const HELD = "Also end your next reply with the word HELD.";

type Body = { method: string; params: any };

function a2aLog(page: Page) {
  const bodies: Body[] = [];
  page.on("request", (req: Request) => {
    if (!req.url().endsWith("/a2a") || req.method() !== "POST") return;
    try {
      const body = JSON.parse(req.postData() || "{}");
      bodies.push({ method: body.method, params: body.params });
    } catch {
      /* not JSON */
    }
  });
  return bodies;
}

async function park(page: Page, bodies: Body[], label: string): Promise<string> {
  const composer = page.locator(`${SLOT} textarea.pl-prompt__field`);
  await composer.waitFor({ state: "visible" });
  await composer.fill(`PARK_ASK_HUMAN ${label}`);
  await composer.press("Enter");
  await expect(page.locator(`${SLOT} .hitl-float .hitl-card`)).toContainText(`Which fruit goes with ${label}?`);
  const sent = bodies.filter((b) => b.method === "SendStreamingMessage").at(-1);
  const sessionId = String(sent?.params?.message?.contextId ?? "");
  expect(sessionId).toMatch(/^chat-/);
  return sessionId;
}

/** A plain message into the parked session from ANOTHER surface (no hitl_resume, no task
 *  id). Returns the task the pause was re-parked on. */
async function plainMessage(request: APIRequestContext, sessionId: string): Promise<string> {
  const res = await request.post("/a2a", {
    headers: { "A2A-Version": "1.0" },
    data: {
      jsonrpc: "2.0",
      id: "other-surface",
      method: "SendStreamingMessage",
      params: {
        message: { role: "ROLE_USER", messageId: `m-${Date.now()}`, contextId: sessionId, parts: [{ text: HELD }] },
      },
    },
  });
  const successor = /"id":"(task-paused-ask_human-repark\d+-[^"]+)"/.exec(await res.text())?.[1] ?? "";
  expect(successor).toContain(sessionId);
  return successor;
}

/** The visible slot waits on the re-parked question — once, on the task that holds it. */
async function expectReparkedForm(page: Page, label: string) {
  await expect(page.locator(`${SLOT} .hitl-float .hitl-card`)).toContainText(`Which fruit goes with ${label}?`);
  await expect(page.locator(`${SLOT} .chat-paused-indicator`)).toHaveCount(1);
  // The chat reads in the order it happened: the question, then the held message.
  await expect(page.locator(`${SLOT} .pl-message--user`)).toHaveText([
    new RegExp(`PARK_ASK_HUMAN ${label}`),
    new RegExp(HELD),
  ]);
  await expect(page.locator(SLOT).getByRole("button", { name: "Stop", exact: true })).toHaveCount(0);
}

/** Answer the form; the answer must continue the re-parked task, and nothing is left waiting. */
async function answerResumes(page: Page, bodies: Body[], sessionId: string, successor: string, fruit: string) {
  const before = bodies.length;
  const card = page.locator(`${SLOT} .hitl-float .hitl-card`);
  await card.locator("textarea").fill(fruit);
  await card.getByRole("button", { name: "Send" }).click();
  await expect(card).toHaveCount(0);
  await expect.poll(() => bodies.slice(before).filter((b) => b.method === "SendStreamingMessage").length).toBe(1);
  const sent = bodies.slice(before).find((b) => b.method === "SendStreamingMessage")!;
  expect(sent.params.message.taskId).toBe(successor);
  expect(sent.params.message.contextId).toBe(sessionId);
  expect(sent.params.message.metadata?.hitl_resume).toBe(true);
  await expect(page.locator(`${SLOT} .pl-message--assistant`).last()).toContainText(
    `You like ${fruit}. (resumed ${successor} in ${sessionId})`,
  );
  await expect(page.locator(`${SLOT} .chat-paused-indicator`)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .tool-waiting`)).toHaveCount(0);
}

test("warm profile: a pause re-parked by a plain message comes back after a reload and the answer resumes it (#3963)", async ({
  page,
}) => {
  const bodies = a2aLog(page);
  await page.goto("/app/", { waitUntil: "load" });
  const sessionId = await park(page, bodies, "mango");
  const successor = await plainMessage(page.request, sessionId);

  // Same profile: the local transcript still names the OLD task. Its reattach finds it
  // superseded and must follow the pointer to the task that holds the pause.
  const subscribed = page.waitForRequest(
    (req) =>
      req.url().endsWith("/a2a") &&
      (req.postData() ?? "").includes("SubscribeToTask") &&
      (req.postData() ?? "").includes(successor),
  );
  await page.reload({ waitUntil: "load" });
  await subscribed;
  await expectReparkedForm(page, "mango");
  await answerResumes(page, bodies, sessionId, successor, "pear");
});

test("fresh profile: a pause re-parked by a plain message comes back from the durable turns and the answer resumes it (#3963)", async ({
  page,
  browser,
}) => {
  const first = a2aLog(page);
  await page.goto("/app/", { waitUntil: "load" });
  const sessionId = await park(page, first, "papaya");
  const successor = await plainMessage(page.request, sessionId);

  // A brand-new profile: nothing local, the chat is rebuilt from GET …/turns — served
  // parked-task FIRST, the superseded completion last.
  const context = await browser.newContext({ baseURL: test.info().project.use.baseURL });
  try {
    const fresh = await context.newPage();
    await fresh.setExtraHTTPHeaders({ "x-e2e-repark-session": sessionId });
    const bodies = a2aLog(fresh);
    const subscribed = fresh.waitForRequest(
      (req) =>
        req.url().endsWith("/a2a") &&
        (req.postData() ?? "").includes("SubscribeToTask") &&
        (req.postData() ?? "").includes(successor),
    );
    await fresh.goto("/app/", { waitUntil: "load" });
    await subscribed;
    await expectReparkedForm(fresh, "papaya");
    await answerResumes(fresh, bodies, sessionId, successor, "fig");
  } finally {
    await context.close();
  }
});
