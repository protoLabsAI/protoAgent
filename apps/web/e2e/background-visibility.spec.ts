import { expect, test } from "@playwright/test";

// #2692 — a background job's completion could silently fail to render live in its
// origin chat tab, with only a generic (and sometimes misleading) toast as fallback.
// Two fixes covered here:
//   1. The Background-agents pill's unread badge is DURABLE (localStorage-backed, keyed
//      by job id) — not a volatile React counter — so a reload must not resurrect a
//      badge for a job the user already opened the panel to review, and must still show
//      one for a job it hasn't (the panel hydrates from GET /api/background regardless
//      of whether the live bus event ever arrived).
//   2. When the origin session isn't a locally open tab, the toast no longer claims
//      "open the chat to read it" — there is no chat to open in that case — it points
//      at the durable, tab-independent Background-agents panel instead.

const JOB_ID = "bg-visibility0001";
const DESC = "sweep the queue for stale PRs";

function mockFinishedJob(page: import("@playwright/test").Page) {
  return page.route("**/api/background", (route) =>
    route.fulfill({
      json: {
        enabled: true,
        jobs: [
          {
            id: JOB_ID,
            origin_session: "session-not-open-here",
            subagent_type: "sweeper",
            description: DESC,
            status: "completed",
            result: "12 stale PRs found",
            created_at: "2026-08-01T10:00:00+00:00",
            completed_at: "2026-08-01T10:04:00+00:00",
          },
        ],
      },
    }),
  );
}

test("the unread badge is durable across reload once the panel has been opened", async ({ page }) => {
  await mockFinishedJob(page);
  await page.goto("/app/", { waitUntil: "load" });

  const pill = page.getByRole("button", { name: /^Background agents/ });
  await expect(pill).toBeVisible();
  await expect(page.locator(".bg-jobs-unread")).toBeVisible();

  // Opening the panel marks the job "seen" — persisted to localStorage, not volatile
  // React state, so it must survive a reload.
  await pill.click();
  await expect(page.locator(".bg-jobs-row")).toBeVisible();
  await page.keyboard.press("Escape");

  await page.reload({ waitUntil: "load" });
  await expect(pill).toBeVisible();
  await expect(page.locator(".bg-jobs-unread")).toHaveCount(0);
});

test("a completion for a session that isn't a local tab toasts the panel, not a dead 'open the chat'", async ({
  page,
}) => {
  await page.route("**/api/background", (route) => route.fulfill({ json: { enabled: true, jobs: [] } }));
  await page.route("**/api/events**", (route) =>
    route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      body: `data: ${JSON.stringify({
        topic: "background.completed",
        data: {
          job_id: JOB_ID,
          origin_session: "session-never-opened-in-this-browser",
          status: "completed",
          description: DESC,
          result: "done",
        },
      })}\n\n`,
    }),
  );

  await page.goto("/app/", { waitUntil: "load" });

  const toast = page.locator(".pl-toast", { hasText: DESC });
  await expect(toast).toBeVisible({ timeout: 15_000 });
  await expect(toast).toContainText("see the Background agents panel");
  await expect(toast).not.toContainText("open the chat to read it");
});
