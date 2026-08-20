import { expect, test } from "@playwright/test";

// Background report chip (#2923; supersedes the ADR 0070 D4 teaser card). A finished
// background job's report renders as a single compact dismissable CHIP row in the
// spawning chat — icon + title + "Open" + ✕ — and the document viewer is the whole
// reading experience: it fetches the FULL report BY ID (GET /api/background/{id}),
// never via the legacy list-and-filter. Dismissing hides the chip from the transcript
// (persisted per jobId in localStorage, so a reload doesn't resurrect it); the report
// stays reviewable in the Background agents panel.
//
// Harness: seed an open chat session, replace the mock SSE stream with one that emits
// `background.completed` frames for that session (BackgroundWatch injects display-only
// report messages), and serve the by-id route with a full result the LIST route does
// not carry — so the viewer showing the full text proves the by-id fetch.

const JOB_ID = "bg-abcdefabcdef";
const SESSION = "chat-bg-e2e";
const TITLE = "Quarterly numbers deep-dive";
// The bus event still carries a trimmed multi-line preview — the chip must NOT render it.
const PREVIEW = Array.from({ length: 40 }, (_, i) => `Preview line ${i + 1} of the trimmed result.`).join("\n\n");
const FULL_MARKER = "FULL-REPORT-ONLY-SERVED-BY-ID";
const FULL = `# ${TITLE}\n\n${FULL_MARKER}\n\nThe untruncated report body.`;
// A second finished report — dismissal must be per job, not all-or-nothing.
const OTHER_JOB_ID = "bg-abcdefabcd99";
const OTHER_TITLE = "Quick store check";
const OTHER_PREVIEW = "All 4 stores match the drive.\n\nNothing to fix.";
// A ONE-LINE success skips the chip entirely (#1651): the preview IS the whole result,
// so it renders as a compact inline note — no chip, no Open control.
const NOTE_JOB_ID = "bg-oneliner77";
const NOTE_TITLE = "ingest youtube video";
const NOTE_RESULT = "Ingested 'YouTube: uD4-uy0GmHE' → 15 chunk(s)";

async function setup(page: import("@playwright/test").Page): Promise<string[]> {
  // An open chat session whose id matches the jobs' origin_session — BackgroundWatch
  // only injects into sessions that are open in this window. Seed ONLY when the key is
  // absent: the reload test relies on the transcript chatStore persisted (with the
  // injected report messages in it), and an unconditional re-seed would wipe it on
  // every navigation.
  await page.addInitScript(
    ([session]) => {
      if (!window.localStorage.getItem("protoagent.chat.sessions")) {
        window.localStorage.setItem(
          "protoagent.chat.sessions",
          JSON.stringify({
            version: 1,
            currentSessionId: session,
            sessions: [{ id: session, title: "spawner", createdAt: 1, updatedAt: 2, messages: [] }],
          }),
        );
      }
    },
    [SESSION],
  );

  // SSE: three background.completed frames for the seeded session — two full reports
  // (chips) and a one-line success (inline note). The stream then closes; EventSource
  // reconnects and replays them — BackgroundWatch dedupes, so each renders exactly once
  // even though delivery is repeated.
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
        job_id: OTHER_JOB_ID,
        origin_session: SESSION,
        status: "completed",
        description: OTHER_TITLE,
        result: OTHER_PREVIEW,
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
  // The LIST route feeds the Background agents panel (a dismissed chip must still be
  // reviewable there) — but it carries only the PREVIEW, so the viewer showing the full
  // text still proves the chip's Open path fetches by id, not from the list.
  await page.route("**/api/background", (route) =>
    route.fulfill({
      json: {
        enabled: true,
        jobs: [
          {
            id: JOB_ID,
            status: "completed",
            subagent_type: "researcher",
            description: TITLE,
            origin_session: SESSION,
            result: PREVIEW,
            created_at: "2026-08-01T10:00:00+00:00",
            completed_at: "2026-08-01T10:04:00+00:00",
          },
        ],
      },
    }),
  );
  return byIdHits;
}

test("report chip: one compact row, no excerpt, Open → docviewer fetched by id", async ({ page }) => {
  const byIdHits = await setup(page);
  await page.goto("/app/", { waitUntil: "load" });

  const chip = page.locator(".chat-report-chip").filter({ hasText: TITLE });
  await expect(chip).toBeVisible({ timeout: 15_000 });
  await expect(chip.locator(".chat-report-title")).toHaveText(TITLE);

  // The chip IS the whole notification: no teaser card, no excerpt block, and the
  // multi-line preview the bus event carried must not leak into the transcript.
  await expect(page.locator(".chat-report-card")).toHaveCount(0);
  await expect(page.locator(".chat-report-excerpt")).toHaveCount(0);
  await expect(chip).not.toContainText("Preview line 1");

  // One compact row (~40px), not the old ~150-200px card.
  const box = await chip.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.height).toBeLessThan(60);

  // The second finished job renders its own chip.
  await expect(page.locator(".chat-report-chip").filter({ hasText: OTHER_TITLE })).toBeVisible();

  // A ONE-LINE success still renders as a compact inline note (#1651) — no chip.
  const note = page.locator(".chat-note").filter({ hasText: "15 chunk(s)" });
  await expect(note).toBeVisible();
  await expect(note).toContainText(NOTE_TITLE);
  await expect(note).toHaveClass(/chat-note--success/);
  await expect(page.locator(".chat-report-chip").filter({ hasText: NOTE_TITLE })).toHaveCount(0);

  // "Open" opens the document viewer with the FULL report — which only the by-id route
  // serves (the list carries the preview), so its presence + the recorded hit prove
  // the fetch path.
  await chip.getByRole("button", { name: "Open" }).click();
  const viewer = page.locator(".doc-viewer");
  await expect(viewer).toBeVisible();
  await expect(viewer).toContainText(FULL_MARKER);
  expect(byIdHits.length).toBeGreaterThan(0);
});

test("dismiss ✕ removes the chip, survives reload, and the report stays in the panel", async ({ page }) => {
  await setup(page);
  await page.goto("/app/", { waitUntil: "load" });

  const chip = page.locator(".chat-report-chip").filter({ hasText: TITLE });
  const otherChip = page.locator(".chat-report-chip").filter({ hasText: OTHER_TITLE });
  await expect(chip).toBeVisible({ timeout: 15_000 });
  await expect(otherChip).toBeVisible();

  // Dismiss removes THIS chip from the transcript immediately — the other stays.
  await chip.getByRole("button", { name: "Dismiss report" }).click();
  await expect(chip).toHaveCount(0);
  await expect(otherChip).toBeVisible();

  // Reload: the dismissal is persisted (localStorage, keyed by jobId) — the dismissed
  // chip must NOT resurrect, while the untouched one still renders from the persisted
  // transcript.
  await page.reload({ waitUntil: "load" });
  await expect(otherChip).toBeVisible({ timeout: 15_000 });
  await expect(chip).toHaveCount(0);

  // The dismissed report is still reviewable in the Background agents panel — the
  // dismiss affects chat rendering only, never the durable registry.
  await page.getByTestId("background-jobs-pill").click();
  await expect(page.locator(".bg-jobs-row").filter({ hasText: TITLE })).toBeVisible();
});
