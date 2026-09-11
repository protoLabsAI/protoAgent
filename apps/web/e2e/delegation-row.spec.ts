import { expect, test } from "@playwright/test";

// A background delegation, as the operator sees it. Before this, one `delegate_to
// (background=true)` put the WHOLE prompt in the chat as a bubble, then a message signed by
// the delegate that was really the tool's receipt to the model ("… I should END my turn now
// …"), then — when the job finished — a note repeating the result next to the delegate's
// actual reply. Now: one row (who · background · the lead's summary · live status) with the
// brief behind a toggle, and a strip above the composer while the chat has work running.

const SESSION = "chat-delegation-e2e";
const JOB = "bg-4109c71161eb";
const DESCRIPTION = "delegate → sonnet: Land PR #13 and close #12";

async function until(ready: () => boolean, budgetMs = 15_000): Promise<void> {
  const deadline = Date.now() + budgetMs;
  while (!ready() && Date.now() < deadline) await new Promise((r) => setTimeout(r, 25));
}

function sse(frames: { topic: string; data: Record<string, unknown> }[]) {
  return frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join("");
}

test("a background delegation is one row that tracks its job, and the chat shows work is running", async ({
  page,
}) => {
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
  // The job list the chat's background-work store hydrates from (nothing running yet), and
  // no single-job row to find — the live bus events below are what it learns from.
  await page.route("**/api/background", (route) => route.fulfill({ json: { enabled: true, jobs: [] } }));
  await page.route("**/api/background/bg-*", (route) => route.fulfill({ status: 404, json: { detail: "no job" } }));

  // The bus, released by the SPEC (the server-turn spec's pattern): the job starts once the
  // row is on screen, and finishes on the reconnect once the running state has been seen.
  let conn = 0;
  let startReleased = false;
  let doneReleased = false;
  await page.route("**/api/events**", async (route) => {
    const n = conn++;
    const headers = { "content-type": "text/event-stream", "cache-control": "no-cache" };
    if (n === 0) {
      await until(() => startReleased);
      return route.fulfill({
        status: 200,
        headers,
        body: sse([
          {
            topic: "background.started",
            data: { job_id: JOB, origin_session: SESSION, subagent_type: "delegate", description: DESCRIPTION },
          },
        ]),
      });
    }
    if (n === 1) {
      await until(() => doneReleased);
      return route.fulfill({
        status: 200,
        headers,
        body: sse([
          {
            topic: "background.completed",
            data: {
              job_id: JOB,
              status: "completed",
              subagent_type: "delegate",
              origin_session: SESSION,
              description: DESCRIPTION,
              result: "PR #13 merged. PR #12 closed.",
            },
          },
        ]),
      });
    }
    await new Promise((r) => setTimeout(r, 5_000)); // later reconnects: quiet, not a hot loop
    return route.fulfill({ status: 200, headers, body: "" });
  });

  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("DELEGATE_BG hand the portfolio work to sonnet");
  await composer.press("Enter");

  // ONE row: who, background, the lead's summary — not the prompt.
  const row = page.locator(".chat-delegation-row");
  await expect(row).toHaveCount(1);
  await expect(row.locator(".chat-delegation-target")).toHaveText("@sonnet");
  await expect(row.locator(".chat-delegation-kind")).toHaveText("background");
  await expect(row.locator(".chat-delegation-summary")).toHaveText("Land PR #13 and close #12");
  await expect(page.getByText("Started it — sonnet will report back.")).toBeVisible();
  await expect(page.getByText(/THREE PHASES/)).toHaveCount(0); // the brief is not in the chat
  await expect(page.getByText(/END my turn|Started a background delegation/)).toHaveCount(0);

  // The brief is one click away.
  await row.getByRole("button", { name: "Show brief" }).click();
  await expect(page.locator(".chat-delegation-brief")).toContainText("THREE PHASES. Do them in order.");
  await row.getByRole("button", { name: "Hide brief" }).click();
  await expect(page.locator(".chat-delegation-brief")).toHaveCount(0);

  // The job starts: the row spins, and the chat says it has work running.
  startReleased = true;
  await expect(row.getByRole("img", { name: "running in the background" })).toBeVisible();
  const strip = page.getByTestId("chat-bgwork");
  await expect(strip).toContainText("1 background job running");
  await expect(strip).toContainText("sonnet: Land PR #13 and close #12");
  await expect(page.locator(".session-dot.processing")).toHaveCount(1); // the tab reads busy too

  // It finishes: ✓ on the row, the strip goes, and NO note repeats the result — the
  // delegate's reply arrives as its own message through the drain.
  doneReleased = true;
  await expect(row.getByRole("img", { name: "finished" })).toBeVisible();
  await expect(strip).toHaveCount(0);
  await expect(page.locator(".session-dot.processing")).toHaveCount(0);
  await expect(page.getByText(/PR #13 merged\. PR #12 closed\./)).toHaveCount(0);
  await expect(page.locator(".chat-note, .chat-report")).toHaveCount(0);
});
