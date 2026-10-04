import { expect, test } from "@playwright/test";

// The "N background jobs running · …" strip above the composer is one unwrapped line. It must
// ellipsize inside the chat column — never set the column's minimum width. Before the fix the
// composer wrapper (a grid item with the default min-width:auto) took the strip's full line as
// its floor and pushed the whole chat wider than the panel (Josh, 2026-10-04).

const SESSION = "chat-bgwork-overflow-e2e";
const DESCRIPTION =
  "delegate → protoEngineer: File a protoAgent issue for reliable browser form-filling, decompose it into " +
  "features, board every feature with acceptance criteria, link the parent epic, and report back with the ids";

function sse(frames: { topic: string; data: Record<string, unknown> }[]) {
  return frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");
}

test("a long background-job label ellipsizes instead of widening the chat column", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 });
  await page.addInitScript(
    ([session]) => {
      window.localStorage.setItem(
        "protoagent.chat.sessions",
        JSON.stringify({
          version: 1,
          currentSessionId: session,
          sessions: [{ id: session, title: "lead", createdAt: 1, updatedAt: 2, messages: [] }],
        }),
      );
    },
    [SESSION],
  );
  await page.route("**/api/background", (route) => route.fulfill({ json: { enabled: true, jobs: [] } }));
  await page.route("**/api/background/bg-*", (route) => route.fulfill({ status: 404, json: { detail: "no job" } }));
  let conn = 0;
  await page.route("**/api/events**", async (route) => {
    const headers = { "content-type": "text/event-stream", "cache-control": "no-cache" };
    if (conn++ === 0) {
      return route.fulfill({
        status: 200,
        headers,
        body: sse([
          {
            topic: "background.started",
            data: { job_id: "bg-overflow0001", origin_session: SESSION, subagent_type: "delegate", description: DESCRIPTION },
          },
        ]),
      });
    }
    await new Promise((r) => setTimeout(r, 5_000));
    return route.fulfill({ status: 200, headers, body: "" });
  });

  await page.goto("/app/", { waitUntil: "load" });
  const strip = page.getByTestId("chat-bgwork");
  await expect(strip).toContainText("1 background job running");

  const m = await page.evaluate(() => {
    const slot = document.querySelector(".chat-session-slot:not([hidden])") as HTMLElement;
    const wrap = slot.querySelector(".composer-wrap") as HTMLElement;
    const list = slot.querySelector(".chat-bgwork-list") as HTMLElement;
    const view = slot.querySelector(".chat-bgwork button") as HTMLElement;
    return {
      slotScroll: slot.scrollWidth,
      slotClient: slot.clientWidth,
      wrap: wrap.getBoundingClientRect().width,
      listScroll: list.scrollWidth,
      listClient: list.clientWidth,
      viewRight: view.getBoundingClientRect().right,
      wrapRight: wrap.getBoundingClientRect().right,
    };
  });
  // Nothing overflows the chat column…
  expect(m.slotScroll).toBeLessThanOrEqual(m.slotClient + 1);
  expect(m.wrap).toBeLessThanOrEqual(m.slotClient + 1);
  // …the label is the thing that gives (it's clipped, so its content is wider than its box)…
  expect(m.listScroll).toBeGreaterThan(m.listClient);
  // …and View stays on screen inside the composer.
  expect(m.viewRight).toBeLessThanOrEqual(m.wrapRight + 1);
});
