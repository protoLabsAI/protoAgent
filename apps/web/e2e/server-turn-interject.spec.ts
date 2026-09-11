import { expect, test, type Page, type Request } from "@playwright/test";

// An interjection typed into an ATTENDED server-fired turn (a background push-resume, a
// scheduled fire, a watch reaction) must behave like the steer it is.
//
// The live bug (v0.164.0, jobCoach): the operator typed "yes 2024 as proposed" while the
// agent was "responding to background reports…". The server queued it, the next model
// call folded it in and the agent's reply used it — but the console never heard that it
// had been consumed. The bubble sat below the answer as "queued interjection — sent to
// this server turn" for the rest of the turn, with no ✕, and when the turn ended it
// silently vanished instead of settling into the transcript.
//
// The server now republishes the steer-consumed boundary for server-fired turns as a
// `chat.progress` frame (the same boundary a browser-owned stream carries inline). These
// specs drive that wire shape through the real compiled console:
//
//   1. the consumed marker settles the bubble as an ordinary user message at the boundary
//      the agent actually read it, and the settled turn keeps it there;
//   2. a MISSED marker (live-only frames are dropped across an SSE reconnect) still settles
//      the bubble at turn end, off the server's pending queue;
//   3. ✕ on a still-pending interjection takes it back out of the queue — never delivered;
//   4. an interjection the turn never reached is sent as a normal message when it ends.
//
// Harness: the bus is scripted per SSE connection (the pattern server-turn-stream.spec.ts
// established) and each phase is released by the spec once its precondition is observable,
// so nothing depends on machine speed. The interject/steer routes are answered here too,
// which keeps every piece of per-test state inside this page — the shared mock server is
// never asked to remember anything.

const TASK = "task-interject-e2e";
const ORIGIN = "background-resume";
const PRE = "Checked the PR diff before you replied.";
const POST = "Locked Dec 2024 into your profile across every surface.";
const FINAL = `${PRE}\n\n${POST}`;
const INTERJECTION = "yes 2024 as proposed";
const SLOT = ".chat-session-slot:not([hidden])";

type Frame = { topic: string; data: Record<string, unknown> };

/** Block a route handler until `ready`, but never past `budgetMs` — a failing run must fail
 *  on its own assertion, not hang on a flag it never got to set. */
async function until(ready: () => boolean, budgetMs = 15_000): Promise<void> {
  const deadline = Date.now() + budgetMs;
  while (!ready() && Date.now() < deadline) await new Promise((r) => setTimeout(r, 25));
}

function sse(frames: Frame[]): string {
  return frames.length ? frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("") : ": idle\n\n";
}

function control(session: string) {
  return {
    session_id: session,
    task_id: TASK,
    origin: ORIGIN,
    trigger: "bg-1",
    controllable: true,
    operator_controllable: true,
  };
}

function progress(session: string, data: Record<string, unknown>): Frame {
  return { topic: "chat.progress", data: { session_id: session, task_id: TASK, ...data, control: control(session) } };
}

/** The turn is running: indicator, the attended control contract, and narration so far. */
function liveFrames(session: string): Frame[] {
  return [
    { topic: "turn.started", data: { session_id: session, origin: ORIGIN, trigger: "bg-1" } },
    progress(session, { phase: "turn_started" }),
    progress(session, { phase: "text", text: PRE }),
  ];
}

/** The terminal pair, in the order the server emits them. */
function terminalFrames(session: string): Frame[] {
  return [
    { topic: "chat.resumed", data: { session_id: session, task_id: TASK, text: FINAL, state: "completed", origin: ORIGIN } },
    { topic: "turn.finished", data: { session_id: session, origin: ORIGIN, trigger: "bg-1" } },
  ];
}

type Harness = {
  /** Release the next scripted bus connection with these frames. */
  release: (frames: Frame[]) => void;
  /** The interjection the console posted to the server turn, once it has. */
  interjected: () => { id: string; text: string } | null;
  /** What `GET …/steer` answers — the server's still-queued items. */
  setPending: (items: { id: string; text: string }[]) => void;
  deletes: string[];
  a2aSends: string[];
  /** Dequeues and sends, in the order the console made them. */
  order: string[];
};

