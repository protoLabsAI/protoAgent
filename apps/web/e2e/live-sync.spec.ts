import { expect, test, type APIRequestContext, type Browser, type BrowserContext, type Page, type TestInfo } from "@playwright/test";

// ADR 0130 S0 — the D4 multi-client sync contract, tested as a contract: two REAL browser
// contexts against ONE mock server (no page.route anywhere), sharing one server-side world
// through the mock's live-sync scope (`x-e2e-sync`, mock-server.mjs). The server owns the
// durable turn rows and a seq'd, replayable change feed; the clients must converge on it.
//
// Cases that fail on today's main are marked test.fail() with the bug or slice id they wait
// on — the suite is green now and each marker flips (fails as "passed unexpectedly") when
// its slice lands, which is the signal to delete the marker. These tests are the acceptance
// bar for S1–S5; they must not be weakened to pass early.

const SLOT = ".chat-session-slot:not([hidden])";
// The default canned scenario's settled answer (fixtures.mjs scenarioFor fallthrough).
const CANNED_ANSWER = "Done — found 8 results.";
// The COMPONENT scenario (#1323): an inline component-v1 table + this answer.
const COMPONENT_ANSWER = "Here's the fleet breakdown.";

type Client = { context: BrowserContext; page: Page };
const openClients: BrowserContext[] = [];
test.afterEach(async () => {
  await Promise.all(openClients.splice(0).map((context) => context.close().catch(() => {})));
});

/** One scope per test run — retries and repeats get a fresh server-side world. */
function syncScope(testInfo: TestInfo, slug: string): string {
  return `sync-${testInfo.workerIndex}-${testInfo.retry}-${testInfo.repeatEachIndex}-${slug}`;
}

/** A client: its own browser context (cookies, storage, SSE), every request stamped with
 *  the scope so both clients of a test read and write the same server-side world. */
async function openClient(browser: Browser, scope: string): Promise<Client> {
  const context = await browser.newContext({
    baseURL: test.info().project.use.baseURL,
    viewport: { width: 1200, height: 900 },
    extraHTTPHeaders: { "x-e2e-sync": scope },
  });
  openClients.push(context);
  const page = await context.newPage();
  await page.goto("/app/", { waitUntil: "load" });
  await page.locator(`${SLOT} .pl-prompt__field`).waitFor({ state: "visible" });
  return { context, page };
}

function syncPost(request: APIRequestContext, scope: string, path: string, data: Record<string, unknown> = {}) {
  return request.post(`/api/__test__/sync/${path}`, { headers: { "x-e2e-sync": scope }, data });
}

/** Seed the scope's durable store with one settled turn BEFORE any client boots, so both
 *  contexts hydrate the same server-side chat as their current tab. */
async function seedChat(request: APIRequestContext, scope: string, sessionId: string, prompt: string, answer: string) {
  const res = await syncPost(request, scope, "seed", {
    sessions: [{ session_id: sessionId, turns: [{ prompt, answer }] }],
  });
  expect(res.ok()).toBe(true);
}

async function sendMessage(page: Page, text: string) {
  const composer = page.locator(SLOT).getByPlaceholder(/Message protoAgent/i);
  await composer.fill(text);
  await composer.press("Enter");
}

/** The "return to the window" nudge (focus + visibility), dispatched until `locator`
 *  resolves — the boot resync is spaced by RESYNC_MIN_GAP_MS, so one dispatch is never
 *  enough (the bd-wk4ti e2e lesson). Convergence itself is what the contract asserts;
 *  D4 puts no single-dispatch bound on the away→return path. */
async function pollReturn(page: Page, visible: ReturnType<Page["locator"]>) {
  await expect
    .poll(
      async () => {
        await page.evaluate(() => {
          window.dispatchEvent(new Event("focus"));
          document.dispatchEvent(new Event("visibilitychange"));
        });
        return visible.count();
      },
      { timeout: 25_000 },
    )
    .toBeGreaterThan(0);
}

