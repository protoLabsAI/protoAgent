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
// A SHORT report that fits entirely: the fade mask must NOT apply (an unconditional
// mask would ghost the report's last line into a fake truncation).
const SHORT_JOB_ID = "bg-abcdefabcd99";
const SHORT_TITLE = "Quick store check";
const SHORT_PREVIEW = "All 4 stores match the drive.\n\nNothing to fix.";
// A ONE-LINE success skips the card entirely (#1651): the preview IS the whole result,
// so it renders as a compact inline note — no card wrapper, no open-report CTA.
const NOTE_JOB_ID = "bg-oneliner77";
const NOTE_TITLE = "ingest youtube video";
const NOTE_RESULT = "Ingested 'YouTube: uD4-uy0GmHE' → 15 chunk(s)";

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

  // SSE: two background.completed frames for the seeded session — a long report (must
  // clamp + fade) and a short one (must NOT fade). The stream then closes; EventSource
  // reconnects and replays them — BackgroundWatch dedupes, so each card renders exactly
  // once even though delivery is repeated.
  const frames = [
    {
      topic: "background.completed",
      data: {
        job_id: JOB_ID,
        origin_session: SESSION,
        status: "completed",
        description: TITLE,
        result: PREVIEW, // the truncated preview the bus event carries
      },
    },
    {
      topic: "background.completed",
      data: {
        job_id: SHORT_JOB_ID,
        origin_session: SESSION,
        status: "completed",
        description: SHORT_TITLE,
        result: SHORT_PREVIEW,
      },
    },
    {
      topic: "background.completed",
      data: {
        job_id: NOTE_JOB_ID,
        origin_session: SESSION,
        status: "completed",
        description: NOTE_TITLE,
        result: NOTE_RESULT,
      },
    },
  ];
  await page.route("**/api/events**", (route) =>
    route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      body: frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join(""),
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

  // The long-report card: raised-card wrapper, header row (title + subtitle), CTA.
  const card = page.locator(".chat-report-card").filter({ hasText: TITLE });
  await expect(card).toBeVisible({ timeout: 15_000 });
  await expect(card.locator(".chat-report-title")).toHaveText(TITLE);
  await expect(card.locator(".chat-report-sub")).toHaveText("Background report");

  // The excerpt is CLAMPED (content overflows the fade window) and carries the
  // fade-out mask.
  const excerpt = card.locator(".chat-report-excerpt");
  await expect(excerpt).toBeVisible();
  await expect(excerpt).toContainText("Preview line 1");
  expect(await excerpt.evaluate((el) => el.scrollHeight > el.clientHeight + 1)).toBe(true);
  await expect(excerpt).toHaveClass(/chat-report-excerpt--clamped/);
  expect(
    await excerpt.evaluate((el) => {
      const s = getComputedStyle(el);
      return s.maskImage || s.webkitMaskImage || "";
    }),
  ).toContain("linear-gradient");

  // The SHORT report fits entirely — no clamp, and crucially NO fade mask (an
  // unconditional mask would ghost its final line into a fake truncation).
  const shortExcerpt = page
    .locator(".chat-report-card")
    .filter({ hasText: SHORT_TITLE })
    .locator(".chat-report-excerpt");
  await expect(shortExcerpt).toBeVisible();
  await expect(shortExcerpt).toContainText("Nothing to fix.");
  expect(await shortExcerpt.evaluate((el) => el.scrollHeight > el.clientHeight + 1)).toBe(false);
  await expect(shortExcerpt).not.toHaveClass(/chat-report-excerpt--clamped/);
  expect(
    await shortExcerpt.evaluate((el) => {
      const s = getComputedStyle(el);
      return s.maskImage === "none" ? "" : s.maskImage || s.webkitMaskImage || "";
    }),
  ).not.toContain("linear-gradient");

  // A ONE-LINE success renders as a compact inline note (#1651): the desc + result
  // in a `.chat-note`, success-tinted, with NO report card and NO open-report CTA.
  const note = page.locator(".chat-note").filter({ hasText: "15 chunk(s)" });
  await expect(note).toBeVisible();
  await expect(note).toContainText(NOTE_TITLE);
  await expect(note).toHaveClass(/chat-note--success/);
  await expect(page.locator(".chat-report-card").filter({ hasText: NOTE_TITLE })).toHaveCount(0);

  // The CTA opens the document viewer with the FULL report — which only the by-id
  // route serves, so its presence + the recorded hit prove the fetch path.
  await card.getByRole("button", { name: "Open report" }).click();
  const viewer = page.locator(".doc-viewer");
  await expect(viewer).toBeVisible();
  await expect(viewer).toContainText(FULL_MARKER);
  expect(byIdHits.length).toBeGreaterThan(0);
});