async function openAttendedServerTurn(page: Page, session: string): Promise<Harness> {
  await page.addInitScript(
    ([id]) => {
      window.localStorage.setItem(
        "protoagent.chat.sessions",
        JSON.stringify({
          version: 1,
          currentSessionId: id,
          sessions: [
            {
              id,
              title: "job search",
              createdAt: 1,
              updatedAt: 2,
              messages: [
                { id: "u-prior", role: "user", content: "Which start date should the site use?", createdAt: 1, status: "done" },
                { id: "a-prior", role: "assistant", content: "Dec 2024 everywhere, as proposed?", createdAt: 2, status: "done" },
              ],
            },
          ],
        }),
      );
    },
    [session],
  );

  // Connection n serves released phase n. The console holds one EventSource at a time and
  // reconnects when a body ends, so each phase lands on its own connection, in order; a
  // connection past the script idles out (the console just reconnects), never replays.
  const phases: Frame[][] = [];
  let connections = 0;
  let posted: { id: string; text: string } | null = null;
  let pending: { id: string; text: string }[] = [];
  const deletes: string[] = [];
  const a2aSends: string[] = [];
  const order: string[] = [];

  await page.route("**/api/events**", async (route) => {
    const n = connections++;
    await until(() => phases.length > n, 20_000);
    await route
      .fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
        body: sse(phases[n] ?? []),
      })
      .catch(() => {});
  });
  await page.route("**/api/chat/sessions/*/server-turns/*/interject", async (route) => {
    const body = route.request().postDataJSON() as { id: string; text: string };
    posted = { id: body.id, text: body.text };
    pending = [...pending, posted];
    await route.fulfill({ json: { ok: true, id: body.id, pending: pending.length } });
  });
  await page.route("**/api/chat/sessions/*/steer", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({ json: { pending } });
  });
  await page.route("**/api/chat/sessions/*/steer/*", async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    const id = decodeURIComponent(route.request().url().split("/").pop() ?? "");
    deletes.push(id);
    order.push("dequeue");
    const removed = pending.some((item) => item.id === id);
    pending = pending.filter((item) => item.id !== id);
    await route.fulfill({ json: { removed, pending: pending.length } });
  });
  page.on("request", (req: Request) => {
    if (!req.url().endsWith("/a2a") || req.method() !== "POST") return;
    const body = req.postData() ?? "";
    if (body.includes("SendStreamingMessage") || body.includes("SendMessage")) {
      a2aSends.push(body);
      order.push("send");
    }
  });

  const release = (frames: Frame[]) => {
    phases.push(frames);
  };

  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(`${SLOT} .pl-prompt__field`).waitFor({ state: "visible" });
  release(liveFrames(session));
  await expect(page.locator(SLOT).getByText(PRE)).toBeVisible();
  // The attended control contract arrived: Enter now interjects instead of sending.
  await expect(page.getByPlaceholder(/Interject into the running server task/i)).toBeVisible();

  return {
    release,
    interjected: () => posted,
    setPending: (items) => {
      pending = items;
    },
    deletes,
    a2aSends,
    order,
  };
}

async function interject(page: Page, h: Harness) {
  const field = page.locator(`${SLOT} .pl-prompt__field`);
  await field.fill(INTERJECTION);
  await field.press("Enter");
  const queued = page.locator(`${SLOT} .pl-message--queued`);
  await expect(queued).toHaveCount(1);
  await expect(queued).toContainText(INTERJECTION);
  await expect(queued).toContainText(/queued interjection/i);
  await expect.poll(() => h.interjected()?.text).toBe(INTERJECTION);
  return h.interjected()!;
}

/** Visible transcript rows, top to bottom, as their text. */
async function rows(page: Page): Promise<string[]> {
  return page.locator(`${SLOT} .pl-message`).evaluateAll((els) => els.map((el) => (el as HTMLElement).innerText));
}

function indexOf(list: string[], needle: string): number {
  return list.findIndex((row) => row.includes(needle));
}

/** User messages carrying the interjection in the persisted store (display state only). */
async function persistedInterjections(page: Page, session: string): Promise<number> {
  return page.evaluate(
    ([id, text]) => {
      const raw = window.localStorage.getItem("protoagent.chat.sessions");
      const state = raw ? (JSON.parse(raw) as { sessions: { id: string; messages: { role: string; content: string }[] }[] }) : null;
      const row = state?.sessions.find((s) => s.id === id);
      return row ? row.messages.filter((m) => m.role === "user" && m.content === text).length : -1;
    },
    [session, INTERJECTION] as const,
  );
}

