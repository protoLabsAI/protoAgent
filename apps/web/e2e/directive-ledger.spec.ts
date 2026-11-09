import { expect, test, type Page } from "@playwright/test";

// The directive-ledger view (#4861, ADR 0127 D6) made visible: the quiet ledger STRIP above
// the composer, the "Standing instructions" DRAWER (Active + History + the inline withdraw
// confirm), and the "You withdrew: …" transcript row. It drives the real console against a
// mocked `…/directives` API (following bgwork-strip-idle.spec.ts), runs the withdraw flow end
// to end, confirms a bare `/directives` opens the drawer, and captures the review screenshots
// at 1440 px and 390 px in both light and dark theme. Screenshots land under the test's
// output dir (testInfo.outputPath), the same convention as chat-frame-scrollback.spec.ts — so
// a CI run never dirties a tracked file. Josh reviews the PR visually; it ships as a DRAFT.

const SESSION = "chat-directive-e2e";

type Row = {
  id: string;
  session_id: string;
  quote: string;
  conditions: string;
  scope: string;
  status: "in_force" | "fulfilled" | "revoked";
  created_at: string;
  closed_by: "agent" | "operator" | null;
  closed_at: string | null;
  close_reason: string | null;
};

function row(over: Partial<Row> & { id: string }): Row {
  return {
    session_id: SESSION,
    quote: "an instruction",
    conditions: "",
    scope: "",
    status: "in_force",
    created_at: "2026-11-07T17:14:02+00:00",
    closed_by: null,
    closed_at: null,
    close_reason: null,
    ...over,
  };
}

const RELEASE_QUOTE =
  "once #4801 and #4808 part 1 merge, cut the release, stopping if any smoke test fails";

function active1(): Row[] {
  return [row({ id: "dir-a10001", quote: RELEASE_QUOTE, conditions: "both PRs are green", scope: "release v0.231.0" })];
}

function active3(): Row[] {
  return [
    row({ id: "dir-a10001", quote: RELEASE_QUOTE, conditions: "both PRs are green", scope: "release v0.231.0", created_at: "2026-11-07T17:14:02+00:00" }),
    row({ id: "dir-a10002", quote: "keep the nightly digest under 500 words", created_at: "2026-11-07T18:02:00+00:00" }),
    row({ id: "dir-a10003", quote: "never force-push main without asking me first", created_at: "2026-11-07T18:40:00+00:00" }),
  ];
}

function mixed(): Row[] {
  return [
    ...active3(),
    row({ id: "dir-c10001", quote: "ship the changelog with every PR", status: "fulfilled", closed_by: "agent", closed_at: "2026-11-07T19:02:00+00:00" }),
    row({
      id: "dir-c10002",
      quote: "pause all deploys over the weekend",
      status: "revoked",
      closed_by: "operator",
      closed_at: "2026-11-07T19:20:00+00:00",
      close_reason: "the freeze is lifted",
    }),
  ];
}

type State = { rows: Row[]; fail: boolean };

/** Route the `…/directives` list + DELETE off a mutable state object, so a withdraw flips the
 *  row to `revoked` and the follow-up refetch sees it move to History. */
async function mockDirectives(page: Page, state: State) {
  await page.route(/\/api\/chat\/sessions\/[^/]+\/directives/, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "DELETE") {
      const id = url.pathname.split("/").pop() ?? "";
      const reason = url.searchParams.get("reason") ?? "";
      const target = state.rows.find((r) => r.id === id);
      if (target) {
        target.status = "revoked";
        target.closed_by = "operator";
        target.closed_at = "2026-11-07T19:30:00+00:00";
        target.close_reason = reason.trim() || "revoked by the operator";
      }
      return route.fulfill({ json: { directive: target ?? null } });
    }
    if (state.fail) return route.fulfill({ status: 500, json: { detail: "mock directives failure" } });
    return route.fulfill({
      json: { directives: state.rows, in_force: state.rows.filter((r) => r.status === "in_force").length },
    });
  });
}

/** Everything the app fetches at boot that would otherwise churn or clutter a screenshot:
 *  an idle background strip, a held-open (never-resolving) event stream so the console stays
 *  "connecting" with no error banner and no refetch loop, and an empty server session index +
 *  turns so the seeded localStorage transcript is authoritative. */
async function routeBoot(page: Page) {
  await page.route("**/api/background", (route) => route.fulfill({ json: { enabled: true, jobs: [] } }));
  await page.route("**/api/events**", () => {
    /* hold the SSE connection open: no frames, no close, no reconnect churn */
  });
  await page.route(/\/api\/chat\/sessions(\?|$)/, (route) => route.fulfill({ json: { sessions: [] } }));
  await page.route(/\/api\/chat\/sessions\/[^/]+\/turns/, (route) => route.fulfill({ json: { turns: [] } }));
}

type Msg = Record<string, unknown>;