// ── Case 1 ────────────────────────────────────────────────────────────────────────────────
// D4: "A change in one (send …) appears in the other within 2s, with no reload." The mock
// already records the turn durably and feeds `session.changed` / `turn.completed` with seq
// over the scoped SSE — today's console has no consumer for them (a browser-streamed turn
// publishes nothing it reacts to), so the other client stays blind until it happens to
// resync. Flips when the S3 sync client (cursor resume + entity refetch) lands.
test.fail("a send in one client appears in the other within 2 seconds, with no reload", {
  annotation: { type: "fails-on-main", description: "ADR 0130 S1–S3 (#5196): no change-feed consumer in the console yet" },
}, async ({ browser, request }, testInfo) => {
  const scope = syncScope(testInfo, "send");
  const session = "chat-sync-send";
  await seedChat(request, scope, session, "Sprint kickoff", "Seeded — the sprint board is ready.");

  const a = await openClient(browser, scope);
  const b = await openClient(browser, scope);
  await expect(a.page.locator(SLOT).getByText("Seeded — the sprint board is ready.")).toBeVisible();
  await expect(b.page.locator(SLOT).getByText("Seeded — the sprint board is ready.")).toBeVisible();

  await sendMessage(a.page, "What's new in the fleet?");
  // The turn settles in A — the server-side change is complete, and the 2s clock starts.
  await expect(a.page.locator(SLOT).getByText(CANNED_ANSWER)).toBeVisible();

  await expect(b.page.locator(SLOT).getByText("What's new in the fleet?")).toBeVisible({ timeout: 2_000 });
  await expect(b.page.locator(SLOT).getByText(CANNED_ANSWER)).toBeVisible({ timeout: 2_000 });
});

// ── Case 2 ────────────────────────────────────────────────────────────────────────────────
// A server-started turn (a background delegation's result) finishes while that chat is
// hidden behind another tab. The hidden pane got the same bus frames (`turn.started` +
// `chat.progress` preview, then the terminal `chat.resumed` + `turn.finished`), so on
// return it must show EXACTLY ONE bubble for the task — the authoritative answer replacing
// the preview, never beside it — and no spinner (the bd-wk4ti contract, fixed by #5199).
test("a server-started turn finishing behind another tab leaves one bubble, no spinner", async ({ browser, request }, testInfo) => {
  const scope = syncScope(testInfo, "bg");
  const session = "chat-sync-bg";
  const preview = "Reviewing the brandLaunch result…";
  const answer = "brandLaunch came back, and I agree with its plan.";
  await seedChat(request, scope, session, "Kick off brandLaunch", "Delegated to brandLaunch in the background.");

  const a = await openClient(browser, scope);
  const b = await openClient(browser, scope);
  await expect(b.page.locator(SLOT).getByText("Delegated to brandLaunch")).toBeVisible();
  // B hides the chat behind a fresh tab; the session stays mounted, just not visible.
  await b.page.locator(".pl-tabbar__add:visible").click();
  await expect(b.page.locator(SLOT).getByText("Delegated to brandLaunch")).toHaveCount(0);

  // The server starts the turn and streams its preview — A (still on the chat) watches live.
  await syncPost(request, scope, "server-turn", {
    session_id: session, task_id: "task-sync-bg-1", action: "start", origin: "background-resume", preview,
  });
  await expect(a.page.locator(SLOT).getByText(preview)).toBeVisible();

  // It finishes while B's chat is hidden.
  await syncPost(request, scope, "server-turn", {
    session_id: session, task_id: "task-sync-bg-1", action: "finish", origin: "background-resume", text: answer,
  });

  // A: the authoritative answer replaced the preview in place.
  await expect(a.page.locator(SLOT).getByText(answer)).toBeVisible();
  await expect(a.page.locator(SLOT).getByText(preview)).toHaveCount(0);
  await expect(a.page.locator(`${SLOT} .chat-server-turn`)).toHaveCount(0);

  // B returns to the chat: exactly one bubble for the task, no spinner, nothing still live.
  await b.page.locator(".pl-tabbar__tab").filter({ hasText: "Kick off brandLaunch" }).click();
  await expect(b.page.locator(SLOT).getByText(answer)).toBeVisible();
  await expect(b.page.locator(`${SLOT} .pl-message--assistant`).filter({ hasText: answer })).toHaveCount(1);
  await expect(b.page.locator(SLOT).getByText(preview)).toHaveCount(0);
  await expect(b.page.locator(`${SLOT} .chat-server-turn`)).toHaveCount(0);
  await expect(b.page.locator(`${SLOT} .pl-message--assistant .pl-spinner`)).toHaveCount(0);
});

