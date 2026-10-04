import { expect, test } from "@playwright/test";

// The bottom-bar background-agents pill shows a spinner + the running count while jobs run.
// It was a fixed-square icon button, so the count squeezed the spinner into an oval and the
// digit half-hid under the unread dot (Josh, 2026-10-04). Running: the spinner stays round and
// the count is fully inside the pill.

const SESSION = "chat-bgpill-e2e";

function sse(frames: { topic: string; data: Record<string, unknown> }[]) {
  return frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");
}

test("the running pill keeps its spinner round and shows the whole count", async ({ page }) => {
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
  const JOB = {
    job_id: "bg-pill00000001",
    status: "running",
    subagent_type: "delegate",
    description: "delegate → sonnet: a job",
    origin_session: SESSION,
    started_at: Date.now() / 1000,
  };
  await page.route("**/api/background", (route) => route.fulfill({ json: { enabled: true, jobs: [JOB] } }));
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
            data: { job_id: "bg-pill00000001", origin_session: SESSION, subagent_type: "delegate", description: "delegate → sonnet: a job" },
          },
        ]),
      });
    }
    await new Promise((r) => setTimeout(r, 5_000));
    return route.fulfill({ status: 200, headers, body: "" });
  });

  await page.goto("/app/", { waitUntil: "load" });
  const pill = page.getByTestId("background-jobs-pill");
  await expect(pill).toHaveAccessibleName(/1 running/);

  const m = await pill.evaluate((el) => {
    const icon = el.firstElementChild as HTMLElement;
    const r = icon.getBoundingClientRect();
    const count = Array.from(el.querySelectorAll("span")).find((s) => s.textContent?.trim() === "1") as HTMLElement;
    const c = count.getBoundingClientRect();
    const p = el.getBoundingClientRect();
    return { w: r.width, h: r.height, cLeft: c.left, cRight: c.right, iconRight: r.right, pLeft: p.left, pRight: p.right };
  });
  expect(Math.abs(m.w - m.h)).toBeLessThanOrEqual(1); // round, not squished
  expect(m.cLeft).toBeGreaterThanOrEqual(m.iconRight); // the count sits beside the spinner…
  expect(m.cRight).toBeLessThanOrEqual(m.pRight + 0.5); // …and inside the pill
});