async function seed(page: Page, opts: { theme: "light" | "dark"; messages?: Msg[] }) {
  await page.addInitScript(
    ([session, messages, mode]) => {
      window.localStorage.setItem(
        "protoagent.chat.sessions",
        JSON.stringify({
          version: 1,
          currentSessionId: session,
          sessions: [{ id: session, title: "ledger", createdAt: 1, updatedAt: 2, messages }],
        }),
      );
      // The DS ThemePanel's localStorage blob (agentTheme.ts PL_THEME_KEY) — a mode-only
      // theme forces `data-theme` on <html>, which is all the screenshots need.
      window.localStorage.setItem("pl-theme", JSON.stringify({ mode }));
    },
    [SESSION, opts.messages ?? [], opts.theme] as const,
  );
  await page.emulateMedia({ colorScheme: opts.theme });
}

const drawer = (page: Page) => page.locator(".pl-drawer");

/** Open the drawer the way a bare `/directives` does: fill the composer and click Send, the
 *  mouse path that RUNS a client command (the slash menu closes) — see commands.spec.ts. */
async function openViaCommand(page: Page) {
  const composer = page.getByPlaceholder(/Message/i);
  await composer.fill("/directives");
  await page.getByRole("button", { name: /^send$/i }).click();
  await expect(drawer(page)).toBeVisible();
  await expect(drawer(page)).toContainText("Standing instructions");
  // Clear the leftover `/directives` draft so the slash autocomplete popover closes and a
  // clean screenshot isn't taken with it hanging over the composer.
  await composer.fill("");
  await expect(page.locator(".slash-menu")).toBeHidden();
}

const MATRIX = [
  { tag: "1440-light", w: 1440, h: 900, theme: "light" as const },
  { tag: "1440-dark", w: 1440, h: 900, theme: "dark" as const },
  { tag: "390-light", w: 390, h: 844, theme: "light" as const },
  { tag: "390-dark", w: 390, h: 844, theme: "dark" as const },
];

for (const v of MATRIX) {
  const mobile = v.w === 390;
  const shot = (page: Page, name: string, testInfo: import("@playwright/test").TestInfo) =>
    // `animations: "disabled"` fast-forwards the Drawer's slide-in transition to completion,
    // so a capture taken right after it opens shows the panel fully in place, not mid-slide.
    page.screenshot({ path: testInfo.outputPath(`${name}-${v.tag}.png`), animations: "disabled" });

  test(`strip — 1 active and 3 active (${v.tag})`, async ({ page }, testInfo) => {
    const state: State = { rows: active1(), fail: false };
    await seed(page, { theme: v.theme });
    await routeBoot(page);
    await mockDirectives(page, state);
    await page.setViewportSize({ width: v.w, height: v.h });
    await page.goto("/app/", { waitUntil: "load" });

    const strip = page.locator(".chat-ledger");
    await expect(strip).toContainText(mobile ? "1 active" : "1 active instruction");
    await expect(strip).not.toHaveClass(/chat-ledger--idle/);
    await shot(page, "strip-1active", testInfo);

    state.rows = active3();
    await page.reload({ waitUntil: "load" });
    await expect(strip).toContainText(mobile ? "3 active" : "3 active instructions");
    await shot(page, "strip-3active", testInfo);
  });

  test(`drawer — several active, History expanded, and the inline withdraw confirm (${v.tag})`, async ({ page }, testInfo) => {
    const state: State = { rows: mixed(), fail: false };
    await seed(page, { theme: v.theme });
    await routeBoot(page);
    await mockDirectives(page, state);
    await page.setViewportSize({ width: v.w, height: v.h });
    await page.goto("/app/", { waitUntil: "load" });

    await page.locator(".chat-ledger").getByRole("button", { name: "View" }).click();
    await expect(drawer(page)).toBeVisible();
    await expect(drawer(page).getByText("Active (3)")).toBeVisible();
    await drawer(page).getByRole("button", { name: /History \(2\)/ }).click();
    await expect(drawer(page).getByText(/Withdrawn by you/)).toBeVisible();
    await shot(page, "drawer-active-history", testInfo);

    await drawer(page).getByRole("button", { name: "Withdraw…", exact: true }).first().click();
    await expect(drawer(page).getByText("Withdraw this instruction?")).toBeVisible();
    await shot(page, "drawer-withdraw-confirm", testInfo);
  });

  test(`drawer — empty and failed-load (${v.tag})`, async ({ page }, testInfo) => {
    const state: State = { rows: [], fail: false };
    await seed(page, { theme: v.theme });
    await routeBoot(page);
    await mockDirectives(page, state);
    await page.setViewportSize({ width: v.w, height: v.h });
    await page.goto("/app/", { waitUntil: "load" });

    await openViaCommand(page);
    await expect(drawer(page).getByText("No standing instructions")).toBeVisible();
    await shot(page, "drawer-empty", testInfo);

    // Flip the list to a hard failure and reload (which dismisses the drawer) — the failed
    // load is its own state, reached cleanly from a fresh page.
    state.fail = true;
    await page.reload({ waitUntil: "load" });
    await openViaCommand(page);
    await expect(drawer(page).getByText("Couldn't load standing instructions")).toBeVisible();
    await shot(page, "drawer-failed", testInfo);
  });

  test(`transcript — the "You withdrew" row renders as a quiet event, not a user bubble (${v.tag})`, async ({ page }, testInfo) => {
    await seed(page, { theme: v.theme });
    await routeBoot(page);
    // Exercise the real /turns wire contract, not a pre-tagged localStorage row.
    await page.route(/\/api\/chat\/sessions(\?|$)/, (route) => route.fulfill({ json: {
      sessions: [{ session_id: SESSION, last_updated: "2026-11-07T19:30:00Z", turn_count: 2 }],
    } }));
    await page.route(/\/api\/chat\/sessions\/[^/]+\/turns/, (route) => route.fulfill({ json: {
      live_task_id: null,
      turns: [{
        task_id: "approval", state: "TASK_STATE_COMPLETED", last_updated: "2026-11-07T19:00:00Z",
        text: "Recorded. I'll cut the release once both PRs merge.",
        history: [{ role: "ROLE_USER", parts: [{ text: RELEASE_QUOTE }] }],
      }, {
        task_id: "directive-withdrawn-dir-a10001", state: "TASK_STATE_COMPLETED",
        last_updated: "2026-11-07T19:30:00Z", text: "", artifacts: [],
        history: [{
          role: "ROLE_USER",
          parts: [{ text: `You withdrew: "${RELEASE_QUOTE}" — holding the release until Monday` }],
          metadata: { protoagent_directive_withdrawn: { id: "dir-a10001" } },
        }],
      }],
    } }));
    await mockDirectives(page, { rows: [], fail: false });
    await page.setViewportSize({ width: v.w, height: v.h });
    await page.goto("/app/", { waitUntil: "load" });

    const note = page.locator('[data-testid="note-card"]', { hasText: "You withdrew" });
    await expect(note).toBeVisible();
    // The quiet event row is NOT a user bubble: the operator's own words never render in the
    // user-bubble text span.
    await expect(page.locator(".chat-user-text", { hasText: "You withdrew" })).toHaveCount(0);
    await shot(page, "transcript-withdrawn", testInfo);
  });
}