// ── Case 3 ────────────────────────────────────────────────────────────────────────────────
// A turn that ends while its client is away must come back WHOLE: the operator returns to
// the answer AND its component parts (the #5161 shape — a board-cards turn finished while
// Josh was on another agent, and on return the cards never rendered). The client drops off
// the network right after its send opens; the server finishes the turn — an inline
// component-v1 table + answer — into the durable store; on return the client converges.
test("a turn that ends while the client is away renders its component parts on return", async ({ browser, request }, testInfo) => {
  const scope = syncScope(testInfo, "away");
  const session = "chat-sync-away";
  await seedChat(request, scope, session, "Board prep", "Seeded — the board is ready.");

  const a = await openClient(browser, scope);
  await expect(a.page.locator(SLOT).getByText("Seeded — the board is ready.")).toBeVisible();

  // SLOW stretches the mock's frame gaps so the drop reliably lands before the component
  // and terminal frames stream; COMPONENT picks the inline-table scenario (#1323).
  await sendMessage(a.page, "SLOW COMPONENT: show the fleet");
  // The stream is open (first frames rendered) — now the client falls off the network.
  await expect(a.page.locator(`${SLOT} .pl-message--assistant`).last()).toBeVisible();
  await a.context.setOffline(true);
  // The drop preceded the component frame — what renders below must come from the server's
  // durable copy on return, never from the live stream this client no longer has.
  await expect(a.page.locator(`${SLOT} .chat-comp`)).toHaveCount(0);
  await expect(a.page.locator(SLOT).getByText(COMPONENT_ANSWER)).toHaveCount(0);

  // The server owns the turn: it completes into the durable store even with the client gone
  // (mock handleSyncSend), component part and all. Wait on the store itself — the test's own
  // request context is not offline — then let the client return.
  await expect
    .poll(async () => {
      const res = await request.get(`/api/chat/sessions/${session}/turns`, { headers: { "x-e2e-sync": scope } });
      const body = (await res.json()) as { turns?: { state?: string }[]; live_task_id?: string | null };
      return (
        body.live_task_id == null &&
        (body.turns?.length ?? 0) === 2 &&
        (body.turns ?? []).every((turn) => turn.state === "TASK_STATE_COMPLETED")
      );
    })
    .toBe(true);
  await a.context.setOffline(false);

  // Converge: the component renders, with the answer, exactly once — and nothing still spins.
  await pollReturn(a.page, a.page.locator(`${SLOT} .chat-comp`));
  const comp = a.page.locator(`${SLOT} .chat-comp`);
  await expect(comp).toBeVisible();
  await expect(comp).toContainText("Fleet");
  await expect(comp).toContainText("Hauler");
  await expect(a.page.locator(SLOT).getByText(COMPONENT_ANSWER)).toBeVisible();
  await expect(a.page.locator(`${SLOT} .pl-message--assistant`).filter({ hasText: COMPONENT_ANSWER })).toHaveCount(1);
  await expect(a.page.locator(`${SLOT} .chat-server-turn`)).toHaveCount(0);
});

