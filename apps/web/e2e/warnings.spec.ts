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

/** Serve `fields(agent)` as each agent's runtime status — the hub (`"host"`) at
 *  /api/runtime/status, a fleet member at /agents/<slug>/api/runtime/status (what slug routing
 *  requests). The real status body is snapshotted ONCE, before routing, and fulfilled
 *  synthetically: proxying via `route.fetch()` inside the handler holds an APIResponse bound to
 *  the page lifecycle, and a `page.reload()` that supersedes an in-flight status poll disposes it
 *  mid-read ("Response has been disposed"). `fields` is read at call time, so a spec can change
 *  what the next poll/reload sees; a field set to `undefined` is omitted from the payload. */
async function routeStatus(page: Page, fields: (agent: string) => Partial<GapFields>, omit: string[] = []) {
  const base = await (await page.request.get("/api/runtime/status")).json();
  for (const key of omit) delete base[key];
  await page.route("**/api/runtime/status", async (route) => {
    const member = /\/agents\/([^/]+)\/api\/runtime\/status/.exec(route.request().url());
    const agent = member ? decodeURIComponent(member[1]) : "host";
    await route.fulfill({ json: { ...base, ...fields(agent) } });
  });
}

/** One agent's stored dismissal signatures, read from the page's sessionStorage. */
const storedDismissals = (page: Page, agent = "host") =>
  page.evaluate((key) => JSON.parse(window.sessionStorage.getItem(key) || "[]") as string[], `protoagent.setupGapDismissals:${agent}`);

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

test("a gap the server CLEARS resets its dismissal — if it breaks again, it shows again", async ({ page }) => {
  // #3421's stated contract: a dismissal resets when the server clears the gap. A current server
  // always sends `setup_gaps` (`[]` when there are none), so an empty list is a REAL clear — the
  // operator fixed it. A later recurrence with the same text is a new problem, and must not stay
  // hidden for the rest of the session (in the desktop webview: until the app restarts).
  let current: Partial<GapFields> = gapStatus(["coder"]);
  await routeStatus(page, () => current);
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: /Dismiss/ }).click();
  await expect(banner).toHaveCount(0);

  current = NO_GAPS; // fixed: the server's list is present and empty
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible();
  await expect.poll(() => storedDismissals(page)).toEqual([]); // sync point: the reset has landed

  current = gapStatus(["coder"]); // …and it breaks again, same text
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".setup-gap-banner")).toBeVisible();
});

test("a dismissal survives a status whose gap list is UNKNOWN (no `setup_gaps` field)", async ({ page }) => {
  // The opposite case: a status that doesn't carry the list says nothing about what the server
  // cleared (a server that predates the field; or, on every reload, the frame before the status
  // arrives), so it must never reset a dismissal.
  let current: Partial<GapFields> = gapStatus(["coder"]);
  await routeStatus(page, () => current);
  await page.goto("/app/", { waitUntil: "load" });

  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: /Dismiss/ }).click();
  await expect(banner).toHaveCount(0);

  current = { warnings: [], setup_gaps: undefined };
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible(); // the status (sans list) has loaded

  current = gapStatus(["coder"]);
  await page.reload({ waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible();
  await expect(page.locator(".setup-gap-banner")).toHaveCount(0);
  await expect(page.locator(".shell-warning-banner")).toHaveCount(0);
});

// Runtime status is the FOCUSED agent's, and a fleet switch (/app/agent/<slug>/) is a full page
// load in the same tab — same sessionStorage. A dismissal is one agent's acknowledgement of one
// of ITS problems (#3438 review: with one shared key, a hub dismissal hid a member's identical
// gap, and visiting a member pruned the hub's dismissals).
test("control: a member's own gap renders on its slug route", async ({ page }) => {
  await routeStatus(page, (agent) => (agent === "ava" || agent === "host" ? gapStatus(["coder"]) : NO_GAPS));
  await page.goto("/app/agent/ava/", { waitUntil: "load" });
  await expect(page.locator(".setup-gap-banner")).toBeVisible();
});

test("dismissing a gap on the hub never hides a member's own identical gap", async ({ page }) => {
  // Both agents run the board plugin and both lack a coder delegate — two separate problems.
  await routeStatus(page, (agent) => (agent === "ava" || agent === "host" ? gapStatus(["coder"]) : NO_GAPS));
  await page.goto("/app/", { waitUntil: "load" });
  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: /Dismiss/ }).click();
  await expect(banner).toHaveCount(0);

  await page.goto("/app/agent/ava/", { waitUntil: "load" }); // focus the member, same tab
  await expect(page.locator(".setup-gap-banner")).toBeVisible();
});

test("visiting another agent never prunes this agent's dismissals", async ({ page }) => {
  await routeStatus(page, (agent) => (agent === "host" ? gapStatus(["coder"]) : agent === "ava" ? gapStatus(["repo"]) : NO_GAPS));
  await page.goto("/app/", { waitUntil: "load" });
  const banner = page.locator(".setup-gap-banner");
  await expect(banner).toBeVisible();
  await banner.getByRole("button", { name: /Dismiss/ }).click();
  await expect(banner).toHaveCount(0);

  await page.goto("/app/agent/ava/", { waitUntil: "load" });
  await expect(page.locator(".setup-gap-banner")).toContainText("can't reach its repository");

  // Back to the hub: its gap is unchanged and this is the same session, so it stays hidden.
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.locator(".pl-rail").first()).toBeVisible();
  await expect(page.locator(".setup-gap-banner")).toHaveCount(0);
  expect(await storedDismissals(page, "host")).toHaveLength(1);
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
