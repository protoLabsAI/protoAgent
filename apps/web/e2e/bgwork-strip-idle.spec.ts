import { expect, test } from "@playwright/test";

// With no background job running, the strip above the composer takes no space. It used to
// reserve its ~38px line permanently (#4247), which left a blank band between the goal strip /
// transcript and the roster most of the time (Josh, 2026-10-22). It still renders (one stable
// live region) and grows back to a full line while a job runs.

const SESSION = "chat-bgwork-idle-e2e";

test("an idle background-work strip collapses; a running one takes its line", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 });
  await page.addInitScript(
    ([session]) => {
      window.localStorage.setItem(
        "protoagent.chat.sessions",
        JSON.stringify({
          version: 1,
          currentSessionId: session,
          sessions: [{ id: session, title: "idle", createdAt: 1, updatedAt: 2, messages: [] }],
        }),
      );
    },
    [SESSION],
  );
  await page.route("**/api/background", (route) => route.fulfill({ json: { enabled: true, jobs: [] } }));
  let release: () => void = () => {};
  const started = new Promise<void>((r) => (release = r));
  let conn = 0;
  await page.route("**/api/events**", async (route) => {
    const headers = { "content-type": "text/event-stream", "cache-control": "no-cache" };
    if (conn++ === 0) {
      await started; // hold the job back until the idle state has been measured
      return route.fulfill({
        status: 200,
        headers,
        body: `data: ${JSON.stringify({
          topic: "background.started",
          data: { job_id: "bg-idle0001", origin_session: SESSION, subagent_type: "delegate", description: "short job" },
        })}\n\n`,
      });
    }
    await new Promise((r) => setTimeout(r, 5_000));
    return route.fulfill({ status: 200, headers, body: "" });
  });

  await page.goto("/app/", { waitUntil: "load" });
  const strip = page.getByTestId("chat-bgwork");
  await expect(strip).toHaveClass(/chat-bgwork--idle/);
  expect((await strip.boundingBox())?.height ?? -1).toBe(0);

  release();
  await expect(strip).toContainText("1 background job running");
  expect((await strip.boundingBox())?.height ?? 0).toBeGreaterThan(28);
});