// ── Case 4 ────────────────────────────────────────────────────────────────────────────────
// D4: "A client never shows a spinner, a queued steer or a locked composer for a session
// the server reports idle." A server turn starts, then the server RESTARTS mid-turn: the
// turn is lost, the rebooted server derives idle from its store and feeds `turn.failed` +
// `session.changed`. Today's console armed its belief from `turn.started` and nothing ever
// hands it back — the spinner stays and the next message is swallowed as a steer the server
// will never drain (bd-0ad5v). Flips when server-held session state is rendered (S2/S4).
test.fail("a session the server reports idle never keeps a spinner or swallows a send as a steer", {
  annotation: { type: "fails-on-main", description: "bd-0ad5v / ADR 0130 S2+S4 (#5196): session status is still inferred locally" },
}, async ({ browser, request }, testInfo) => {
  const scope = syncScope(testInfo, "idle");
  const session = "chat-sync-idle";
  await seedChat(request, scope, session, "Standup notes", "Seeded — notes captured.");

  const client = await openClient(browser, scope);
  await expect(client.page.locator(SLOT).getByText("Seeded — notes captured.")).toBeVisible();

  // The server starts a scheduled turn in this chat… and restarts before it finishes.
  await syncPost(request, scope, "server-turn", {
    session_id: session, task_id: "task-sync-ghost-1", action: "start", origin: "scheduler",
  });
  await expect(client.page.locator(`${SLOT} .chat-server-turn`)).toBeVisible();
  await syncPost(request, scope, "restart");

  // The server now reports the session idle (failed turn, no live task) and its feed says
  // so. The client must converge: no spinner held for a turn the server doesn't report.
  await expect(client.page.locator(`${SLOT} .chat-server-turn`)).toHaveCount(0, { timeout: 10_000 });

  // And the next message is a SEND that starts a turn — never a steer queued forever
  // against a live turn that doesn't exist.
  await sendMessage(client.page, "Are you still there?");
  await expect(client.page.locator(SLOT).getByText(CANNED_ANSWER)).toBeVisible();
  await expect(client.page.locator(`${SLOT} .pl-message--queued`)).toHaveCount(0);
});

// ── Case 5 ────────────────────────────────────────────────────────────────────────────────
// D3/D4: network drop → reconnect and hidden → visible all converge to the server's state.
// While one client is off the network, the other runs a whole turn; on reconnect + return
// the away client converges to the server's transcript — the new turn exactly once, no
// spinner, composer open. (Today this rides the ADR 0104 focus/visibility resync; S3 moves
// it onto the cursor-resumed feed. The contract — convergence — is the same.)
test("a client that dropped offline converges to the server's state on reconnect", async ({ browser, request }, testInfo) => {
  const scope = syncScope(testInfo, "drop");
  const session = "chat-sync-drop";
  await seedChat(request, scope, session, "Deploy status", "Seeded — deploy is green.");

  const a = await openClient(browser, scope);
  const b = await openClient(browser, scope);
  await expect(b.page.locator(SLOT).getByText("Seeded — deploy is green.")).toBeVisible();

  // B falls off the network (laptop lid, tunnel, sleep)…
  await b.context.setOffline(true);

  // …while A runs a whole turn against the live server.
  await sendMessage(a.page, "Ship the release notes");
  await expect(a.page.locator(SLOT).getByText(CANNED_ANSWER)).toBeVisible();

  // B reconnects and returns to the window: it must converge to the server's transcript.
  await b.context.setOffline(false);
  await pollReturn(b.page, b.page.locator(SLOT).getByText("Ship the release notes"));
  await expect(b.page.locator(SLOT).getByText(CANNED_ANSWER)).toBeVisible();
  await expect(b.page.locator(`${SLOT} .pl-message--user`).filter({ hasText: "Ship the release notes" })).toHaveCount(1);
  await expect(b.page.locator(`${SLOT} .pl-message--assistant`).filter({ hasText: CANNED_ANSWER })).toHaveCount(1);
  await expect(b.page.locator(`${SLOT} .pl-message--assistant .pl-spinner`)).toHaveCount(0);
  // The composer is open for a normal send — nothing is locked on a turn that is over.
  await expect(b.page.locator(SLOT).getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});
