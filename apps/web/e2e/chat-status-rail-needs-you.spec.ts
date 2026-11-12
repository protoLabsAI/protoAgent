import { expect, test } from "@playwright/test";
import type { DecisionRow } from "../src/lib/api/decisions";

// The fleet Needs-you marker + decision answer states (work consolidation v2 §5, S7) against
// the real shell: the marker FIRST on the rail (primary, xs; numbers only at 390), counting
// another chat's decision + an undelivered page + a stopped goal; the grouped details' first
// section with the shared DecisionCard; sent / answered-elsewhere / failed-delivery /
// expired / withdrawn / stale, with screenshots per state at both widths, light and dark.
// Count equality with the shared selector is pinned in ChatStatusRail.decisions.test.tsx —
// here the layout truths: first on the row, no sideways scroll, the phone sheet.

const SESSION = "needs-you-e2e";

const decision = (extra: Partial<DecisionRow> = {}): DecisionRow => ({
  id: "d-ship", session_id: "chat-elsewhere", agent: "protoEngineer",
  question: "Ship the release after the checks pass?",
  options: [
    { id: "ship", label: "Ship it", description: "Tag and publish now.", recommended: true },
    { id: "hold", label: "Hold the release" },
  ],
  // 40 minutes pending: old enough for the muted stale hint.
  created_at: new Date(Date.now() - 40 * 60_000).toISOString(),
  state: "pending", answer: null, answered_at: null,
  expires_at: new Date(Date.now() + 2 * 3600_000).toISOString(),
  default_option: "hold", thread_id: "t1", interrupt_id: "i1", context: "",
  default_action: null, withdraw_reason: null, answered_by: null, answered_surface: null,
  default_action_text: "hold the release", answer_label: null, ...extra,
});

type AnswerMode = "sent" | "elsewhere" | "failure" | "expired" | "withdrawn";
const SETTLED: Record<Exclude<AnswerMode, "sent" | "failure">, Partial<DecisionRow>> = {
  elsewhere: { state: "answered", answer: "hold", answer_label: "Hold the release", answered_by: "chat", answered_at: new Date().toISOString() },
  expired: { state: "expired" },
  withdrawn: { state: "withdrawn", withdraw_reason: "The checks answered it." },
};