// ── Behaviour flows (one viewport; the matrix above already proves the visuals) ────────────

test("the withdraw flow: Withdraw… → reason → Withdraw moves the entry to History and collapses the strip", async ({ page }) => {
  const state: State = { rows: active1(), fail: false };
  await seed(page, { theme: "light" });
  await routeBoot(page);
  await mockDirectives(page, state);
  await page.route(/\/api\/chat\/sessions(\?|$)/, (route) => route.fulfill({ json: {
    sessions: state.rows[0].status === "revoked"
      ? [{ session_id: SESSION, last_updated: "2026-11-07T19:30:00Z", turn_count: 1 }] : [],
  } }));
  await page.route(/\/api\/chat\/sessions\/[^/]+\/turns/, (route) => route.fulfill({ json: {
    live_task_id: null,
    turns: state.rows[0].status === "revoked" ? [{
      task_id: "directive-withdrawn-dir-a10001", state: "TASK_STATE_COMPLETED",
      last_updated: "2026-11-07T19:30:00Z", text: "", artifacts: [],
      history: [{
        role: "ROLE_USER",
        parts: [{ text: `You withdrew: "${RELEASE_QUOTE}" — ${state.rows[0].close_reason}` }],
        metadata: { protoagent_directive_withdrawn: { id: "dir-a10001" } },
      }],
    }] : [],
  } }));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/app/", { waitUntil: "load" });

  const strip = page.locator(".chat-ledger");
  await expect(strip).toContainText("1 active instruction");
  await strip.getByRole("button", { name: "View" }).click();
  await expect(drawer(page)).toBeVisible();

  await drawer(page).getByRole("button", { name: "Withdraw…", exact: true }).click();
  await drawer(page).getByPlaceholder("For example: holding the release until Monday").fill("holding the release until Monday");
  await drawer(page).getByRole("button", { name: "Withdraw", exact: true }).click();

  // The entry leaves Active (nothing active → the strip collapses to zero height) …
  await expect(strip).toHaveClass(/chat-ledger--idle/);
  await expect(drawer(page).getByText("Nothing is active right now.")).toBeVisible();
  // … and lands in History as a withdrawn entry.
  await drawer(page).getByRole("button", { name: /History \(1\)/ }).click();
  await expect(drawer(page).getByText(/Withdrawn by you/)).toBeVisible();
  await expect(page.locator('[data-testid="note-card"]', { hasText: "You withdrew" })).toHaveCount(1);
  await expect(page.locator(".chat-user-text", { hasText: "You withdrew" })).toHaveCount(0);
});

test("a bare /directives with no argument opens the drawer (its palette entry point)", async ({ page }) => {
  await seed(page, { theme: "light" });
  await routeBoot(page);
  await mockDirectives(page, { rows: active1(), fail: false });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/app/", { waitUntil: "load" });

  await expect(drawer(page)).toBeHidden();
  await openViaCommand(page);
  await expect(drawer(page).getByText(/What this agent believes you've allowed it to do/)).toBeVisible();
});
