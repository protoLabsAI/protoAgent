import { expect, test, type Locator, type Page } from "@playwright/test";

// Work view additions §2 (#5543): a scheduled job's detail shows its health and a Recent runs
// list. The acceptance point this spec owns is §4's reflow rule — at 390 px nothing scrolls
// sideways, even with a URL-length error in the Health field and in a failed run. It also
// owns the compact Technical details disclosure's collapsed size (at most 32 CSS px, no card
// chrome) and its keyboard toggle.

const LONG_ERROR = `Gateway refused https://ci.example.com/protoAgent/runs?trace=${"a1b2c3d4e5".repeat(16)} after 30 s`;
const CREATED = "2026-11-01T08:00:00+00:00";
const JOB = {
  id: "job-digest-with-a-deliberately-long-identifier-0123456789abcdef0123456789abcdef",
  prompt: "Post the overnight digest to the team channel",
  schedule: "0 9 * * *", agent_name: "protoAgent", enabled: true, timezone: "Europe/Berlin",
  created_at: CREATED, next_fire: "2099-01-01T08:00:00+00:00", last_fire: "2020-11-20T08:00:00+00:00",
  consecutive_failures: 3, last_ok: "2020-11-16T08:00:00+00:00", last_error: LONG_ERROR,
};
const RUN = { job_id: JOB.id, task_id: "task-0123456789abcdef0123456789abcdef", message_id: "msg-0123456789abcdef0123456789abcdef", job_created_at: CREATED };
const RUNS = [
  { ...RUN, scheduled_for: "2020-11-20T08:00:00+00:00", state: "settled", task_state: "failed", ok: false, error: LONG_ERROR, duration_s: 31.2, reported: null },
  { ...RUN, scheduled_for: "2020-11-19T08:00:00+00:00", state: "interrupted_needs_operator", task_state: null, ok: null, error: null },
  { ...RUN, scheduled_for: "2020-11-16T08:00:00+00:00", state: "settled", task_state: "completed", ok: true, error: null, duration_s: 65, reported: false,
    job_created_at: "2026-10-01T08:00:00+00:00" },
];

/** Every collapsed Technical details toggle in `scope`: its height and card chrome. */
async function collapsedToggles(scope: Locator) {
  return scope.locator(".technical-details__toggle").evaluateAll((nodes) => nodes.map((node) => {
    const style = getComputedStyle(node);
    return {
      expanded: node.getAttribute("aria-expanded"),
      short: node.getBoundingClientRect().height <= 32,
      border: style.borderTopWidth !== "0px",
      fill: style.backgroundColor !== "rgba(0, 0, 0, 0)",
    };
  }));
}
const COMPACT = { expanded: "false", short: true, border: false, fill: false };

async function serve(page: Page) {
  await page.route("**/api/scheduler/jobs", (route) => route.request().method() === "GET"
    ? route.fulfill({ json: { backend: "local", jobs: [JOB] } }) : route.fallback());
  await page.route("**/api/scheduler/jobs/*/runs?*", (route) => {
    expect(new URL(route.request().url()).searchParams.get("limit")).toBe("20");
    return route.fulfill({ json: { job_id: JOB.id, runs: RUNS } });
  });
}