for (const width of [1440, 390] as const) {
  for (const mode of ["light", "dark"] as const) {
    test(`needs-you marker and answer states at ${width} ${mode}`, async ({ page }) => {
      test.setTimeout(120_000); // six navigations: one per answer state
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.setViewportSize({ width, height: 900 });
      await page.addInitScript(([session]) => {
        localStorage.setItem("protoagent.chat.sessions", JSON.stringify({
          version: 1, currentSessionId: session,
          sessions: [{ id: session, title: "Needs you", createdAt: 1, updatedAt: 2, messages: [] }],
        }));
      }, [SESSION]);

      let pending: DecisionRow[] = [decision()];
      let current: DecisionRow = pending[0];
      let answerMode: AnswerMode = "sent";
      let goals = [{ session_id: "goal-chat", condition: "Green CI on main", status: "exhausted",
        verifier: { type: "command", command: "pytest -q" }, iteration: 8, max_iterations: 8,
        started_at: 1751000000, finished_at: Math.floor(Date.now() / 1000) - 600 }];
      let inbox = [{ id: 9, created_at: "2026-05-30T09:00:00+00:00", priority: "now", source: "ci",
        text: "build failed on main", dedup_key: null, delivered_at: null, needs_human: true }];
      await page.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode, overrides: {} } } }));
      await page.route(/\/api\/chat\/sessions\?/, (route) =>
        route.fulfill({ json: { sessions: [{ session_id: SESSION, last_updated: "2026-11-07T17:00:00Z", turn_count: 1 }] } }));
      await page.route(`**/api/chat/sessions/${SESSION}/directives**`, (route) =>
        route.fulfill({ json: { directives: [], in_force: 0 } }));
      await page.route("**/api/goals", (route) => route.fulfill({ json: { enabled: true, goals } }));
      await page.route("**/api/inbox**", (route) => route.fulfill({ json: { items: inbox } }));
      await page.route("**/api/events**", (route) =>
        route.fulfill({ contentType: "text/event-stream", body: ": idle\n\n" }));
      await page.route("**/api/decisions/*/answer", (route) => {
        if (answerMode === "sent") {
          current = decision({ state: "answered", answer: "ship", answer_label: "Ship it", answered_by: "operator" });
          pending = [];
          return route.fulfill({ json: { decision: current, resumed: true } });
        }
        if (answerMode === "failure") return route.fulfill({ json: { decision: current, resumed: false } });
        current = decision(SETTLED[answerMode]);
        pending = [];
        return route.fulfill({ status: 409, json: { detail: current } });
      });
      await page.route(/\/api\/decisions\/[^/?]+$/, (route) => route.fulfill({ json: current }));
      await page.route("**/api/decisions?**", (route) =>
        route.fulfill({ json: { decisions: pending, pending: pending.length } }));

      const shot = (name: string, p = page) =>
        p.screenshot({ path: `e2e/screenshots/needs-you/${width}-${mode}-${name}.png`, fullPage: true, animations: "disabled" });
      const marker = page.getByTestId("chat-status-chip-needs-you");
      const details = page.locator(".chat-status-details");
      const openPanel = async () => {
        await marker.click();
        await expect(details.locator(".chat-needs-you h2").first()).toBeVisible();
      };

      await page.goto("/app/", { waitUntil: "load" });
      await expect(page.locator("html")).toHaveAttribute("data-theme", mode);

      // The marker: FIRST on the row, fleet-wide 3 (another chat's decision + an undelivered
      // page + a stopped goal), numbers only on the phone — and nothing scrolls sideways.
      await expect(marker).toHaveText(width === 390 ? "3" : "Needs you 3", { useInnerText: true });
      await expect(marker).toHaveAttribute("aria-label", "3 things need you. Show them");
      await expect(page.locator('[data-testid="chat-status-rail"] > :first-child'))
        .toHaveAttribute("data-testid", "chat-status-chip-needs-you");
      const overflow = await page.evaluate(() => ({
        doc: document.documentElement.scrollWidth, vw: window.innerWidth }));
      expect(overflow.doc).toBeLessThanOrEqual(overflow.vw);
      await shot("marker-3");

      // The details' FIRST section: the expanded card (recommended primary + suggested, the
      // stale hint, Reply in chat), the neutral delivery and stopped-goal rows, Open in Work.
      await openPanel();
      await expect(details.locator("h2").first()).toHaveText("Needs you · 3");
      await expect(details.getByRole("button", { name: /Ship it/ })).toBeVisible();
      await expect(details.locator(".chat-needs-you__stale")).toContainText("30 minutes");
      await expect(details).toContainText("build failed on main");
      await expect(details).toContainText("Goal stopped");
      await expect(details.getByRole("button", { name: "Keep going" })).toBeVisible();
      await expect(details.getByRole("button", { name: "Drop the goal" })).toBeVisible();
      if (width === 390) await expect(page.locator(".pl-drawer--bottom")).toBeVisible();
      await shot("section-stale");

      // Consumed success: Sent + Continuing… immediately; the held card stays in the OPEN
      // panel while the marker drops to pending-only (the page + the goal).
      await details.getByRole("button", { name: /Ship it/ }).click();
      await expect(details).toContainText('Sent: "Ship it". Continuing…');
      await expect(marker).toHaveText(width === 390 ? "2" : "Needs you 2", { useInnerText: true });
      await shot("sent");
      await page.keyboard.press("Escape");
      await expect(details).toHaveCount(0);

      // Each remaining outcome from a fresh pending decision (the store is per-load).
      const states: Array<[AnswerMode, string | RegExp]> = [
        ["elsewhere", "Already answered"],
        ["failure", "Couldn't send your answer"],
        ["expired", /went ahead and hold the release/],
        ["withdrawn", "doesn't need this any more. The checks answered it."],
      ];
      for (const [modeName, text] of states) {
        answerMode = modeName;
        pending = [decision()];
        current = pending[0];
        await page.reload({ waitUntil: "load" });
        await openPanel();
        await details.getByRole("button", { name: /Ship it/ }).click();
        await expect(details).toContainText(text);
        if (modeName === "failure") await expect(details.getByRole("button", { name: "Try again" })).toBeVisible();
        await shot(modeName);
        await page.keyboard.press("Escape");
      }

      // Zero hides — and a lone decision reads 1 ("marker-1").
      answerMode = "sent";
      pending = [decision()];
      current = pending[0];
      goals = [];
      inbox = [];
      await page.reload({ waitUntil: "load" });
      await expect(marker).toHaveText(width === 390 ? "1" : "Needs you 1", { useInnerText: true });
      await expect(marker).toHaveAttribute("aria-label", "1 thing needs you. Show it");
      await shot("marker-1");
    });
  }
}
