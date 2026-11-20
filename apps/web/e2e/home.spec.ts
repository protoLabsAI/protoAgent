import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { expectNoAxeViolations } from "./axe";

// The Home conversation, end to end in a real browser (#5545): briefs and alerts the server
// writes into Home's thread show up there, Home stays the pinned first tab, a quiet stretch
// does not lose the next brief, and a job another chat scheduled reports in THAT chat.
//
// The server side is stubbed at the routes the console reads:
//   GET  /api/home            → Home's session id + `last_read_at`
//   POST /api/home/read       → marks Home read (stamps `last_read_at`)
//   GET  /api/home/entries    → the entries in Home's thread (`graph/home_entries.py`)
//   GET  /api/chat/sessions…  → the session index and each session's durable turns
// A new entry "arrives" the way it does live: the stubbed thread gains the entry, then a
// `session.changed` frame on the event stream makes the open console re-read it. Frames are
// sent through the mock's bus tap, one tap per test so parallel tests never see each other's.
//
// What the agent is told when the operator replies to an entry, and the isolation of `job:`
// threads, are server behavior — covered by the Python tests of H2/H3, not here.

const HOME_ID = "home-e2e";
const OTHER_ID = "chat-deploys";
const OTHER_TITLE = "Check the deploys";
const MINUTE = 60_000;
const DAY = 24 * 60 * MINUTE;

type HomeEntryRow = {
  id: string;
  content: string;
  additional_kwargs: {
    protoagent_home_entry: { kind: string; importance: string; source: string; notice_id: string };
    protoagent_sent_at: string;
  };
};

/** One entry as `GET /api/home/entries` returns it, written `agoMs` before now. */
function entry(
  noticeId: string,
  meta: { kind: string; importance?: string; source: string },
  text: { title: string; body: string },
  agoMs: number,
): HomeEntryRow {
  return {
    id: `home-entry-${noticeId}`,
    content: `[Home entry: ${meta.kind}]\n${text.title}\n\n${text.body}`,
    additional_kwargs: {
      protoagent_home_entry: { kind: meta.kind, importance: meta.importance ?? "normal", source: meta.source, notice_id: noticeId },
      protoagent_sent_at: new Date(Date.now() - agoMs).toISOString(),
    },
  };
}

const brief = (noticeId: string, title: string, agoMs: number) =>
  entry(noticeId, { kind: "review", source: "scheduler" }, { title, body: "Three pull requests merged and CI is green." }, agoMs);

const fleetDown = (agoMs: number) =>
  entry(
    "ntc_fleet",
    { kind: "notify", importance: "high", source: "fleet-health" },
    { title: "protoengineer stopped", body: "The fleet member stopped answering health checks." },
    agoMs,
  );

/** The stubbed server state one test owns. Tests mutate it, then call `homeChanged`. */
type World = { lastReadAt: string | null; entries: HomeEntryRow[]; reads: number };

const tapFor = (info: TestInfo) => `home-${info.testId}-${info.retry}`;

async function openConsole(page: Page, info: TestInfo, world: World) {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.context().setExtraHTTPHeaders({ "x-e2e-bus-tap": tapFor(info) });
  const state = () => ({ session_id: HOME_ID, last_read_at: world.lastReadAt });
  await page.route(/\/api\/home$/, (route) => route.fulfill({ json: state() }));
  await page.route(/\/api\/home\/read$/, (route) => {
    world.reads += 1;
    world.lastReadAt = new Date().toISOString();
    return route.fulfill({ json: state() });
  });
  await page.route(/\/api\/home\/entries$/, (route) =>
    route.fulfill({ json: { session_id: HOME_ID, entries: world.entries } }),
  );
  // The session index: one ordinary chat. Home is not an index row — the console adds its
  // tab from `GET /api/home` — and Home's thread holds no operator turns yet.
  await page.route(/\/api\/chat\/sessions(\?.*)?$/, (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    if (new URL(route.request().url()).searchParams.get("parked") === "true") return route.fulfill({ json: { sessions: [] } });
    return route.fulfill({
      json: { sessions: [{ session_id: OTHER_ID, last_updated: "2026-11-01T09:00:00Z", turn_count: 1 }], closed: [] },
    });
  });
  await page.route(/\/api\/chat\/sessions\/[^/]+\/turns(\?.*)?$/, (route) => {
    if (!route.request().url().includes(`/${OTHER_ID}/`)) return route.fulfill({ json: { turns: [], live_task_id: null } });
    return route.fulfill({
      json: {
        turns: [{
          task_id: "task-deploys",
          state: "TASK_STATE_COMPLETED",
          last_updated: "2026-11-01T09:00:00Z",
          text: "All four deploys are healthy.",
          status: { state: "TASK_STATE_COMPLETED" },
          artifacts: [{ parts: [{ text: "All four deploys are healthy." }] }],
          history: [{ role: "ROLE_USER", parts: [{ text: OTHER_TITLE }] }],
        }],
        live_task_id: null,
      },
    });
  });
  await page.goto("/app/", { waitUntil: "load" });
}

