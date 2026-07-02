import { expect, test } from "@playwright/test";

// Background report card (ADR 0070 D4). A finished background job's report renders as a
// real CARD in the spawning chat — raised surface, clamped excerpt with a bottom fade-out,
// an "Open report" CTA — and the document viewer fetches the FULL report BY ID
// (GET /api/background/{id}), never via the legacy list-and-filter.
//
// Harness: seed an open chat session, replace the mock SSE stream with one that emits a
// `background.completed` for that session (BackgroundWatch injects the display-only report
// message), and serve the by-id route with a full result the LIST route does not carry —
// so the viewer showing the full text proves the by-id fetch.

const JOB_ID = "bg-abcdefabcdef";
const SESSION = "chat-bg-e2e";
const TITLE = "Quarterly numbers deep-dive";
// Long enough that the excerpt must clamp (scrollHeight > clientHeight).
const PREVIEW = Array.from({ length: 40 }, (_, i) => `Preview line ${i + 1} of the trimmed result.`).join("\n\n");
const FULL_MARKER = "FULL-REPORT-ONLY-SERVED-BY-ID";
const FULL = `# ${TITLE}\n\n${FULL_MARKER}\n\nThe untruncated report body.`;

test("report card: clamped fading excerpt, Open CTA → docviewer, fetched by id", async ({ page }) => {
  // An open chat session whose id matches the job's origin_session — BackgroundWatch
  // only injects into sessions that are open in this window.
  await page.addInitScript(
    ([session]) => {
      window.localStorage.setItem(
        "protoagent.chat.sessions",
        JSON.stringify({
          version: 1,
          currentSessionId: session,
          sessions: [{ id: session, title: "spawner", createdAt: 1, updatedAt: 2, messages: [] }],
        }),
      );
    },
    [SESSION],
  );

  // SSE: one background.completed frame for the seeded session. The stream then closes;
  // EventSource reconnects and replays it — BackgroundWatch dedupes, so the card renders
  // exactly once even though delivery is repeated.
  const frame = {
    topic: "background.completed",
    data: {
      job_id: JOB_ID,
      origin_session: SESSION,
      status: "completed",
      description: TITLE,
      result: PREVIEW, // the truncated preview the bus event carries
    },
  };
  await page.route("**/api/events**", (route) =>
    route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      body: `data: ${JSON.stringify(frame)}\n\n`,
    }),
  );

  // The by-id route (ADR 0070) carries the FULL result; record its hits.
  const byIdHits: string[] = [];
  await page.route(`**/api/background/${JOB_ID}`, (route) => {
    byIdHits.push(route.request().url());
    return route.fulfill({
      json: {
        id: JOB_ID,
        status: "completed",
        subagent_type: "researcher",
        description: TITLE,
        origin_session: SESSION,
        result: FULL,
      },
    });
  });
  // The LIST route must NOT be the card's source — it answers, but without the report.
  await page.route("**/api/background", (route) => route.fulfill({ json: { enabled: true, jobs: [] } }));

  await page.goto("/app/", { waitUntil: "load" });

  // The card: raised-card wrapper, header row (title + subtitle), CTA.
  const card = page.locator(".chat-report-card");
  await expect(card).toBeVisible({ timeout: 15_000 });
  await expect(card.locator(".chat-report-title")).toHaveText(TITLE);
  await expect(card.locator(".chat-report-sub")).toHaveText("Background report");

  // The excerpt is CLAMPED (content overflows the fade window) and carries the
  // fade-out mask class.
  const excerpt = card.locator(".chat-report-excerpt");
  await expect(excerpt).toBeVisible();
  await expect(excerpt).toContainText("Preview line 1");
  expect(await excerpt.evaluate((el) => el.scrollHeight > el.clientHeight + 1)).toBe(true);
  expect(
    await excerpt.evaluate((el) => {
      const s = getComputedStyle(el);
      return s.maskImage || s.webkitMaskImage || "";
    }),
  ).toContain("linear-gradient");

  // The CTA opens the document viewer with the FULL report — which only the by-id
  // route serves, so its presence + the recorded hit prove the fetch path.
  await card.getByRole("button", { name: "Open report" }).click();
  const viewer = page.locator(".doc-viewer");
  await expect(viewer).toBeVisible();
  await expect(viewer).toContainText(FULL_MARKER);
  expect(byIdHits.length).toBeGreaterThan(0);
});
