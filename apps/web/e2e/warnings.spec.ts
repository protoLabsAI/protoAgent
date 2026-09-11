import { expect, test, type Page } from "@playwright/test";

import { SETUP_GAPS_GOLDEN } from "./fixtures.mjs";

// Runtime-status `warnings` (#706 co-located instances etc.) render as a slim
// alert strip under the topbar; server-driven, so no warnings → no strip.

test("runtime warnings render as the shell alert strip", async ({ page }) => {
  await page.route("**/api/runtime/status", async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    json.warnings = ["Another running instance shares this agent's data (~/.protoagent): roxy (pid 12345, port 7871)."];
    await route.fulfill({ json });
  });
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".shell-warning-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("Another running instance");
  await expect(banner).toHaveAttribute("role", "alert");
});

test("no warnings → no alert strip (the default fixture)", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible(); // app booted
  await expect(page.locator(".shell-warning-banner")).toHaveCount(0);
});

// Plugin setup gaps (graph/plugins/setup_gaps.py) render as actionable, dismissible banners.
//
// Every payload below is the REAL server shape, not a hand-written guess: SETUP_GAPS_GOLDEN is
// what GET /api/runtime/status returns for two reported gaps — each gap as a structured record in
// `setup_gaps[]` (#3395) AND as its legacy `Label: message` line in `warnings[]` — and
// tests/test_console_handlers.py asserts that against the real handler. The first version of this
// spec mocked gap OBJECTS inside `warnings[]`, a shape the server never sends, so it stayed green
// while every real gap rendered as a plain alert with no Configure button and no dismiss (QA of
// v0.164.0). `boardy` is the enabled+loaded plugin in the e2e fixture, so its Configure dialog
// resolves cleanly.
type Gap = { plugin: string; key: string; label: string; message: string; actions?: unknown[] };
type GapFields = { warnings: string[]; setup_gaps: Gap[] };
const GOLDEN = SETUP_GAPS_GOLDEN.status as GapFields;

/** The runtime-status fields for the golden gaps with these keys, as the server sends them:
 *  `plain` operational warnings first, then each gap's legacy line (index-aligned with its
 *  record — the Python golden test pins that), plus the records in `setup_gaps[]`. */
function gapStatus(keys: string[], plain: string[] = []): GapFields {
  const picked = GOLDEN.setup_gaps.flatMap((gap, i) => (keys.includes(gap.key) ? [i] : []));
  return {
    warnings: [...plain, ...picked.map((i) => GOLDEN.warnings[i])],
    setup_gaps: picked.map((i) => GOLDEN.setup_gaps[i]),
  };
}

const CODER_LINE = GOLDEN.warnings[GOLDEN.setup_gaps.findIndex((g) => g.key === "coder")];
const NO_GAPS: GapFields = { warnings: [], setup_gaps: [] };

/** Serve `fields()` as this page's runtime status. The real status body is snapshotted ONCE,
 *  before routing, and fulfilled synthetically: proxying via `route.fetch()` inside the handler
 *  holds an APIResponse bound to the page lifecycle, and a `page.reload()` that supersedes an
 *  in-flight status poll disposes it mid-read ("Response has been disposed"). `fields` is read
 *  at call time, so a spec can change what the next poll/reload sees. */
async function routeStatus(page: Page, fields: () => Partial<GapFields>, omit: string[] = []) {
  const base = await (await page.request.get("/api/runtime/status")).json();
  for (const key of omit) delete base[key];
  await page.route("**/api/runtime/status", async (route) => {
    await route.fulfill({ json: { ...base, ...fields() } });
  });
}

test("a real setup gap renders ONE actionable banner, and Configure opens the plugin-config dialog", async ({ page }) => {
  await routeStatus(page, () => gapStatus(["coder"]));
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toHaveAttribute("role", "alert");
  await expect(banner).toContainText("No coder delegate is configured");
  await expect(banner.getByRole("button", { name: /Dismiss/ })).toBeVisible();
  // ONE banner for the gap: its legacy `warnings[]` line is the same gap, not a second alert.
  await expect(page.locator(".shell-warning-banner")).toHaveCount(1);

  await banner.getByRole("button", { name: "Configure Project Board" }).click();
  // Opens the reporting plugin's existing Configure dialog (titled by the gap label).
  await expect(page.getByRole("dialog", { name: "Project Board" })).toBeVisible();
});

