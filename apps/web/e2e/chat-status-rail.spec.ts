import { expect, test } from "@playwright/test";

// The chat status rail (work consolidation v2 §5, S6a) against the real shell: mixed chips
// counted this-chat-only, the grouped details (popover at 1440, bottom sheet at 390), the
// absent empty rail, the preserved instructions failure state — at both widths, light and
// dark, with screenshots for each state. Also pins "no sideways scroll at 390px".

const SESSION = "chat-status-rail-e2e";

function routes(page: import("@playwright/test").Page, mode: string, opts: { failDirectives?: boolean } = {}) {
  return Promise.all([
    page.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode, overrides: {} } } })),
    // The durable session index (the directive-inventory discovery) — the mock server has no
    // default for it, and a 404 would downgrade a clean ledger read to "stale".
    page.route(/\/api\/chat\/sessions\?/, (route) =>
      route.fulfill({ json: { sessions: [{ session_id: SESSION, last_updated: "2026-11-07T17:00:00Z", turn_count: 1 }] } }),
    ),
    page.route("**/api/goals", (route) =>
      route.fulfill({
        json: {
          enabled: true,
          goals: [{
            session_id: SESSION, condition: "All tests pass", status: "active",
            verifier: { type: "command", command: "pytest -q" }, iteration: 3, max_iterations: 8,
            started_at: 1751000000,
          }],
        },
      }),
    ),
    page.route("**/api/background", (route) =>
      route.fulfill({
        json: {
          enabled: true,
          jobs: [
            { id: "bg-1", status: "running", origin_session: SESSION, subagent_type: "delegate", description: "delegate → sonnet: Land PR #13" },
            { id: "bg-2", status: "running", origin_session: "another-chat", subagent_type: "delegate", description: "elsewhere" },
          ],
        },
      }),
    ),
    page.route("**/api/tasks/issues", (route) =>
      route.fulfill({
        json: {
          issues: [
            { id: "bd-9", title: "Collect the weekly numbers", status: "open", priority: 2, issue_type: "task", session_id: SESSION, created_at: "2026-06-02T09:00:00Z" },
          ],
        },
      }),
    ),
    page.route(`**/api/chat/sessions/${SESSION}/directives**`, (route) =>
      opts.failDirectives
        ? route.fulfill({ status: 500, json: { detail: "ledger unavailable" } })
        : route.fulfill({
            json: {
              directives: [
                { id: "dir-1", session_id: SESSION, quote: "Don't merge anything into main until I say so", conditions: "", scope: "merges", status: "in_force", created_at: "2026-11-07T17:14:02+00:00", closed_by: null, closed_at: null, close_reason: null },
                { id: "dir-2", session_id: SESSION, quote: "Keep replies short", conditions: "", scope: "", status: "in_force", created_at: "2026-11-07T18:00:00+00:00", closed_by: null, closed_at: null, close_reason: null },
              ],
              in_force: 2,
            },
          }),
    ),
    page.route("**/api/events**", (route) =>
      route.fulfill({ contentType: "text/event-stream", body: ": idle\n\n" }),
    ),
  ]);
}

function seedSession(page: import("@playwright/test").Page) {
  return page.addInitScript(([session]) => {
    localStorage.setItem("protoagent.chat.sessions", JSON.stringify({
      version: 1, currentSessionId: session,
      sessions: [{ id: session, title: "Rail chat", createdAt: 1, updatedAt: 2, messages: [] }],
    }));
  }, [SESSION]);
}

