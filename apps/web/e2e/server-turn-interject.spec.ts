import { expect, test, type Page, type Request, type Route } from "@playwright/test";

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
const TASK2 = "task-interject-e2e-2";
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

function control(session: string, task = TASK) {
  return {
    session_id: session,
    task_id: task,
    origin: ORIGIN,
    trigger: "bg-1",
    controllable: true,
    operator_controllable: true,
  };
}

function progress(session: string, data: Record<string, unknown>, task = TASK): Frame {
  return {
    topic: "chat.progress",
    data: { session_id: session, task_id: task, ...data, control: control(session, task) },
  };
}

/** The turn is running: indicator, the attended control contract, narration, and the tool it
 *  ran — the tool matters because #3443 settles this turn IN PLACE, keeping the streamed
 *  order, and a split must not undo that. */
function liveFrames(session: string): Frame[] {
  return [
    { topic: "turn.started", data: { session_id: session, origin: ORIGIN, trigger: "bg-1" } },
    progress(session, { phase: "turn_started" }),
    progress(session, { phase: "text", text: PRE }),
    progress(session, { phase: "tool_start", tool: "github_pr_diff", tool_call_id: "tc1" }),
    progress(session, { phase: "tool_end", tool: "github_pr_diff", tool_call_id: "tc1", output: "clean" }),
  ];
}

/** The terminal pair, in the order the server emits them. `addressed` mirrors a server that
 *  stamps the task id on `turn.finished`; without it (an older server / an unreadable reply)
 *  the console has to fall back on its own in-flight count. */
function terminalFrames(session: string, task = TASK, addressed = false): Frame[] {
  return [
    { topic: "chat.resumed", data: { session_id: session, task_id: task, text: FINAL, state: "completed", origin: ORIGIN } },
    {
      topic: "turn.finished",
      data: { session_id: session, origin: ORIGIN, trigger: "bg-1", ...(addressed ? { task_id: task } : {}) },
    },
  ];
}

type Harness = {
  /** Release the next scripted bus connection with these frames. */
  release: (frames: Frame[]) => void;
  /** The interjection the console posted to the server turn, once it has. */
  interjected: () => { id: string; text: string } | null;
  /** What `GET …/steer` answers — the server's still-queued items. */
  setPending: (items: { id: string; text: string }[]) => void;
  /** Hold interject POSTs unanswered (the slow-link / turn-end race window). */
  holdInterject: (on: boolean) => void;
  heldInterjects: () => number;
  releaseInterjects: (reply: (body: { id: string; text: string }) => Record<string, unknown>) => Promise<void>;
  /** Hold `GET …/steer` so the reconcile can't resolve anything while a test acts. */
  holdSteerReads: (on: boolean) => void;
  /** Force every `DELETE …/steer/{id}` to answer `removed: false` (the agent got there
   *  first), whatever the mock's own queue says. */
  forceDeleteNotRemoved: (on: boolean) => void;
  /** Per-task `GetTask` state (default: completed) — what the reconcile reads to decide
   *  whether the turn an interjection was sent to can still reach it. */
  taskState: Map<string, string>;
  /** Ids a task's DURABLE history records as folded in (steer-consumed-v1). */
  taskConsumed: Map<string, string[]>;
  /** Fail the next N `GET …/steer` reads with a 500. */
  failSteerReads: (n: number) => void;
  steerReads: () => number;
  /** Ids the server reports as FOLDED IN (`GET …/steer`'s `drained`) — the record that
   *  outlives the queue and tells "the agent read it" from "it never arrived". */
  setDrained: (ids: string[]) => void;
  /** Fail the next N `DELETE …/steer/{id}` calls. */
  failDeletes: (n: number) => void;
  /** Kill a held interject POST so no answer ever comes back (the bubble stays unconfirmed). */
  abortInterjects: () => Promise<void>;
  /** Make this id's DELETE answer `removed: false` AND appear in `drained` — the server
   *  folded it in between the reconcile's read and its dequeue. */
  drainOnDelete: (id: string) => void;
  drained: () => string[];
  /** Reload the tab. Use this, not `page.reload`: it retires the bus connections the old
   *  document opened, so none of them can take a phase meant for the reloaded console. */
  reload: () => Promise<void>;
  /** Hold `SubscribeToTask` unanswered — a reattach's resubscribe that is slow to come back
   *  (a loaded box, a cold member behind the fleet proxy). */
  holdResubscribe: (on: boolean) => void;
  heldResubscribes: () => number;
  /** Resubscribes the console gave up on (the request was aborted from the page side). */
  resubscribeAborts: () => number;
  deletes: string[];
  a2aSends: string[];
  /** Dequeues and sends, in the order the console made them. */
  order: string[];
};

