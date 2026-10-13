import { expect, test, type Page, type Request } from "@playwright/test";

// #3957: a fresh profile hydrates only the newest SESSION_INDEX_LIMIT sessions. A session
// PARKED on an `ask_human` question that has since sunk below that cut — newer chats piled
// on top of it — was never fetched, so its tab, and the question still waiting for an
// answer, never came back. The console now also reads the `parked=true` index and pins
// those sessions into the set it hydrates.
//
// The mock parks exactly like the real server ("PARK_ASK_HUMAN <label>"). With the
// x-e2e-buried-parked-session header its newest index serves `limit` newer finished
// sessions (never the parked one), and only the parked index names it.

const SLOT = ".chat-session-slot:not([hidden])";
const LABEL = "kumquat";

function a2aLog(page: Page) {
  const bodies: { method: string; params: any }[] = [];
  page.on("request", (req: Request) => {
    if (!req.url().endsWith("/a2a") || req.method() !== "POST") return;
    try {
      const body = JSON.parse(req.postData() || "{}");
      bodies.push({ method: body.method, params: body.params });
    } catch {
      /* not JSON */
    }
  });
  return bodies;
}

test("a parked session older than the newest-50 index still comes back in a fresh profile (#3957)", async ({
  page,
  browser,
}) => {
  const bodies = a2aLog(page);
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.locator(`${SLOT} textarea.pl-prompt__field`);
  await composer.waitFor({ state: "visible" });
  await composer.fill(`PARK_ASK_HUMAN ${LABEL}`);
  await composer.press("Enter");
  await expect(page.locator(`${SLOT} .hitl-float .hitl-card`)).toContainText(`Which fruit goes with ${LABEL}?`);
  const sent = bodies.filter((b) => b.method === "SendStreamingMessage").at(-1);
  const sessionId = String(sent?.params?.message?.contextId ?? "");
  expect(sessionId).toMatch(/^chat-/);

  const context = await browser.newContext({ baseURL: test.info().project.use.baseURL });
  try {
    const fresh = await context.newPage();
    await fresh.setExtraHTTPHeaders({ "x-e2e-buried-parked-session": sessionId });
    const indexReads: string[] = [];
    fresh.on("request", (req) => {
      if (req.url().includes("/api/chat/sessions?")) indexReads.push(req.url());
    });
    await fresh.goto("/app/", { waitUntil: "load" });
    // The newer sessions hydrate either way: wait for them, so the check below is not a race.
    await expect(fresh.locator(".pl-tabbar__tab").first()).toBeVisible();
    await expect.poll(() => indexReads.length).toBeGreaterThan(0);

    // The parked session is among the hydrated tabs, and the set stays within the cap.
    await expect
      .poll(async () =>
        fresh.evaluate(() => {
          const stored = JSON.parse(localStorage.getItem("protoagent.chat.sessions") || "{}");
          return (stored.sessions ?? []).map((session: { id: string }) => session.id);
        }),
      )
      .toContain(sessionId);
    const ids: string[] = await fresh.evaluate(() =>
      (JSON.parse(localStorage.getItem("protoagent.chat.sessions") || "{}").sessions ?? []).map(
        (session: { id: string }) => session.id,
      ),
    );
    expect(ids.length).toBeLessThanOrEqual(50);
    expect(ids.filter((id) => id.startsWith("chat-buried-newer-")).length).toBeGreaterThan(0);

    // Opening its tab brings the waiting question back.
    const tab = fresh.locator(".pl-tabbar__tab").filter({ hasText: `PARK_ASK_HUMAN ${LABEL}` });
    await expect(tab).toHaveCount(1);
    await tab.click();
    await expect(fresh.locator(`${SLOT} .hitl-float .hitl-card`)).toContainText(`Which fruit goes with ${LABEL}?`);
    await expect(fresh.locator(`${SLOT} .chat-paused-indicator`)).toContainText("Waiting for your input");
  } finally {
    await context.close();
  }
});