/** Send frames down this test's event stream, once the console's stream is attached. */
async function publish(page: Page, info: TestInfo, frames: { topic: string; data: Record<string, unknown> }[]) {
  await expect.poll(async () => {
    const res = await page.request.post("/api/__test__/bus", { data: { tap: tapFor(info), frames: [] } });
    return (await res.json()).streams;
  }).toBeGreaterThan(0);
  await page.request.post("/api/__test__/bus", { data: { tap: tapFor(info), frames } });
}

/** The server's "Home's thread moved" signal: the console re-reads the index and Home's entries. */
const homeChanged = (page: Page, info: TestInfo) =>
  publish(page, info, [{ topic: "session.changed", data: { session_id: HOME_ID } }]);

const tabs = (page: Page) => page.locator('.pl-tabbar [role="tab"]');
const homeTab = (page: Page) => tabs(page).first();
const otherTab = (page: Page) => tabs(page).filter({ hasText: OTHER_TITLE });
/** Select Home. Its tab is added once `GET /api/home` answers, so wait for it to be the first
 *  tab before clicking "the first tab". */
async function openHome(page: Page) {
  await expect(homeTab(page)).toHaveAccessibleName(/^Home/);
  await homeTab(page).click();
  await expect(homeTab(page)).toHaveAttribute("aria-selected", "true");
}
const homeTranscript = (page: Page) => page.locator(`#pl-conv-${HOME_ID}`);
const cards = (page: Page) => homeTranscript(page).locator(".pl-home-alert-card");
const card = (page: Page, noticeId: string) => homeTranscript(page).locator(`[data-home-notice="${noticeId}"]`);

// (1) ─────────────────────────────────────────────────────────────────────────────────────
test("a brief and a later fleet-down alert both appear in Home", async ({ page }, info) => {
  const world: World = { lastReadAt: null, entries: [brief("ntc_brief", "Morning brief", 10 * MINUTE)], reads: 0 };
  await openConsole(page, info, world);
  await openHome(page);

  await expect(card(page, "ntc_brief")).toBeVisible();
  await expect(card(page, "ntc_brief").getByRole("heading", { name: "Morning brief" })).toBeVisible();
  await expect(card(page, "ntc_brief")).toContainText("Three pull requests merged and CI is green.");
  // The bracketed head is the model's reference text, never shown to the operator.
  await expect(homeTranscript(page)).not.toContainText("[Home entry");

  // Later, with Home still open, a fleet member goes down.
  world.entries = [...world.entries, fleetDown(0)];
  await homeChanged(page, info);

  const alert = card(page, "ntc_fleet");
  await expect(alert).toBeVisible();
  await expect(alert.getByRole("heading", { name: "protoengineer stopped" })).toBeVisible();
  await expect(alert).toContainText("Urgent");
  await expect(alert.getByRole("link", { name: "Fleet member" })).toBeVisible();
  // Both are there, once each, in the order they were written.
  await expect(cards(page)).toHaveCount(2);
  await expect(cards(page).nth(0)).toContainText("Morning brief");
  await expect(cards(page).nth(1)).toContainText("protoengineer stopped");
  // A later re-read of the same thread adds nothing.
  await homeChanged(page, info);
  await expect(card(page, "ntc_fleet")).toBeVisible();
  await expect(cards(page)).toHaveCount(2);
});

