import { expect, test } from "@playwright/test";
import type { DecisionRow } from "../src/lib/api/decisions";

// A long decision in the Needs-you panel (three options, each with an explanation, an expiry
// note and the stale hint) used to grow the panel past the top of the window, cutting off its
// "Needs you · N" heading. The cards and rows now scroll inside the panel instead.

const SESSION = "needs-you-tall-e2e";
const long = "Read-only: I confirm which tools the run had, which skill the agent loaded, and how this case did on earlier nights. Then I come back with a firmer recommendation.";
const row: DecisionRow = {
  id: "d-tall", session_id: "chat-elsewhere", agent: "projectManager",
  question: 'One nightly test case, "research_delegation", has failed since 18 November. The agent researched a topic itself instead of handing the work to a research helper, and a rerun failed the same way. What should I do with it?',
  options: [
    { id: "a", label: "Move it to the tracked tier", description: long, recommended: true },
    { id: "b", label: "Check a few things first", description: long },
    { id: "c", label: "Leave it as it is", description: long },
    { id: "d", label: "Ask the team", description: long },
  ],
  created_at: new Date(Date.now() - 40 * 60_000).toISOString(),
  state: "pending", answer: null, answered_at: null,
  expires_at: new Date(Date.now() + 20 * 3600_000).toISOString(),
  default_option: "c", thread_id: "t1", interrupt_id: "i1", context: "",
  default_action: null, withdraw_reason: null, answered_by: null, answered_surface: null,
  default_action_text: "leave the case as it is and stop asking about it", answer_label: null,
};

for (const height of [900, 620] as const) {
  test(`a tall decision stays inside the window at ${height}px high`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.setViewportSize({ width: 1440, height });
    await page.addInitScript(([session]) => {
      localStorage.setItem("protoagent.ui", JSON.stringify({ state: { rightCollapsed: true }, version: 14 }));
      localStorage.setItem("protoagent.chat.sessions", JSON.stringify({
        version: 1, currentSessionId: session,
        sessions: [{ id: session, title: "Needs you", createdAt: 1, updatedAt: 2, messages: [] }],
      }));
    }, [SESSION]);
    await page.route(/\/api\/chat\/sessions\?/, (route) =>
      route.fulfill({ json: { sessions: [{ session_id: SESSION, last_updated: "2026-11-07T17:00:00Z", turn_count: 1 }] } }));
    await page.route(`**/api/chat/sessions/${SESSION}/directives**`, (route) =>
      route.fulfill({ json: { directives: [], in_force: 0 } }));
    await page.route("**/api/goals", (route) => route.fulfill({ json: { enabled: true, goals: [] } }));
    await page.route("**/api/inbox**", (route) => route.fulfill({ json: { items: [] } }));
    await page.route(/\/api\/decisions\/[^/?]+$/, (route) => route.fulfill({ json: row }));
    await page.route("**/api/decisions?**", (route) => route.fulfill({ json: { decisions: [row], pending: 1 } }));

    await page.goto("/app/", { waitUntil: "load" });
    await page.getByTestId("chat-status-chip-needs-you").click();
    const panel = page.locator(".chat-status-details").filter({ has: page.locator(".chat-needs-you") });
    const heading = panel.locator(".chat-needs-you h2").first();
    await expect(heading).toBeVisible();

    // The whole panel, heading included, is inside the window.
    const box = (await panel.boundingBox())!;
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.y + box.height).toBeLessThanOrEqual(height);
    expect((await heading.boundingBox())!.y).toBeGreaterThanOrEqual(0);

    // The cards scroll inside it, and "Open in Work" stays reachable below them.
    const scroller = panel.locator(".chat-needs-you__scroll");
    const sizes = await scroller.evaluate((el) => ({ scroll: el.scrollHeight, client: el.clientHeight }));
    expect(sizes.scroll).toBeGreaterThan(sizes.client);
    await expect(panel.getByRole("button", { name: "Open in Work" })).toBeInViewport();
    // Every option can be reached by scrolling.
    await panel.getByRole("button", { name: "Ask the team" }).scrollIntoViewIfNeeded();
    await expect(panel.getByRole("button", { name: "Ask the team" })).toBeInViewport();
  });
}
