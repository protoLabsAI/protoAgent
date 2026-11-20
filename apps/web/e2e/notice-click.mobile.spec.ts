import { expect, test, type Page } from "@playwright/test";
import type { DecisionRow } from "../src/lib/api/decisions";

// Tapping a push notification on a phone (ADR 0135 W2) must land on the decision's answer
// form, not on whatever screen the console was left on. The push worker reaches the console
// two ways — it opens `/app/?notice=<id>`, or it messages a console that is already open —
// and the notice id for a decision is `decision-<decision id>` (graph/decisions.py).

const row: DecisionRow = {
  id: "dec-4ba15c14", agent: "protoAgent", session_id: "chat-decisions", question: "Tea or coffee?",
  options: [{ id: "tea", label: "Tea" }, { id: "coffee", label: "Coffee" }],
  created_at: "2026-11-19T14:00:00Z", state: "pending", answer: null, answered_at: null,
  expires_at: null, default_option: null, thread_id: "thread-one", interrupt_id: "interrupt-one",
  context: "", default_action: null, withdraw_reason: null, answered_by: null,
  answered_surface: null, default_action_text: "carry on without this step", answer_label: null,
};

async function seed(page: Page) {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.route("**/api/inbox?*", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/decisions?**", (route) => route.fulfill({ json: { decisions: [row], pending: 1 } }));
  await page.route(/\/api\/decisions\/[^/?]+$/, (route) => route.fulfill({ json: { ...row, delivering: false } }));
}

async function expectAnswerForm(page: Page) {
  const detail = page.locator(".work-decision-detail");
  await expect(detail).toBeVisible();
  await expect(detail).toContainText("Tea or coffee?");
  await expect(detail.getByRole("button", { name: "Coffee", exact: true })).toBeVisible();
}

test("mobile: a tap that opens the console lands on the decision's answer form", async ({ page }) => {
  await seed(page);
  await page.goto("/app/?notice=decision-dec-4ba15c14", { waitUntil: "load" });
  await expectAnswerForm(page);
  expect(new URL(page.url()).searchParams.has("notice")).toBe(false);
});

test("mobile: a tap while the console sits on Settings closes Settings and opens the decision", async ({ page }) => {
  await seed(page);
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  await page.getByTestId("app-drawer").getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("button", { name: /Settings sections/ })).toBeVisible();
  // What public/sw.js posts to the focused console on `notificationclick`.
  await page.evaluate(() => navigator.serviceWorker.dispatchEvent(new MessageEvent("message", {
    data: { type: "protoagent:notice-click", v: 1, id: "decision-dec-4ba15c14", route: "/app/?notice=decision-dec-4ba15c14" },
  })));
  await expect(page.getByRole("button", { name: /Settings sections/ })).toBeHidden();
  await expectAnswerForm(page);
});

test("mobile: a decision another agent asked for opens in that agent's console", async ({ page }) => {
  await seed(page);
  // The hub knows which member raised the notice (`GET /api/notices/<id>`, #5614).
  await page.route("**/api/notices/*", (route) => route.fulfill({ json: { ok: true, agent: "protoengineer" } }));
  await page.goto("/app/?notice=decision-dec-4ba15c14", { waitUntil: "load" });
  await expect.poll(() => new URL(page.url()).pathname).toBe("/app/agent/protoengineer/");
  await expectAnswerForm(page);
  expect(new URL(page.url()).searchParams.has("notice")).toBe(false);
});

test("mobile: the console comes to the front with no URL and no message, and still opens the decision", async ({ page }) => {
  await seed(page);
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByTestId("header-menu").click();
  await page.getByTestId("app-drawer").getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("button", { name: /Settings sections/ })).toBeVisible();
  // What public/sw.js leaves on `notificationclick` (`rememberClick`), then the app is foregrounded.
  await page.evaluate(async () => {
    const body = JSON.stringify({ v: 1, id: "decision-dec-4ba15c14", at: Date.now() });
    await (await caches.open("protoagent-notice-click")).put("/app/__notice-click", new Response(body));
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expectAnswerForm(page);
  expect(await page.evaluate(() => caches.keys())).toEqual([]); // read once, nothing left behind
});
