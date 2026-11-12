import { expect, test } from "@playwright/test";

// With nothing running (and no cast), the chat status rail above the composer is absent —
// it takes no space at all. The old background strip reserved its ~38px line permanently
// (#4247), which left a blank band between the transcript and the composer most of the time
// (Josh, 2026-10-22). While a job runs, the rail renders the Doing now chip (S6a) — counted
// from the same workGroups selector the Work view reads, this chat only.

const SESSION = "chat-bgwork-idle-e2e";

test("an idle rail is absent; a running job brings up the Doing now chip", async ({ page }) => {
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
  // No undelivered pages: the mock default would put the fleet Needs-you marker (S7) on the
  // idle rail; this spec pins the chip-less rail being ABSENT.
  await page.route("**/api/inbox**", (route) => route.fulfill({ json: { items: [] } }));
  // The REST list follows the bus: empty until the job starts, then the running row — the
  // rail reads the Work sources (GET /api/background), refetched on `background.started`.
  let running = false;
  await page.route("**/api/background", (route) =>
    route.fulfill({
      json: {
        enabled: true,
        jobs: running
          ? [{ id: "bg-idle0001", status: "running", origin_session: SESSION, subagent_type: "delegate", description: "short job" }]
          : [],
      },
    }),
  );
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
  await page.getByPlaceholder(/Message protoAgent/i).waitFor({ state: "visible" });
  await expect(page.getByTestId("chat-status-rail")).toHaveCount(0); // absent, not collapsed

  running = true;
  release();
  const chip = page.getByTestId("chat-status-chip-doing-now");
  await expect(chip).toHaveText("Doing now 1");
  await expect(chip).toHaveAttribute("aria-label", "Doing now, 1 running in this chat. Show details");
  expect((await chip.boundingBox())?.height ?? 0).toBeGreaterThan(16);
});