test("a gap whose action the host sanitized away renders message + dismiss, and no CTA", async ({ page }) => {
  // The `repo` gap was reported with an `open_url` action; the host's closed allowlist dropped it
  // (setup_gaps._sanitize_action), so the record arrives with no `actions` at all.
  await routeStatus(page, () => gapStatus(["repo"]));
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("can't reach its repository");
  await expect(banner.getByRole("button", { name: /Configure|Open settings/ })).toHaveCount(0);
  await expect(banner.locator("a")).toHaveCount(0); // the plugin's URL never became a link
  await expect(banner.getByRole("button", { name: /Dismiss/ })).toBeVisible();
  await expect(page.locator(".pl-rail").first()).toBeVisible(); // strip + app still healthy
});

test("legacy operational warnings and a setup gap coexist in the strip", async ({ page }) => {
  await routeStatus(page, () => gapStatus(["coder"], ["Another running instance shares this agent's data."]));
  await page.goto("/app/", { waitUntil: "load" });

  // The plain string still renders as a warning alert; the gap renders as its own banner — and
  // only once (its legacy line isn't a third alert).
  await expect(page.locator(".setup-gap-banner")).toHaveCount(1);
  await expect(page.locator(".shell-warning-banner")).toHaveCount(2);
  await expect(page.getByText("Another running instance shares this agent's data.")).toBeVisible();
});

test("a setup gap dismisses for the session only, and returns on a new session", async ({ page }) => {
  await routeStatus(page, () => gapStatus(["coder"]));
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: /Dismiss/ }).click();
  await expect(banner).toHaveCount(0);
  // Dismissed means GONE — the gap's legacy line must not resurface as a plain alert either.
  await expect(page.getByText(CODER_LINE)).toHaveCount(0);
  await expect(page.locator(".shell-warning-banner")).toHaveCount(0);

  // Same session (reload keeps sessionStorage) → the unchanged gap stays hidden even though
  // the server still reports it every poll.
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible();
  await expect(page.locator(".setup-gap-banner")).toHaveCount(0);
  await expect(page.locator(".shell-warning-banner")).toHaveCount(0);

  // New session (sessionStorage cleared) → it returns; dismissal never touched the server.
  await page.evaluate(() => window.sessionStorage.clear());
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".setup-gap-banner")).toBeVisible();
});

test("a dismissed gap stays hidden across a transient empty runtime status in the same session", async ({ page }) => {
  // The status payload is mutable across reloads, so we can simulate a transient/null runtime
  // status (reload catching an unresolved poll) between two live polls.
  let current: GapFields = gapStatus(["coder"]);
  await routeStatus(page, () => current);
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: /Dismiss/ }).click();
  await expect(banner).toHaveCount(0);

  // Status blips to empty — the strip clears, but the session dismissal must NOT be pruned just
  // because the live gap set is momentarily empty.
  current = NO_GAPS;
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible();
  await expect(page.locator(".setup-gap-banner")).toHaveCount(0);

  // The unchanged gap returns on the next poll/reload → it stays hidden for the rest of the
  // session (the regression the #3421 review caught: it must NOT reappear).
  current = gapStatus(["coder"]);
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible();
  await expect(page.locator(".setup-gap-banner")).toHaveCount(0);
  await expect(page.locator(".shell-warning-banner")).toHaveCount(0);
});

test("a server without structured gaps (pre-#3395) still shows its gap line as a plain alert", async ({ page }) => {
  // No `setup_gaps` field at all, only the legacy line: nothing to make actionable, so the line
  // must keep rendering as the plain warning it always was — never silently dropped.
  await routeStatus(page, () => ({ warnings: [CODER_LINE] }), ["setup_gaps"]);
  await page.goto("/app/", { waitUntil: "load" });

  await expect(page.locator(".shell-warning-banner")).toHaveCount(1);
  await expect(page.locator(".shell-warning-banner")).toContainText("No coder delegate is configured");
  await expect(page.locator(".setup-gap-banner")).toHaveCount(0);
});