async function openAttendedServerTurn(page: Page, session: string): Promise<Harness> {
  await page.addInitScript(
    ([id]) => {
      // Seed ONCE per tab: a reload must keep what the console persisted, not a fresh copy.
      if (window.sessionStorage.getItem("interject-seeded")) return;
      window.sessionStorage.setItem("interject-seeded", "1");
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

  // Released phases are served in order, one per bus connection. The console holds one
  // EventSource at a time and reconnects when a body ends, so each phase lands on its own
  // connection; a connection with nothing released idles out (the console just reconnects),
  // never replays. A connection opened before the last `reload()` is dropped UNSERVED: its
  // document is gone, so a phase handed to it is swallowed — and whether the old tab's 1s
  // reconnect beat the reload would decide what the reloaded console gets to see.
  const phases: Frame[][] = [];
  let served = 0;
  let generation = 0;
  let posted: { id: string; text: string } | null = null;
  let pending: { id: string; text: string }[] = [];
  const deletes: string[] = [];
  const a2aSends: string[] = [];
  const order: string[] = [];

  await page.route("**/api/events**", async (route) => {
    const gen = generation;
    await until(() => phases.length > served || gen !== generation, 20_000);
    if (gen !== generation) return route.abort().catch(() => {});
    const frames = phases.length > served ? phases[served++] : [];
    await route
      .fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
        body: sse(frames),
      })
      .catch(() => {});
  });
  let holdI = false;
  const heldI: { route: Route; body: { id: string; text: string } }[] = [];
  let holdReads = false;
  let denyRemoval = false;
  let steerFailures = 0;
  let steerReads = 0;
  let deleteFailures = 0;
  let drained: string[] = [];
  const drainDuringDelete = new Set<string>();
  const taskState = new Map<string, string>();
  const taskConsumed = new Map<string, string[]>();
  let holdResub = false;
  const heldResub: Route[] = [];
  let resubAborts = 0;
  await page.route("**/api/chat/sessions/*/server-turns/*/interject", async (route) => {
    const body = route.request().postDataJSON() as { id: string; text: string };
    posted = { id: body.id, text: body.text };
    if (holdI) {
      heldI.push({ route, body });
      return;
    }
    pending = [...pending, { id: body.id, text: body.text }];
    await route.fulfill({ json: { ok: true, id: body.id, pending: pending.length } });
  });
  await page.route("**/api/chat/sessions/*/steer", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await until(() => !holdReads, 20_000);
    steerReads += 1;
    if (steerFailures > 0) {
      steerFailures -= 1;
      return route.fulfill({ status: 500, json: { detail: "transient" } }).catch(() => {});
    }
    await route.fulfill({ json: { pending, drained } }).catch(() => {});
  });
  // GetTask: the durable task the reconcile consults — its state, and the steer-consumed
  // markers its history carries.
  await page.route("**/a2a", async (route) => {
    const body = route.request().postDataJSON() as { id?: unknown; method?: string; params?: { id?: string } } | null;
    if (body?.method === "SubscribeToTask" && holdResub) {
      heldResub.push(route); // never answered: only the console giving up ends this request
      return;
    }
    if (body?.method !== "GetTask") return route.fallback();
    const id = String(body.params?.id ?? "");
    const consumed = taskConsumed.get(id) ?? [];
    await route
      .fulfill({
        json: {
          jsonrpc: "2.0",
          id: body.id,
          result: {
            id,
            contextId: session,
            status: { state: taskState.get(id) ?? "TASK_STATE_COMPLETED" },
            artifacts: [],
            history: consumed.map((steerId) => ({
              role: "ROLE_AGENT",
              parts: [
                {
                  data: { items: [{ id: steerId, text: INTERJECTION }] },
                  metadata: { mimeType: "application/vnd.protolabs.steer-consumed-v1+json" },
                },
              ],
            })),
          },
        },
      })
      .catch(() => {});
  });
  await page.route("**/api/chat/sessions/*/steer/*", async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    const id = decodeURIComponent(route.request().url().split("/").pop() ?? "");
    if (deleteFailures > 0) {
      deleteFailures -= 1;
      return route.fulfill({ status: 500, json: { detail: "transient" } }).catch(() => {});
    }
    deletes.push(id);
    order.push("dequeue");
    if (drainDuringDelete.has(id)) {
      // A turn folded it in just after the read that said "not queued, not drained".
      drained = [...drained, id];
      pending = pending.filter((item) => item.id !== id);
      return route.fulfill({ json: { removed: false, pending: pending.length } }).catch(() => {});
    }
    const removed = !denyRemoval && pending.some((item) => item.id === id);
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
  page.on("requestfailed", (req: Request) => {
    if (req.url().endsWith("/a2a") && (req.postData() ?? "").includes("SubscribeToTask")) resubAborts += 1;
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
    holdInterject: (on) => {
      holdI = on;
    },
    heldInterjects: () => heldI.length,
    releaseInterjects: async (reply) => {
      for (const { route, body } of heldI.splice(0)) {
        const answer = reply(body);
        if (answer.ok) pending = [...pending, { id: body.id, text: body.text }];
        await route.fulfill({ json: answer }).catch(() => {});
      }
    },
    holdSteerReads: (on) => {
      holdReads = on;
    },
    forceDeleteNotRemoved: (on) => {
      denyRemoval = on;
    },
    taskState,
    taskConsumed,
    failSteerReads: (n) => {
      steerFailures = n;
    },
    steerReads: () => steerReads,
    setDrained: (ids) => {
      drained = ids;
    },
    failDeletes: (n) => {
      deleteFailures = n;
    },
    abortInterjects: async () => {
      for (const { route } of heldI.splice(0)) await route.abort("failed").catch(() => {});
    },
    drainOnDelete: (id) => {
      drainDuringDelete.add(id);
    },
    drained: () => drained,
    reload: async () => {
      await page.reload({ waitUntil: "load" });
      // Only now is the old document gone for good, so every connection opened before this
      // point is stale. At worst that includes the reloaded page's own first one, which costs
      // it a 1s reconnect and never a frame.
      generation += 1;
    },
    holdResubscribe: (on) => {
      holdResub = on;
    },
    heldResubscribes: () => heldResub.length,
    resubscribeAborts: () => resubAborts,
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

/** Document order of the narration, the tool card it ran, and the operator's message — the
 *  #3443 no-reflow check, which a split turn has to keep too. */
async function documentOrder(page: Page, texts: string[]): Promise<number[]> {
  return page.evaluate((needles) => {
    const all = [...document.querySelectorAll("*")];
    return needles.map((needle) =>
      needle === "@tool"
        ? all.findIndex((el) => el.classList.contains("pl-toolcard") && (el.textContent ?? "").includes("github_pr_diff"))
        : all.findIndex((el) => el.children.length === 0 && (el.textContent ?? "").includes(needle)),
    );
  }, texts);
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

  // Retired from the slot's QUEUE, not merely hidden from the rendered list — and while the
  // turn is still running, the slot's own retire is the only thing that can have done it. ↑
  // on an empty composer is the probe: a still-queued item takes the pull-it-back-out path
  // (a DELETE) instead of history recall, so anything that still thought this message was
  // pending — ↑, Escape — would be acting on one the agent has already read. (The recalled
  // TEXT can't tell them apart: the input-history ring holds it either way.)
  await page.locator(`${SLOT} .pl-prompt__field`).press("ArrowUp");
  await page.waitForTimeout(300);
  expect(h.deletes, "↑ must not try to dequeue a message that already landed").toEqual([]);
  await page.locator(`${SLOT} .pl-prompt__field`).fill("");

  // The turn settles. A reply to background reports settles IN PLACE (#3443) — no card, no
  // re-flow — and the split has to keep that: the operator's message stays exactly where it
  // was, exactly once, with the authoritative text distributed across the split rather than
  // landed twice, and the tool card still above the narration that followed it.
  const liveOrder = await documentOrder(page, [PRE, "@tool", INTERJECTION, POST]);
  h.release(terminalFrames(session));
  await expect(page.locator(".pl-toast", { hasText: "Task resumed" })).toBeVisible();
  await expect(page.getByText(/responding to background reports/i)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .chat-server-result`)).toHaveCount(0);
  await expect(page.locator(SLOT).getByText(PRE)).toBeVisible();
  await expect(page.locator(SLOT).getByText(POST)).toBeVisible();
  await expect(page.locator(".pl-toolcard").filter({ hasText: "github_pr_diff" }).first()).toBeVisible();
  await expect(user).toHaveCount(1);
  const settledOrder = await documentOrder(page, [PRE, "@tool", INTERJECTION, POST]);
  expect(settledOrder.every((i) => i > -1)).toBe(true);
  expect(settledOrder).toEqual([...settledOrder].sort((a, b) => a - b));
  expect(liveOrder.every((i) => i > -1)).toBe(true); // the live view had the same order
  const settled = await rows(page);
  expect(settled.filter((row) => row.includes(PRE))).toHaveLength(1); // landed once, not twice
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
  const reply = indexOf(settled, POST);
  expect(reply).toBeGreaterThan(-1);
  expect(indexOf(settled, INTERJECTION)).toBeLessThan(reply);
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
  await expect(page.locator(SLOT).getByText(POST)).toBeVisible();
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
  // It is a fresh message, so it reads BELOW the reply that never included it.
  const settled = await rows(page);
  const reply = indexOf(settled, POST);
  expect(reply).toBeGreaterThan(-1);
  expect(indexOf(settled, INTERJECTION)).toBeGreaterThan(reply);
});

test("an interjection still in flight when the turn ends is left alone until the server answers", async ({ page }) => {
  const session = "chat-interject-inflight";
  const h = await openAttendedServerTurn(page, session);
  // The POST is slow (a remote member, a loaded box). The turn ends underneath it.
  h.holdInterject(true);
  const field = page.locator(`${SLOT} .pl-prompt__field`);
  await field.fill(INTERJECTION);
  await field.press("Enter");
  await expect.poll(() => h.heldInterjects()).toBe(1);
  h.release(terminalFrames(session));
  await expect(page.getByText(/responding to background reports/i)).toHaveCount(0);

  // The server has not said whether it queued it, so its absence from the queue means
  // NOTHING yet: settling it would claim the agent read it, re-sending could double it,
  // and handing the words back would deny a message the agent is about to read. Waited out
  // past the reconcile's own re-check ladder — an in-flight submission is not "unresolved",
  // it is unanswered, and the two must not be confused however long it takes.
  await page.waitForTimeout(6_000);
  expect(h.deletes, "nothing may be dequeued while the submission is unanswered").toEqual([]);
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(1);
  expect(await page.locator(`${SLOT} .pl-prompt__field`).inputValue()).toBe("");

  // Answered at last, and accepted: now it can be resolved — the turn is over and nothing
  // else will drain it, so it goes as the operator's next message.
  await h.releaseInterjects((body) => ({ ok: true, id: body.id, pending: 1 }));
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect.poll(() => h.deletes.length).toBe(1);
  await expect.poll(() => h.a2aSends.filter((body) => body.includes(INTERJECTION)).length).toBe(1);
});

test("a refused interjection is never lost: its words come back, and nothing is delivered", async ({ page }) => {
  const session = "chat-interject-refused";
  const h = await openAttendedServerTurn(page, session);
  h.holdInterject(true);
  const field = page.locator(`${SLOT} .pl-prompt__field`);
  await field.fill(INTERJECTION);
  await field.press("Enter");
  await expect.poll(() => h.heldInterjects()).toBe(1);
  // The operator keeps typing while it is in flight — their in-hand draft must survive too.
  await field.fill("and fix the résumé date as well");
  // The turn ended between its last control frame and this POST, so the server refuses it:
  // nothing was queued, and nothing will ever settle this bubble.
  await h.releaseInterjects(() => ({ ok: false, reason: "not_live", pending: 0 }));

  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect(page.locator(".pl-toast--error")).toContainText(/wasn't sent/i);
  const draft = await field.inputValue();
  expect(draft).toContain(INTERJECTION); // the refused words
  expect(draft).toContain("and fix the résumé date as well"); // and the in-hand ones
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);
  // The composer is back to ordinary sending: the server said that task is gone.
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("Stop settles what the agent had already read and hands back what it hadn't", async ({ page }) => {
  const session = "chat-interject-stop";
  const h = await openAttendedServerTurn(page, session);
  const sent = await interject(page, h);

  await page.locator(SLOT).getByRole("button", { name: /stop/i }).first().click();
  // The bubble goes at once, but the SERVER's copy is not left behind to ride the next turn.
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect.poll(() => h.deletes).toEqual([sent.id]);
  // It was never read, so the words are the operator's again — not silently discarded.
  await expect.poll(() => page.locator(`${SLOT} .pl-prompt__field`).inputValue()).toContain(INTERJECTION);
  await expect(page.locator(".pl-toast--error")).toContainText(/never sent/i);
});

test("✕ after the turn ended settles an interjection the agent had already read", async ({ page }) => {
  const session = "chat-interject-cancel-late";
  const h = await openAttendedServerTurn(page, session);
  await interject(page, h);

  // The turn ends; hold the reconcile's queue read so the ✕ is what resolves this.
  h.holdSteerReads(true);
  h.release(terminalFrames(session));
  await expect(page.getByText(/responding to background reports/i)).toHaveCount(0);
  // The agent had drained it, so the dequeue answers `removed: false`.
  h.setPending([]);
  // Nothing has resolved it yet — with the queue read held, the ✕ is the only actor that
  // can. (Without this the reconcile sometimes wins the race and the test would pass
  // without exercising the branch at all.)
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(1);
  expect(h.deletes).toEqual([]);
  await page.locator(SLOT).getByRole("button", { name: "Cancel queued message" }).click();
  await expect.poll(() => h.deletes.length).toBe(1);

  // There is no marker left to wait for — restoring the bubble would park it "queued"
  // forever, and dropping it would deny a message that shaped the reply.
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION })).toHaveCount(1);
  h.holdSteerReads(false);
  await page.waitForTimeout(500);
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);
});

test("a re-send whose dequeue lost the race settles instead of sending a duplicate", async ({ page }) => {
  const session = "chat-interject-dequeue-race";
  const h = await openAttendedServerTurn(page, session);
  await interject(page, h);

  // The turn ends with the message still queued, so the reconcile goes to re-send it — but
  // between the read and the dequeue, a turn drained it after all (`removed: false`). The
  // exactly-once guard IS that answer: sending anyway would give the agent it twice.
  h.forceDeleteNotRemoved(true);
  h.release(terminalFrames(session));

  await expect.poll(() => h.deletes.length).toBe(1);
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION })).toHaveCount(1);
  await page.waitForTimeout(1_000);
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION)), "no duplicate send").toEqual([]);
});

// ── the server's answer is the only authority, and a missing answer is not one ──────────

test("a transient queue-read failure at turn end is retried, not taken as an answer", async ({ page }) => {
  const session = "chat-interject-read-fails";
  const h = await openAttendedServerTurn(page, session);
  await interject(page, h);

  // The turn ends and the reconcile's first read fails. A one-shot reconcile left the
  // bubble claiming "sent to this server turn" forever — and the text in the server's
  // queue, to be folded into whatever turn ran next, unseen.
  h.failSteerReads(1);
  h.release(terminalFrames(session));
  await expect(page.getByText(/responding to background reports/i)).toHaveCount(0);

  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect.poll(() => h.steerReads()).toBeGreaterThanOrEqual(2); // it asked again
  // Resolved the honest way: the turn never reached it, so it was dequeued and re-sent.
  await expect.poll(() => h.deletes.length).toBe(1);
  await expect.poll(() => h.a2aSends.filter((body) => body.includes(INTERJECTION)).length).toBe(1);
});

test("a turn whose task hasn't settled yet is re-checked until it has", async ({ page }) => {
  const session = "chat-interject-late-terminal";
  const h = await openAttendedServerTurn(page, session);
  await interject(page, h);

  // `turn.finished` can land while the durable task still reads WORKING (the self-POST
  // timed out, or the store commit lags). "Can't tell yet" must not settle or re-send —
  // and must not be the last word either.
  h.taskState.set(TASK, "TASK_STATE_WORKING");
  h.release(terminalFrames(session));
  await page.waitForTimeout(1_000);
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(1);
  expect(h.deletes).toEqual([]);

  h.taskState.set(TASK, "TASK_STATE_COMPLETED"); // it settles a moment later
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect.poll(() => h.a2aSends.filter((body) => body.includes(INTERJECTION)).length).toBe(1);
});

test("with a second server turn live, a leftover is handed to it instead of pulled out", async ({ page }) => {
  const session = "chat-interject-two-turns";
  const h = await openAttendedServerTurn(page, session);
  await interject(page, h);

  // A second nudge comes up while the first still runs (the A2A server serializes the
  // turns, but the second's control frame arrives first).
  h.taskState.set(TASK, "TASK_STATE_WORKING");
  h.taskState.set(TASK2, "TASK_STATE_WORKING");
  h.release([
    { topic: "turn.started", data: { session_id: session, origin: ORIGIN, trigger: "bg-2" } },
    progress(session, { phase: "turn_started" }, TASK2),
  ]);
  await expect.poll(() => h.steerReads()).toBeGreaterThanOrEqual(1);

  // Turn 1 ends — un-addressed, as an older server reports it.
  h.taskState.set(TASK, "TASK_STATE_COMPLETED");
  h.release(terminalFrames(session, TASK));
  await page.waitForTimeout(2_500);

  // Turn 2 drains the same queue, so the message stays queued FOR IT: pulling it out to
  // re-send would race the live turn and could deliver it twice.
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(1);
  expect(h.deletes, "must not pull the message out from under the live second turn").toEqual([]);
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);

  // When turn 2 reads it, its marker settles the bubble — same as any other turn.
  const sent = h.interjected()!;
  h.setPending([]);
  h.release([progress(session, { phase: "steer_consumed", items: [{ id: sent.id, text: sent.text }] }, TASK2)]);
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION })).toHaveCount(1);
});

test("an interjection parked behind an approval settles when another device answers it", async ({ page }) => {
  const session = "chat-interject-hitl-elsewhere";
  const h = await openAttendedServerTurn(page, session);
  await interject(page, h);

  // The turn parks on an approval before reaching the message. The server holds the queue
  // and folds it in right after the form is answered (#1560), so it must stay queued.
  h.taskState.set(TASK, "TASK_STATE_INPUT_REQUIRED");
  h.release([{ topic: "turn.finished", data: { session_id: session, origin: ORIGIN, trigger: "bg-1" } }]);
  await page.waitForTimeout(1_500);
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(1);
  expect(h.deletes).toEqual([]);

  // The approval is answered on ANOTHER device: that turn resumes there and drains the
  // queue, and its marker never reaches this console (an operator turn is not republished).
  // The durable history is what still tells us — and a re-check is what asks.
  h.setPending([]);
  h.taskConsumed.set(TASK, [h.interjected()!.id]);
  h.taskState.set(TASK, "TASK_STATE_COMPLETED");
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION })).toHaveCount(1);
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);
});

test("the durable marker settles a message the in-memory queue can no longer vouch for", async ({ page }) => {
  const session = "chat-interject-durable-marker";
  const h = await openAttendedServerTurn(page, session);
  await interject(page, h);

  // The steering queue lives in memory, so "gone from the queue" alone cannot tell a
  // message the agent READ from one a restart dropped. The task history can: the executor
  // writes a steer-consumed marker into it, which is why this settles while the task is
  // still running and no bus frame ever arrived.
  h.setPending([]);
  h.taskConsumed.set(TASK, [h.interjected()!.id]);
  h.taskState.set(TASK, "TASK_STATE_WORKING");
  h.release([{ topic: "turn.finished", data: { session_id: session, origin: ORIGIN, trigger: "bg-1" } }]);

  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION })).toHaveCount(1);
  expect(h.deletes).toEqual([]);
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);
});

test("a reload mid-submission never leaves the server holding a message nobody tracks", async ({ page }) => {
  const session = "chat-interject-reload-inflight";
  const h = await openAttendedServerTurn(page, session);
  h.holdInterject(true);
  const field = page.locator(`${SLOT} .pl-prompt__field`);
  await field.fill(INTERJECTION);
  await field.press("Enter");
  await expect.poll(() => h.heldInterjects()).toBe(1);

  // The tab reloads while the POST is in flight: the console never learned whether the
  // server took it, but the queued bubble (and its unconfirmed flag) are persisted.
  await h.reload();
  await page.locator(`${SLOT} .pl-prompt__field`).waitFor({ state: "visible" });
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(1);

  // The server DID queue it. A reconcile that read "not in the queue" as "never arrived"
  // would have handed the words back while the agent was about to read them.
  await h.releaseInterjects((body) => ({ ok: true, id: body.id, pending: 1 }));
  h.release(terminalFrames(session));

  // Deliberately multi-step: the re-check ladder has to see the submission land in the
  // queue before anything can resolve it, so give it room rather than race the budget.
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0, { timeout: 25_000 });
  // Accounted for either way: delivered as the operator's next message, and no copy left
  // in the server's queue to ride a later turn.
  await expect.poll(() => h.a2aSends.filter((body) => body.includes(INTERJECTION)).length, { timeout: 15_000 }).toBe(1);
  await expect.poll(() => h.deletes.length, { timeout: 15_000 }).toBe(1);
});

test("a reload whose reattach loses the race to the turn's end still hands the session back", async ({ page }) => {
  const session = "chat-interject-reattach-race";
  const h = await openAttendedServerTurn(page, session);
  h.holdInterject(true);
  const field = page.locator(`${SLOT} .pl-prompt__field`);
  await field.fill(INTERJECTION);
  await field.press("Enter");
  await expect.poll(() => h.heldInterjects()).toBe(1);

  // The reload reattaches to the turn's still-streaming preview, and that resubscribe is slow
  // to come back. The turn ends meanwhile: its `chat.resumed` settles the preview on the bus
  // while the reattach is still waiting, so the slot cancels the reattach — which then never
  // reaches the finalize that would have released the session. The CI flake hit this order
  // by chance (a loaded runner answered the resubscribe late); here it is forced.
  h.holdResubscribe(true);
  await h.reload();
  await field.waitFor({ state: "visible" });
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(1);
  await expect.poll(() => h.heldResubscribes()).toBe(1);
  const stop = page.locator(SLOT).getByRole("button", { name: "Stop", exact: true });
  await expect(stop).toBeVisible(); // reattached: the session reads as busy

  await h.releaseInterjects((body) => ({ ok: true, id: body.id, pending: 1 }));
  h.release(terminalFrames(session));
  await expect(page.locator(SLOT).getByText(POST)).toBeVisible();
  // The reattach gave up on its resubscribe, which was never answered — so the losing order
  // really happened, and nothing but the cancel can have released the session below.
  await expect.poll(() => h.resubscribeAborts()).toBe(1);

  // THE BUG: nothing released it. Stop stayed up, Send stayed disabled, and the interjection
  // stayed "queued" for good, held back as though this browser's own stream would drain it.
  await expect(stop).toHaveCount(0);
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0, { timeout: 15_000 });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  // Resolved the ordinary way for a leftover the ended turn never read: dequeued, then sent.
  await expect.poll(() => h.a2aSends.filter((body) => body.includes(INTERJECTION)).length).toBe(1);
  expect(h.deletes).toHaveLength(1);
});

test("another task's answer landing mid-reattach leaves the live turn's reattach running, and the turn's own end idles", async ({ page }) => {
  const session = "chat-interject-other-task";
  const h = await openAttendedServerTurn(page, session);

  // The reload reattaches to the turn's still-streaming preview, and the resubscribe is slow.
  h.holdResubscribe(true);
  await h.reload();
  await page.locator(`${SLOT} .pl-prompt__field`).waitFor({ state: "visible" });
  await expect.poll(() => h.heldResubscribes()).toBe(1);
  const stop = page.locator(SLOT).getByRole("button", { name: "Stop", exact: true });
  await expect(stop).toBeVisible();

  // A DIFFERENT task finishes in this chat (a scheduled fire) and has no preview here, so its
  // answer is appended after the live preview. It must not stand in for the live turn: the
  // preview's reattach keeps running, and the session stays busy.
  const OTHER = "Nightly backup finished.";
  h.release([
    { topic: "chat.resumed", data: { session_id: session, task_id: TASK2, text: OTHER, state: "completed", origin: "scheduler" } },
  ]);
  await expect(page.locator(SLOT).getByText(OTHER)).toBeVisible();
  await page.waitForTimeout(500);
  expect(h.resubscribeAborts()).toBe(0);
  await expect(stop).toBeVisible();

  // That turn now ends: its `chat.resumed` settles the preview, the slot lets go of its
  // reattach, and the session is handed back.
  h.release(terminalFrames(session));
  await expect(page.locator(SLOT).getByText(POST)).toBeVisible();
  await expect.poll(() => h.resubscribeAborts()).toBe(1);
  await expect(stop).toHaveCount(0);
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

// ── the ladder itself: it must outlive every kind of missing answer ─────────────────────

test("a failing dequeue on the re-send path keeps the ladder alive", async ({ page }) => {
  const session = "chat-interject-delete-fails";
  const h = await openAttendedServerTurn(page, session);
  await interject(page, h);

  // The turn ends with the message still queued, so the reconcile goes to re-send it — and
  // the dequeue that must come first keeps failing. Cancelling the ladder here (because
  // nothing was left in `keep`) strands exactly what it exists to retire: the bubble sits
  // "sent to this server turn" and the text waits in the server's queue for a later turn.
  h.failDeletes(3);
  h.release(terminalFrames(session));
  await expect.poll(() => h.steerReads(), { timeout: 15_000 }).toBeGreaterThanOrEqual(3);
  // …and it resolves once the dequeue gets through.
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect.poll(() => h.deletes.length).toBe(1);
  await expect.poll(() => h.a2aSends.filter((body) => body.includes(INTERJECTION)).length).toBe(1);
});

test("the server's drain log settles a read message with no marker of any kind", async ({ page }) => {
  const session = "chat-interject-drain-log";
  const h = await openAttendedServerTurn(page, session);
  const sent = await interject(page, h);

  // The agent read it, but nothing announced the boundary: the sync middleware path emits
  // no marker, and a failed dispatch emits none either. Absence from the queue alone must
  // not become "never arrived" — the server's drain log is what answers it.
  h.setPending([]);
  h.setDrained([sent.id]);
  h.taskState.set(TASK, "TASK_STATE_WORKING");
  h.release([{ topic: "turn.finished", data: { session_id: session, origin: ORIGIN, trigger: "bg-1", task_id: TASK } }]);

  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
  await expect(page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION })).toHaveCount(1);
  // Never re-offered and never re-sent: the agent already has it.
  expect(await page.locator(`${SLOT} .pl-prompt__field`).inputValue()).toBe("");
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);
});

test("a message the agent read is never handed back, even with nothing to prove it", async ({ page }) => {
  const session = "chat-interject-unlocated";
  const h = await openAttendedServerTurn(page, session);
  await interject(page, h);

  // The worst case: the queue drained it, no marker frame, no drain log (an older server,
  // or one that restarted), and the task never reaches a terminal state. The console cannot
  // tell "read" from "lost" — so it settles the bubble as sent and never re-offers the
  // words. An operator re-sending a message the agent already used is the worse outcome.
  h.setPending([]);
  h.setDrained([]);
  h.taskState.set(TASK, "TASK_STATE_WORKING");
  h.release([{ topic: "turn.finished", data: { session_id: session, origin: ORIGIN, trigger: "bg-1", task_id: TASK } }]);

  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0, { timeout: 25_000 });
  await expect(page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION })).toHaveCount(1);
  expect(await page.locator(`${SLOT} .pl-prompt__field`).inputValue()).toBe("");
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);
  expect(h.deletes).toEqual([]);
});

test("switching windows doesn't refund the grace, and a blip doesn't spend it", async ({ page }) => {
  const session = "chat-interject-grace";
  const h = await openAttendedServerTurn(page, session);
  await interject(page, h);

  // Four reads never reach the server (a link blip across the turn's end). They must not
  // spend the grace that exists to survive them: after the FIRST successful read the
  // message is still queued, not resolved on one answer.
  h.failSteerReads(4);
  h.setPending([]);
  h.setDrained([]);
  h.taskState.set(TASK, "TASK_STATE_WORKING");
  h.release([{ topic: "turn.finished", data: { session_id: session, origin: ORIGIN, trigger: "bg-1", task_id: TASK } }]);
  await expect.poll(() => h.steerReads(), { timeout: 25_000 }).toBeGreaterThanOrEqual(5);
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(1);

  // And coming back to the tab re-checks NOW without refunding the grace — otherwise
  // alt-tabbing every couple of seconds keeps an unresolvable message unresolved forever.
  for (let i = 0; i < 6; i++) {
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.waitForTimeout(400);
  }
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0, { timeout: 15_000 });
  await expect(page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION })).toHaveCount(1);
});

test("words are not re-offered when the dequeue proves the agent just read them", async ({ page }) => {
  const session = "chat-interject-reclaim-race";
  const h = await openAttendedServerTurn(page, session);

  // The window the dequeue-first rule exists to close, INVERTED. The submission never got
  // an answer (unconfirmed), the queue read says neither queued nor drained — and then a
  // turn folds the message in between that read and the dequeue. The dequeue takes nothing
  // back and the server's drain log now names the id: that answer is in hand, so the only
  // honest outcome is to settle. Handing the words over here is the "delivered AND
  // re-offered" hole — the operator re-sends what the agent already used.
  h.holdInterject(true);
  const field = page.locator(`${SLOT} .pl-prompt__field`);
  await field.fill(INTERJECTION);
  await field.press("Enter");
  await expect.poll(() => h.heldInterjects()).toBe(1);
  const sent = h.interjected()!;
  h.setPending([]);
  h.setDrained([]);
  await h.abortInterjects();
  h.drainOnDelete(sent.id);
  h.taskState.set(TASK, "TASK_STATE_COMPLETED");
  h.release(terminalFrames(session));

  await expect.poll(() => h.deletes.length, { timeout: 25_000 }).toBeGreaterThanOrEqual(1);
  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0, { timeout: 15_000 });
  expect(h.drained()).toContain(sent.id); // the server says the agent has it
  expect(await field.inputValue(), "its words must not be re-offered").not.toContain(INTERJECTION);
  await expect(page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION })).toHaveCount(1);
  expect(h.a2aSends.filter((body) => body.includes(INTERJECTION))).toEqual([]);
});

test("words DO come back when the dequeue proves nothing was delivered", async ({ page }) => {
  const session = "chat-interject-reclaim-clean";
  const h = await openAttendedServerTurn(page, session);

  // The other side of the same answer: the submission was never acknowledged, the server
  // tracks folds and reports neither a queued nor a drained copy, and our dequeue takes
  // nothing back — it never landed, so the operator gets their words.
  h.holdInterject(true);
  const field = page.locator(`${SLOT} .pl-prompt__field`);
  await field.fill(INTERJECTION);
  await field.press("Enter");
  await expect.poll(() => h.heldInterjects()).toBe(1);
  h.setPending([]);
  h.setDrained([]);
  await h.abortInterjects();
  h.taskState.set(TASK, "TASK_STATE_COMPLETED");
  h.release(terminalFrames(session));

  await expect(page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0, { timeout: 25_000 });
  await expect.poll(() => field.inputValue(), { timeout: 10_000 }).toContain(INTERJECTION);
  await expect(page.locator(`${SLOT} .pl-message--user`).filter({ hasText: INTERJECTION })).toHaveCount(0);
  // (The aborted submission toasts too — "couldn't confirm" — so match the hand-back one.)
  await expect(page.locator(".pl-toast--error").filter({ hasText: /never reached the agent/i })).toBeVisible();
});
