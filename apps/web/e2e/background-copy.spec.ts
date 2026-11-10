import { expect, test } from "@playwright/test";

// Copy a finished background job's FULL result out of the Work Background section (#2352).
//
// The reported friction: a delegate's reply landed in the panel and the operator had to
// select several thousand words out of a scrolling markdown pane by hand. The panel
// hydrates from GET /api/background (which carries the full result), so the button copies
// what is already in the row — the assertion that matters is that the clipboard holds the
// WHOLE report, tail included, not a preview.

const JOB_ID = "bg-copyable0001";
// Long enough that the result is clamped — i.e. the case where hand-selecting is painful
// and the copy affordance must stay reachable rather than scrolling away with the text.
const BODY = Array.from({ length: 60 }, (_, i) => `Result line ${i + 1} of the delegate's reply.`).join("\n\n");
const TAIL_MARKER = "TAIL-OF-THE-REPLY-42";
const FULL = `# Delegate reply\n\n${BODY}\n\n${TAIL_MARKER}`;

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

test("copies a finished job's full result, tail included", async ({ page }) => {
  await page.route("**/api/background", (route) =>
    route.fulfill({
      json: {
        enabled: true,
        jobs: [
          {
            id: JOB_ID,
            agent_name: "a",
            origin_session: "chat-x",
            subagent_type: "delegate",
            description: "delegate → andrew",
            status: "completed",
            result: FULL,
            notified: true,
            created_at: "2026-08-01T10:00:00+00:00",
            completed_at: "2026-08-01T10:04:00+00:00",
            a2a_task_id: "",
            origin_incognito: false,
            batch_id: null,
            dismissed: false,
            deterministic: true,
          },
        ],
      },
    }),
  );

  await page.goto("/app/", { waitUntil: "load" });

  await page.getByRole("button", { name: /^Background agents/ }).click();
  const work = page.locator('.work-view[data-frame="modal"]');
  await expect(work.getByRole("tab", { selected: true })).toContainText("Background agents");
  await work.locator(".work-row").click();
  await expect(work.locator(".work-background__result")).toBeVisible();

  const copyBtn = work.getByRole("button", { name: "Copy result to clipboard" });
  await expect(copyBtn).toBeVisible();
  await copyBtn.click();

  // Confirmation state (the acceptance criterion from the issue).
  await expect(copyBtn).toHaveText(/Copied/);

  // The whole report reached the clipboard — the tail is the proof it wasn't a preview.
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  expect(clip).toContain(TAIL_MARKER);
  expect(clip).toContain("Result line 1 of");
  expect(clip.length).toBeGreaterThan(2000); // > the 2000-char background.completed preview
});

// Empty jobs still expose their metadata, but have no result or expansion controls.
test("a job with no result and no tools has no result controls", async ({ page }) => {
  await page.route("**/api/background", (route) =>
    route.fulfill({
      json: {
        enabled: true,
        jobs: [
          {
            id: "bg-empty0001",
            origin_session: "chat-not-open",
            subagent_type: "delegate",
            description: "finished with an empty reply",
            status: "completed",
            result: "",
            created_at: "2026-08-01T10:00:00+00:00",
            completed_at: "2026-08-01T10:04:00+00:00",
          },
        ],
      },
    }),
  );

  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: /^Background agents/ }).click();

  const work = page.locator('.work-view[data-frame="modal"]');
  await work.locator(".work-row").click();
  await expect(work.locator(".work-view__detail")).toContainText("finished with an empty reply");
  await expect(work.locator(".work-background__result")).toHaveCount(0);
  await expect(work.getByRole("button", { name: "Show full text" })).toHaveCount(0);
  await expect(work.getByRole("button", { name: "Copy result to clipboard" })).toHaveCount(0);
});
