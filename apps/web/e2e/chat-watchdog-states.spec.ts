import { expect, test, type Page } from "@playwright/test";

// The stalled-stream watchdog in ChatSessionSlot (#3972). A live turn's stream that stops
// sending frames for WATCHDOG_IDLE_MS (45s) is checked against the durable task (GetTask);
// the watchdog's onTerminal then settles the bubble from it. These hold a turn open on the
// mock (its frames stop, the socket stays up), jump the page clock past the idle window,
// and read how the REAL compiled console settled the turn:
//   • a REJECTED task is a failure — the bubble and the session read "error", not "done";
//   • an UNSPECIFIED task is one no producer will ever move on — the live turn settles
//     instead of spinning "Working…" forever.

type Stored = { sessions: { messages: { role: string; status?: string; taskId?: string }[] }[] };

async function lastAssistant(page: Page) {
  return page.evaluate(() => {
    const raw = window.localStorage.getItem("protoagent.chat.sessions");
    const state = raw ? (JSON.parse(raw) as Stored) : null;
    const messages = (state?.sessions ?? []).flatMap((s) => s.messages);
    const last = [...messages].reverse().find((m) => m.role === "assistant");
    return last ? { status: last.status, taskId: last.taskId } : null;
  });
}

async function stallAndWait(page: Page, state: string) {
  await page.clock.install();
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await expect(composer).toBeVisible();
  await composer.fill(`hold the turn open, task ${state}`);
  await composer.press("Enter");
  await expect(page.getByRole("button", { name: "Stop" })).toBeVisible();
  // The turn's task id has landed on the bubble — the watchdog can consult it.
  await expect.poll(async () => (await lastAssistant(page))?.taskId).toBe(`task-e2e-held-${state}`);
  expect((await lastAssistant(page))?.status).toBe("streaming");
  const consulted = page.waitForRequest(
    (r) => r.url().endsWith("/a2a") && /"GetTask"/.test(r.postData() || "") && (r.postData() || "").includes(`held-${state}`),
  );
  await page.clock.fastForward(46_000); // past the 45s idle window, no frames in between
  await consulted;
}

test("a stalled live turn whose task was REJECTED settles as an error", async ({ page }) => {
  await stallAndWait(page, "rejected");
  await expect.poll(async () => (await lastAssistant(page))?.status).toBe("error");
  await expect(page.getByRole("button", { name: "Stop" })).toBeHidden();
  await expect(page.locator(".pl-message--assistant .spin")).toHaveCount(0);
});

test("a stalled live turn whose task state is UNSPECIFIED is settled by the watchdog, not left spinning", async ({
  page,
}) => {
  await stallAndWait(page, "unspecified");
  await expect.poll(async () => (await lastAssistant(page))?.status).toBe("done");
  await expect(page.getByRole("button", { name: "Stop" })).toBeHidden();
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeEnabled();
});
