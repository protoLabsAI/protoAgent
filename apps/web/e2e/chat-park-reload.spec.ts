import { expect, test, type Page, type Request } from "@playwright/test";

import { seedCurrentChat } from "./chat-helpers";

// #3956: a turn parked LIVE on an `ask_human` question, in the SAME browser profile.
//
// The SDK closes SendStreamingMessage when the task parks (input-required is an interrupted
// state), and the console settled that close like a finished turn: the in-flight `ask_human`
// card flipped to done ✓ while the form was still up, and the transcript it persisted said
// the turn was over. After a reload the warm local cache won (hydration skips a session it
// already has), no bubble was left streaming, so nothing reattached — no form, no waiting
// cue, and typing the answer in the composer became a held message while the question
// re-asked. A fresh profile was fine: cold hydration marks the same turn paused (#3946).
//
// The mock's "PARK_ASK_HUMAN <label>" parks exactly like the real server (tool START, then
// input-required with the question, then the stream closes); its task id is per session and
// SubscribeToTask serves it as the held-open parked snapshot. The answer echoes the task it
// continued, so the spec can see the RIGHT task resumed.

const SLOT = ".chat-session-slot:not([hidden])";

/** Every A2A request body the page sends, parsed. */
function a2aLog(page: Page) {
  const bodies: { method: string; params: any }[] = [];
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

async function send(page: Page, text: string) {
  const composer = page.locator(`${SLOT} textarea.pl-prompt__field`);
  await composer.waitFor({ state: "visible" });
  await composer.fill(text);
  await composer.press("Enter");
}

/** Send a parking prompt in the current tab; returns its session id (the message contextId). */
async function park(page: Page, bodies: { method: string; params: any }[], label: string): Promise<string> {
  await send(page, `PARK_ASK_HUMAN ${label}`);
  await expect(page.locator(`${SLOT} .hitl-float .hitl-card`)).toContainText(`Which fruit goes with ${label}?`);
  const sent = bodies.filter((b) => b.method === "SendStreamingMessage").at(-1);
  const sessionId = String(sent?.params?.message?.contextId ?? "");
  expect(sessionId).toMatch(/^chat-/);
  return sessionId;
}

const taskFor = (sessionId: string) => `task-paused-ask_human-live-${sessionId}`;

/** The visible slot reads as parked: the form is up and live, the ask_human card waits. */
async function expectWaiting(page: Page, label: string) {
  const card = page.locator(`${SLOT} .hitl-float .hitl-card`);
  await expect(card).toContainText(`Which fruit goes with ${label}?`);
  const tool = page.locator(`${SLOT} .pl-toolcard`).filter({ hasText: "ask_human" });
  await expect(tool).toHaveCount(1);
  await expect(tool.locator(".tool-waiting")).toContainText("waiting for you");
  await expect(tool.locator(".pl-toolcard__status--running")).toHaveCount(0);
  await expect(page.locator(`${SLOT} .chat-paused-indicator`)).toContainText("Waiting for your input");
  await expect(page.locator(`${SLOT} .chat-streaming-indicator`)).toHaveCount(0);
  await expect(page.locator(SLOT).getByRole("button", { name: "Stop", exact: true })).toHaveCount(0);
}

/** Answer the visible form; returns the SendStreamingMessage it produced. */
async function answer(page: Page, bodies: { method: string; params: any }[], text: string) {
  const before = bodies.length;
  const card = page.locator(`${SLOT} .hitl-float .hitl-card`);
  await card.locator("textarea").fill(text);
  await card.getByRole("button", { name: "Send" }).click();
  await expect(card).toHaveCount(0);
  await expect.poll(() => bodies.slice(before).find((b) => b.method === "SendStreamingMessage")).toBeTruthy();
  return bodies.slice(before).find((b) => b.method === "SendStreamingMessage")!;
}

test("a live park shows the ask_human card waiting, not done ✓ (#3956)", async ({ page }) => {
  const bodies = a2aLog(page);
  await page.goto("/app/", { waitUntil: "load" });
  await park(page, bodies, "cheese");
  await expectWaiting(page, "cheese");
  // The form's buttons are live: the session is not held "streaming".
  await expect(page.locator(`${SLOT} .hitl-float .hitl-card`).getByRole("button", { name: "Send" })).toBeVisible();
});

test("after a reload with warm local storage the form is back, and answering it resumes the right task (#3956)", async ({
  page,
}) => {
  const bodies = a2aLog(page);
  await page.goto("/app/", { waitUntil: "load" });
  const sessionId = await park(page, bodies, "crackers");

  // Same profile: localStorage keeps the transcript, so hydration leaves it alone and the
  // reattach is the only way back to the form.
  const subscribed = page.waitForRequest(
    (req) =>
      req.url().endsWith("/a2a") &&
      (req.postData() ?? "").includes("SubscribeToTask") &&
      (req.postData() ?? "").includes(taskFor(sessionId)),
  );
  await page.reload({ waitUntil: "load" });
  await subscribed;
  await expectWaiting(page, "crackers");

  const sent = await answer(page, bodies, "pear");
  expect(sent.params.message.taskId).toBe(taskFor(sessionId));
  expect(sent.params.message.contextId).toBe(sessionId);
  expect(sent.params.message.metadata?.hitl_resume).toBe(true);
  await expect(page.locator(`${SLOT} .pl-message--assistant`).last()).toContainText(
    `You like pear. (resumed ${taskFor(sessionId)} in ${sessionId})`,
  );
  // The paused bubble the answer continued is settled: no waiting cue is left behind.
  await expect(page.locator(`${SLOT} .chat-paused-indicator`)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .tool-waiting`)).toHaveCount(0);
});

test("two sessions parked at once each reattach to their own task after a reload (#3956)", async ({ page }) => {
  const bodies = a2aLog(page);
  await page.goto("/app/", { waitUntil: "load" });
  await seedCurrentChat(page); // "+" reuses a pristine tab, so the first one is used first
  await page.locator(".pl-tabbar__add:visible").click();
  const first = await park(page, bodies, "brie");
  await page.locator(".pl-tabbar__add:visible").click();
  const second = await park(page, bodies, "gouda");
  expect(first).not.toBe(second);

  const subscriptions = () =>
    bodies.filter((b) => b.method === "SubscribeToTask").map((b) => String(b.params?.id ?? ""));
  const before = subscriptions().length;
  await page.reload({ waitUntil: "load" });
  // Both parked tabs mount and resubscribe — each to its OWN task, never the other's.
  await expect
    .poll(() => new Set(subscriptions().slice(before)))
    .toEqual(new Set([taskFor(first), taskFor(second)]));

  // The focused tab (the second) shows its own question…
  await expectWaiting(page, "gouda");
  const sentSecond = await answer(page, bodies, "fig");
  expect(sentSecond.params.message.taskId).toBe(taskFor(second));
  expect(sentSecond.params.message.contextId).toBe(second);
  await expect(page.locator(`${SLOT} .pl-message--assistant`).last()).toContainText(
    `You like fig. (resumed ${taskFor(second)} in ${second})`,
  );

  // …and the first tab still holds ITS question, answered onto ITS task.
  await page.locator(".pl-tabbar__tab").filter({ hasText: "PARK_ASK_HUMAN brie" }).click();
  await expectWaiting(page, "brie");
  const sentFirst = await answer(page, bodies, "grape");
  expect(sentFirst.params.message.taskId).toBe(taskFor(first));
  expect(sentFirst.params.message.contextId).toBe(first);
  await expect(page.locator(`${SLOT} .pl-message--assistant`).last()).toContainText(
    `You like grape. (resumed ${taskFor(first)} in ${first})`,
  );
});

// #3956 review: the stream can fail AFTER the park — a dropped socket, or an error frame
// behind the input-required one. The task is still parked server-side, so the turn must
// settle as parked (not as an error): waiting now, the form back after a reload, and the
// answer continuing the parked task with no card left waiting behind it.
for (const mode of ["DROPNET", "ERRFRAME"]) {
  test(`a stream that fails after the park (${mode}) still settles as parked (#3956)`, async ({ page }) => {
    const bodies = a2aLog(page);
    await page.goto("/app/", { waitUntil: "load" });
    const sessionId = await park(page, bodies, mode);
    await expectWaiting(page, mode);
    // Not an error: no error bubble, the session is not in its error state.
    await expect(page.locator(`${SLOT} .pl-message--assistant`).last()).not.toContainText("stream failed");
    // Persisted as parked — the shape a reload reattaches through (the store writes on a
    // debounce, so poll until the settle lands).
    await expect
      .poll(() =>
        page.evaluate(
          (id) =>
            JSON.parse(localStorage.getItem("protoagent.chat.sessions") || "{}")
              .sessions.find((s: { id: string }) => s.id === id)
              .messages.filter((m: { role: string }) => m.role === "assistant")
              .map((m: { status: string; paused?: boolean; taskId?: string }) => [m.status, m.paused, m.taskId]),
          sessionId,
        ),
      )
      .toEqual([["streaming", true, taskFor(sessionId)]]);

    await page.reload({ waitUntil: "load" });
    await expectWaiting(page, mode);
    const sent = await answer(page, bodies, "kiwi");
    expect(sent.params.message.taskId).toBe(taskFor(sessionId));
    await expect(page.locator(`${SLOT} .pl-message--assistant`).last()).toContainText("You like kiwi.");
    await expect(page.locator(`${SLOT} .tool-waiting`)).toHaveCount(0);
    await expect(page.locator(`${SLOT} .chat-paused-indicator`)).toHaveCount(0);
  });
}

test("a plugin composer form on the stream does not leave the turn paused (#3956)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await send(page, "PARK_ASK_HUMAN PLUGINFORM");
  await expect(page.locator(`${SLOT} .hitl-float .hitl-card`)).toContainText("Plugin form question?");
  // It parks no graph: the bubble settles as before — no waiting cue, nothing to reattach.
  await expect(page.locator(`${SLOT} .chat-streaming-indicator`)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .chat-paused-indicator`)).toHaveCount(0);
  // The store persists on a debounce: poll until the settled bubble lands.
  await expect
    .poll(() =>
      page.evaluate(() =>
        JSON.parse(localStorage.getItem("protoagent.chat.sessions") || "{}")
          .sessions.flatMap((s: { messages: { role: string; status: string; paused?: boolean }[] }) => s.messages)
          .filter((m: { role: string }) => m.role === "assistant")
          .map((m: { status: string; paused?: boolean }) => [m.status, m.paused ?? null]),
      ),
    )
    .toEqual([["done", null]]);
});