// (4) ─────────────────────────────────────────────────────────────────────────────────────
test("Home is the first tab and has no close control", async ({ page }, info) => {
  const world: World = { lastReadAt: null, entries: [], reads: 0 };
  await openConsole(page, info, world);

  await expect(otherTab(page)).toBeVisible(); // the strip holds ordinary chats too
  const home = homeTab(page);
  await expect(home).toHaveAccessibleName("Home");
  await expect(home.locator(".pl-tabbar__label")).toHaveText("Home");
  await expect(home.locator(".pl-tabbar__close")).toHaveCount(0);
  await expect(home.getByRole("button")).toHaveCount(0);
  // Every other chat keeps its close control — Home is the exception, not the strip.
  await expect(otherTab(page).locator(".pl-tabbar__close")).toHaveCount(1);
  await expect(tabs(page).filter({ hasText: /^Home$/ })).toHaveCount(1);

  // Selecting Home, and coming back to it from another chat, never moves it or lets it close.
  await openHome(page);
  await expect(page.getByPlaceholder("Reply to your agent…")).toBeVisible();
  await otherTab(page).click();
  await expect(otherTab(page)).toHaveAttribute("aria-selected", "true");
  await expect(homeTab(page)).toHaveAccessibleName("Home");
  await expect(homeTab(page).locator(".pl-tabbar__close")).toHaveCount(0);

  // Still first after a reload.
  await page.reload({ waitUntil: "load" });
  await expect(otherTab(page)).toBeVisible();
  await expect(homeTab(page)).toHaveAccessibleName("Home");
  await expect(homeTab(page).locator(".pl-tabbar__close")).toHaveCount(0);
});

// (5) ─────────────────────────────────────────────────────────────────────────────────────
test("after 3 idle days the next brief still lands", async ({ page }, info) => {
  // The last thing Home held is a brief from three days ago, read back then. Nothing since.
  const world: World = {
    lastReadAt: new Date(Date.now() - 3 * DAY + MINUTE).toISOString(),
    entries: [brief("ntc_old", "Monday brief", 3 * DAY)],
    reads: 0,
  };
  await openConsole(page, info, world);
  await otherTab(page).click();
  await expect(homeTab(page)).toHaveAccessibleName("Home"); // nothing unread after the quiet days

  // The next brief is written while the operator is in another chat.
  world.entries = [...world.entries, brief("ntc_next", "Thursday brief", 0)];
  await homeChanged(page, info);
  await expect(homeTab(page)).toHaveAccessibleName("Home, 1 new");
  expect(world.reads).toBe(0); // Home was never on screen, so nothing marked it read

  await openHome(page);
  await expect(card(page, "ntc_next")).toBeVisible();
  await expect(card(page, "ntc_next").getByRole("heading", { name: "Thursday brief" })).toBeVisible();
  // The old brief is still above it, under its own day; the new one starts "Today" and is
  // marked as the one unread entry.
  await expect(cards(page)).toHaveCount(2);
  await expect(cards(page).nth(0)).toContainText("Monday brief");
  const dividers = homeTranscript(page).locator(".pl-home-divider");
  await expect(dividers).toHaveCount(3);
  await expect(dividers.nth(0)).not.toHaveText(/^(Today|Yesterday)$/);
  await expect(dividers.nth(1)).toHaveText("Today");
  await expect(dividers.nth(2)).toHaveText("1 new");
  // Opening Home reads it: the tab's count clears.
  await expect(homeTab(page)).toHaveAccessibleName("Home");
  expect(world.reads).toBeGreaterThan(0);
});

// (6) ─────────────────────────────────────────────────────────────────────────────────────
test("a job from another chat stays in that chat", async ({ page }, info) => {
  const world: World = {
    lastReadAt: new Date(Date.now() - 5 * MINUTE).toISOString(),
    entries: [brief("ntc_brief", "Morning brief", 10 * MINUTE)],
    reads: 0,
  };
  await openConsole(page, info, world);
  await otherTab(page).click();
  const otherTranscript = page.locator(`#pl-conv-${OTHER_ID}`);
  await expect(otherTranscript).toContainText("All four deploys are healthy.");

  // A job the other chat scheduled fires. Its result is addressed to that chat
  // (`origin_session`), and the server writes nothing into Home's thread for it.
  const summary = "Nightly deploy check: all four regions green.";
  await publish(page, info, [{
    topic: "scheduler.completed",
    data: {
      job_id: "job-deploy-check", origin_session: OTHER_ID, status: "completed", summary,
      fired_at: new Date().toISOString(), notice_id: "ntc_job", task_id: "task-job", collapse: false,
    },
  }]);
  await homeChanged(page, info);

  await expect(otherTranscript.locator(".chat-scheduled")).toHaveCount(1);
  await expect(otherTranscript.locator(".chat-scheduled")).toContainText(summary);
  // Home did not get it: no unread count, no card, no copy of the result.
  await expect(homeTab(page)).toHaveAccessibleName("Home");
  await openHome(page);
  await expect(card(page, "ntc_brief")).toBeVisible();
  await expect(cards(page)).toHaveCount(1);
  await expect(homeTranscript(page).locator(".chat-scheduled")).toHaveCount(0);
  await expect(homeTranscript(page)).not.toContainText(summary);
  // And it is still in the chat that asked for it.
  await otherTab(page).click();
  await expect(otherTranscript.locator(".chat-scheduled")).toHaveCount(1);
});