for (const theme of ["light", "dark"] as const) {
  test(`phone 390 (${theme}): a failing job's detail shows health and Recent runs with no sideways scroll`, async ({ page }, info) => {
    await page.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode: theme, overrides: {} } } }));
    await serve(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/app/");
    await page.getByRole("button", { name: "Open Work", exact: true }).click();
    const work = page.locator(".work-view:visible");

    // §2.2: the row keeps its Time badge, gains a warning dot, and its meta ends with the note.
    const row = work.locator(".work-row", { hasText: JOB.prompt });
    await expect(row.locator(".pl-dot--warning")).toBeVisible();
    await expect(row.locator(".pl-badge")).toHaveText("Time");
    await expect(row.locator(".work-row__meta")).toHaveText("Every day at 09:00 · failed the last 3 runs");

    await row.click();
    const detail = work.locator(".work-view__detail");
    await expect(detail.locator("h2")).toBeFocused();
    await expect(detail).toContainText("Failed the last 3 runs. Last worked Nov 16 2020 09:00 Europe/Berlin.");

    // §2.3: an h3 labelling a ul, one li per run, each time in the job's clock with its zone.
    const runs = detail.getByRole("list", { name: "Recent runs" });
    await expect(detail.getByRole("heading", { level: 3, name: "Recent runs" })).toBeVisible();
    await expect(runs.getByRole("listitem")).toHaveCount(3);
    await expect(runs.locator(".job-run__word")).toHaveText(["Failed", "Waiting on you", "Ran"]);
    await expect(runs.getByRole("listitem").first()).toContainText("Nov 20 2020 09:00 Europe/Berlin");
    await expect(runs.getByRole("listitem").nth(2)).toContainText("Completed · stayed silent · From an earlier version of this job");
    await expect(detail.getByRole("button", { name: "Show older runs" })).toHaveCount(0); // 3 < 20

    // Open a run's Technical details and the job's own: the long ids and stamps must wrap too.
    const summaries = detail.getByRole("button", { name: "Technical details" });
    await expect(summaries).toHaveCount(4);
    // Collapsed, each is a compact muted text toggle — not a bordered, padded card.
    expect(await collapsedToggles(detail)).toEqual([COMPACT, COMPACT, COMPACT, COMPACT]);
    await summaries.first().click();
    await summaries.last().click();
    await expect(summaries.first()).toHaveAttribute("aria-expanded", "true");
    await expect(summaries.nth(1)).toHaveAttribute("aria-expanded", "false"); // per run, not shared
    await expect(runs.getByRole("listitem").first()).toContainText("2020-11-20T08:00:00+00:00");
    await expect(runs.getByRole("listitem").first()).toContainText(RUN.task_id);

    // §4: nothing scrolls sideways — the page, the Work view, the detail pane, or any run row.
    const fits = () => page.evaluate(() => {
      const wide = (el: Element | null) => !!el && el.scrollWidth > el.clientWidth + 1;
      const view = document.querySelector(".work-view__detail");
      return {
        page: wide(document.documentElement),
        detail: wide(view),
        rows: Array.from(document.querySelectorAll(".job-run")).filter(wide).length,
        past: Array.from(view?.querySelectorAll("*") ?? []).filter((el) => {
          const edge = el.getBoundingClientRect();
          return edge.width > 0 && edge.right > window.innerWidth + 1;
        }).length,
      };
    });
    await expect.poll(fits).toEqual({ page: false, detail: false, rows: 0, past: 0 });

    const shot = info.outputPath(`job-runs-390-${theme}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    await info.attach(`Recent runs at 390 (${theme})`, { path: shot, contentType: "image/png" });
  });
}

test("a healthy job with no runs says when it first runs; the window detail lists runs beside the list", async ({ page }, info) => {
  let runs: unknown[] = [];
  await page.route("**/api/scheduler/jobs", (route) => route.request().method() === "GET"
    ? route.fulfill({ json: { backend: "local", jobs: [{ ...JOB, consecutive_failures: 0, last_error: null, last_ok: null, last_fire: null }] } })
    : route.fallback());
  await page.route("**/api/scheduler/jobs/*/runs?*", (route) => route.fulfill({ json: { job_id: JOB.id, runs } }));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/app/");
  await expect(page.getByTestId("header-menu")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await page.locator(".pl-cmdk-commands__input").fill("Open Work");
  await page.getByRole("option", { name: "Open Work", exact: true }).click();
  const work = page.getByRole("dialog", { name: /^Work/ }).locator(".work-view");
  const row = work.locator(".work-row", { hasText: JOB.prompt });
  await expect(row.locator(".pl-dot--warning")).toHaveCount(0);
  await row.click();
  const detail = work.locator(".work-view__detail");
  await expect(detail).toContainText("Hasn't run yet.");
  await expect(detail).toContainText("No runs yet. It first runs Jan 1 2099 09:00 Europe/Berlin.");
  await expect(detail.getByRole("list", { name: "Recent runs" })).toHaveCount(0);

  // Closing and reopening the detail reads the first page again — now with runs.
  runs = RUNS;
  await detail.getByRole("button", { name: "Close details" }).click();
  await work.locator(".work-row", { hasText: JOB.prompt }).click();
  await expect(detail.getByRole("list", { name: "Recent runs" }).getByRole("listitem")).toHaveCount(3);
  await expect.poll(() => detail.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true);

  // Desktop scale: every collapsed Technical details row is compact; the keyboard opens and
  // closes one run's disclosure with a visible focus ring, leaving the others closed.
  expect(await collapsedToggles(detail)).toEqual([COMPACT, COMPACT, COMPACT, COMPACT]);
  const toggle = detail.locator(".job-run").first().getByRole("button", { name: "Technical details" });
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(toggle).toBeFocused();
  expect(await toggle.evaluate((el) => getComputedStyle(el).outlineStyle)).toBe("solid");
  await expect(detail.locator(".job-run").first()).toContainText(RUN.message_id);
  await expect(detail.locator(".job-run").nth(1)).not.toContainText(RUN.message_id);
  await page.keyboard.press("Space");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  const shot = info.outputPath("job-runs-wide.png");
  await page.screenshot({ path: shot });
  await info.attach("Recent runs in the window", { path: shot, contentType: "image/png" });
});