test("the server's consumed marker settles a queued interjection at the boundary it was read", async ({ page }) => {
  const session = "chat-interject-acked";
  const h = await openAttendedServerTurn(page, session);
  const sent = await interject(page, h);

  // The server folds it in at the next model call and says so on the bus — then the agent
  // keeps working, now with the operator's answer in hand.
  h.setPending([]);
  h.release([
    progress(session, { phase: "steer_consumed", items: [{ id: sent.id, text: sent.text }] }),
    progress(session, { phase: "text", text: POST }),
  ]);

  // THE BUG: the bubble stayed queued here, below an answer that had already used it.
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  const user = page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION });
  await expect(user).toHaveCount(1);
  await expect(page.locator(SLOT).getByText(POST)).toBeVisible();
  // …and in the right place: after what the agent had said, before what it said next.
  const live = await rows(page);
  expect(indexOf(live, PRE)).toBeLessThan(indexOf(live, INTERJECTION));
  expect(indexOf(live, INTERJECTION)).toBeLessThan(indexOf(live, POST));

  // The turn settles. The operator's message stays exactly where it was, exactly once —
  // the authoritative final text is distributed across the split, not landed twice.
  h.release(terminalFrames(session));
  await expect(page.getByText(/responding to background reports/i)).toHaveCount(0);
  const cards = page.locator(`${SLOT} .chat-server-result`);
  await expect(cards).toHaveCount(2);
  await expect(cards.first().locator(".chat-server-result-preview")).toContainText(PRE);
  await expect(cards.last().locator(".chat-server-result-preview")).toContainText(POST);
  await expect(cards.last().locator(".chat-server-result-preview")).not.toContainText(PRE);
  await expect(user).toHaveCount(1);
  const settled = await rows(page);
  expect(indexOf(settled, PRE)).toBeLessThan(indexOf(settled, INTERJECTION));
  expect(indexOf(settled, INTERJECTION)).toBeLessThan(indexOf(settled, POST));
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect.poll(() => persistedInterjections(page, session)).toBe(1);
  // Nothing was re-sent: the agent already had it.
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);
});

test("a missed consumed marker still settles the interjection when the turn ends", async ({ page }) => {
  const session = "chat-interject-missed";
  const h = await openAttendedServerTurn(page, session);
  await interject(page, h);

  // The marker rode a live-only frame the console never saw; the server's queue no
  // longer holds the message, which is the durable proof it was folded in.
  h.setPending([]);
  h.release(terminalFrames(session));

  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  const user = page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION });
  await expect(user).toHaveCount(1);
  // No boundary to honor, so it lands on the conservative side: above the reply it shaped.
  const settled = await rows(page);
  const card = settled.findIndex((row) => /background report/i.test(row));
  expect(card).toBeGreaterThan(-1);
  expect(indexOf(settled, INTERJECTION)).toBeLessThan(card);
  await expect.poll(() => persistedInterjections(page, session)).toBe(1);
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);
});

test("✕ on a pending interjection takes it back out of the server queue", async ({ page }) => {
  const session = "chat-interject-cancel";
  const h = await openAttendedServerTurn(page, session);
  const sent = await interject(page, h);

  // THE BUG: there was no ✕ at all on a server-turn interjection.
  await page.locator(`${SLOT}`).getByRole("button", { name: "Cancel queued message" }).click();
  await expect.poll(() => h.deletes).toEqual([sent.id]);
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);

  // The turn ends. A cancelled message is not delivered late, and not re-sent either.
  h.release(terminalFrames(session));
  await expect(page.locator(`${SLOT} .chat-server-result`)).toHaveCount(1);
  await expect(page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION })).toHaveCount(0);
  await page.waitForTimeout(500);
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);
  await expect.poll(() => persistedInterjections(page, session)).toBe(0);
});

test("an interjection the server turn never reached is sent as a normal message when it ends", async ({ page }) => {
  const session = "chat-interject-late";
  const h = await openAttendedServerTurn(page, session);
  const sent = await interject(page, h);

  // The turn's last model call had already run: the message is still in the server's
  // queue when the turn finishes. Dropping the bubble left it to ride some later turn,
  // unseen; it has to reach the agent now, as the operator's next message.
  h.release(terminalFrames(session));

  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  // Pulled out of the server queue FIRST, so the agent can't read it twice — then sent as
  // an ordinary turn.
  await expect.poll(() => h.a2aSends.filter((body) => body.includes(INTERJECTION)).length).toBe(1);
  expect(h.deletes).toEqual([sent.id]);
  expect(h.order).toEqual(["dequeue", "send"]);
  const user = page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION });
  await expect(user).toHaveCount(1);
  const settled = await rows(page);
  const card = settled.findIndex((row) => /background report/i.test(row));
  expect(card).toBeGreaterThan(-1);
  expect(indexOf(settled, INTERJECTION)).toBeGreaterThan(card);
});