// (7) ─────────────────────────────────────────────────────────────────────────────────────
/** Home with each kind of entry in it: a brief, an urgent alert and a decision pointer. */
const fullHome = (): World => ({
  lastReadAt: new Date(Date.now() - 3 * MINUTE).toISOString(),
  entries: [
    brief("ntc_brief", "Morning brief", 2 * DAY),
    fleetDown(2 * MINUTE),
    entry(
      "decision-dec-1",
      { kind: "question", importance: "high", source: "decision" },
      { title: "Ship the release tonight?", body: "The release branch is green and waiting on you." },
      MINUTE,
    ),
  ],
  reads: 0,
});

const HOME_TAB = '.pl-tabbar [role="tab"][aria-label^="Home"]';
const HOME_SLOT = `.chat-session-slot:has(#pl-conv-${HOME_ID})`;

test("Home's tab, transcript and composer have no accessibility violations at 1280 px and 390 px", async ({ page }, info) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openConsole(page, info, fullHome());

  await test.step("1280 px: the desktop tab strip", async () => {
    await expect(otherTab(page)).toBeVisible();
    // Unselected, carrying its unread count.
    await expect(homeTab(page)).toHaveAccessibleName("Home, 2 new");
    await expectNoAxeViolations(page, { include: HOME_TAB });

    await openHome(page);
    await expect(cards(page)).toHaveCount(3);
    await expect(homeTab(page)).toHaveAccessibleName("Home"); // read on open
    await expect(page.getByPlaceholder("Reply to your agent…")).toBeVisible();

    await expectNoAxeViolations(page, { include: HOME_TAB });
    await expectNoAxeViolations(page, { include: `#pl-conv-${HOME_ID}` });
    await expectNoAxeViolations(page, { include: `${HOME_SLOT} .composer-wrap` });
    // Every tab in the strip. The one excused rule is `nested-interactive`, and only for the
    // OTHER chats' tabs: each holds its close button. Home's tab has none, and passed with
    // nothing excused above.
    await expect(otherTab(page).locator(".pl-tabbar__close")).toHaveCount(1);
    await expectNoAxeViolations(page, { include: '.pl-tabbar [role="tab"]', disableRules: ["nested-interactive"] });
  });

  await test.step("390 px: the phone shell", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload({ waitUntil: "load" });
    // The phone shell opens on Home; its "tab" is the header title, which opens the session
    // sheet where Home is the pinned first row.
    const title = page.locator(".mshell-title-heading");
    await expect(title).toHaveText("Home");
    await expect(cards(page)).toHaveCount(3);
    await expect(page.getByPlaceholder("Reply to your agent…")).toBeVisible();

    await expectNoAxeViolations(page, { include: ".mshell-title-heading" });
    await expectNoAxeViolations(page, { include: `#pl-conv-${HOME_ID}` });
    await expectNoAxeViolations(page, { include: `${HOME_SLOT} .composer-wrap` });

    await title.getByRole("button").click();
    const pinned = page.locator(".session-sheet-pinned");
    await expect(pinned.locator(".session-sheet-title")).toHaveText("Home");
    await expect(page.locator(".session-sheet-row").first()).toContainText("Home"); // first row
    await expect(pinned.locator(".session-sheet-del")).toHaveCount(0); // and no delete control
    await expect(page.locator(".session-sheet-row").filter({ hasText: OTHER_TITLE })).toBeVisible();
    await expectNoAxeViolations(page, { include: ".session-sheet-pinned" });
  });
});