for (const width of [1440, 390]) {
  for (const mode of ["light", "dark"]) {
    test(`mixed chips and grouped details at ${width} ${mode}`, async ({ page }, info) => {
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.setViewportSize({ width, height: 900 });
      await seedSession(page);
      await routes(page, mode);
      await page.goto("/app/", { waitUntil: "load" });
      await expect(page.locator("html")).toHaveAttribute("data-theme", mode);

      // Exact §5 chips, counted this-chat-only: the other chat's job never shows here.
      const doing = page.getByTestId("chat-status-chip-doing-now");
      const coming = page.getByTestId("chat-status-chip-coming-up");
      const told = page.getByTestId("chat-status-chip-instructions");
      // useInnerText: the phone hides the wordy spans with CSS; textContent would still see them.
      await expect(doing).toHaveText(width === 390 ? "2" : "Doing now · goal 3/8 · 2", { useInnerText: true });
      await expect(coming).toHaveText(width === 390 ? "1" : "Coming up 1", { useInnerText: true });
      await expect(told).toHaveText(width === 390 ? "2" : "Told it 2", { useInnerText: true });
      // (The exact spoken labels are pinned once, in ChatStatusRail.test.tsx — not re-checked here.)

      // Nothing scrolls sideways (§6, the 390px / 400%-zoom rule).
      const overflow = await page.evaluate(() => ({
        doc: document.documentElement.scrollWidth,
        vw: window.innerWidth,
      }));
      expect(overflow.doc).toBeLessThanOrEqual(overflow.vw);
      await page.screenshot({ path: info.outputPath("rail-chips.png"), fullPage: true, animations: "disabled" });

      // Grouped details: popover on desktop, the bottom sheet on the phone; Work's names.
      await doing.click();
      const details = page.locator(".chat-status-details");
      await expect(details).toContainText("Doing now · 2");
      await expect(details).toContainText("Coming up · 1");
      await expect(details).toContainText("Your instructions · 2");
      await expect(details).toContainText("Land PR #13");
      await expect(details).toContainText("Collect the weekly numbers");
      await expect(details).toContainText("Don't merge anything into main until I say so");
      if (width === 390) {
        await expect(page.locator(".pl-drawer--bottom")).toBeVisible();
        // Phone ergonomics measured for real, not pinned to the stylesheet's source text:
        // rows at the 48px floor, actions spanning the sheet's full column.
        const row = details.locator(".chat-status-details__row").first();
        expect((await row.boundingBox())!.height).toBeGreaterThanOrEqual(48);
        const actions = details.locator(".chat-status-details__actions").first();
        const action = actions.locator(".pl-btn").first();
        expect((await action.boundingBox())!.width).toBeGreaterThanOrEqual((await actions.boundingBox())!.width - 1);
      }
      await expect(details.getByRole("button", { name: "Open in Work" })).toHaveCount(3);
      await page.screenshot({ path: info.outputPath("rail-details.png"), fullPage: true, animations: "disabled" });

      await page.keyboard.press("Escape");
      await expect(details).toHaveCount(0);
      await expect(doing).toHaveAttribute("aria-expanded", "false");
    });

    test(`empty rail absent and instruction failure at ${width} ${mode}`, async ({ page }, info) => {
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.setViewportSize({ width, height: 900 });
      await seedSession(page);
      await routes(page, mode, { failDirectives: true });
      // Another chat's ledger loads FINE, so the cross-chat summed source reads merely
      // "stale" (its loadedAt is the max across sessions) — the downgrade that used to
      // swallow this chat's failure. The rail must still name THIS chat's problem.
      // (Registered after routes(), so these take precedence over its session-index route.)
      await page.route(/\/api\/chat\/sessions\?/, (route) =>
        route.fulfill({ json: { sessions: [
          { session_id: SESSION, last_updated: "2026-11-07T17:00:00Z", turn_count: 1 },
          { session_id: "other-rail-chat", last_updated: "2026-11-07T16:00:00Z", turn_count: 1 },
        ] } }),
      );
      await page.route("**/api/chat/sessions/other-rail-chat/directives**", (route) =>
        route.fulfill({ json: { directives: [], in_force: 0 } }));
      // Nothing running or planned for this chat: only the failed ledger speaks.
      await page.unroute("**/api/goals");
      await page.route("**/api/goals", (route) => route.fulfill({ json: { enabled: true, goals: [] } }));
      await page.unroute("**/api/background");
      await page.route("**/api/background", (route) => route.fulfill({ json: { enabled: false, jobs: [] } }));
      await page.unroute("**/api/tasks/issues");
      await page.route("**/api/tasks/issues", (route) => route.fulfill({ json: { issues: [] } }));
      await page.goto("/app/", { waitUntil: "load" });
      await expect(page.locator("html")).toHaveAttribute("data-theme", mode);

      // The ledger failed: the Told it chip renders in its problem state without a count.
      // (Its exact spoken label is pinned once, in ChatStatusRail.test.tsx.)
      const told = page.getByTestId("chat-status-chip-instructions");
      await expect(told).toHaveClass(/chat-status-chip--problem/);
      await expect(told.locator(".chat-status-chip__count")).toHaveCount(0);
      await page.screenshot({ path: info.outputPath("rail-instruction-failure.png"), fullPage: true, animations: "disabled" });

      // Load again is one click away, inside the grouped details.
      await told.click();
      const details = page.locator(".chat-status-details");
      await expect(details).toContainText("Couldn't check your instructions.");
      await expect(details.getByRole("button", { name: "Load again" })).toBeVisible();
      if (width === 390) {
        // Two actions here (Load again + Open in Work): stacked on the phone, never side by side.
        const buttons = details.locator(".chat-status-details__actions .pl-btn");
        const [load, open] = [await buttons.nth(0).boundingBox(), await buttons.nth(1).boundingBox()];
        expect(open!.y).toBeGreaterThanOrEqual(load!.y + load!.height);
      }
      await page.keyboard.press("Escape");

      // And once the ledger loads clean with nothing in force, the rail is simply absent.
      await page.unroute(`**/api/chat/sessions/${SESSION}/directives**`);
      await page.route(`**/api/chat/sessions/${SESSION}/directives**`, (route) =>
        route.fulfill({ json: { directives: [], in_force: 0 } }));
      await page.reload({ waitUntil: "load" });
      await page.getByPlaceholder(/Message protoAgent/i).waitFor({ state: "visible" });
      await expect(page.getByTestId("chat-status-rail")).toHaveCount(0);
      await page.screenshot({ path: info.outputPath("rail-empty-absent.png"), fullPage: true, animations: "disabled" });
    });
  }
}
