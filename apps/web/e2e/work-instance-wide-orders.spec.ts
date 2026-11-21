import { expect, test, type Page } from "@playwright/test";

// Instance-wide standing orders in Work → Your instructions (docs/design/work-view-additions.md
// §3, §4): an order recorded in ONE chat lists and counts in every chat, sorted first; its
// detail withdraws it everywhere through POST /api/directives/{id}/withdraw?instance_wide=true;
// and focus follows the §4 rules (heading on open, Withdraw… after Keep it / Escape, then the
// next row, the previous row or the group heading after a withdraw).

const SESSION = "chat-instance-wide-e2e";
const ORIGIN = "chat-instance-wide-origin";
const OWN_QUOTE = "Cut the release once both PRs merge";

type Row = {
  id: string; session_id: string; quote: string; conditions: string; scope: string; status: string;
  created_at: string; closed_by: string | null; closed_at: string | null; close_reason: string | null;
  author: string; links: unknown[]; instance_wide: boolean;
};
const order = (id: string, quote: string, created_at: string): Row => ({
  id, session_id: ORIGIN, quote, conditions: "", scope: "", status: "in_force", created_at,
  closed_by: null, closed_at: null, close_reason: null, author: "operator", links: [], instance_wide: true,
});

/** Boot the console on SESSION with `orders` in the instance ledger (recorded in ORIGIN) and
 *  `own` in SESSION's own ledger. Returns the withdraw requests the page made. */
async function boot(page: Page, orders: Row[], own: Row[], onWithdraw?: (row: Row) => { status: number } | void) {
  const withdrawals: { id: string; instanceWide: string | null; reason: string | null }[] = [];
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(({ session, origin }) => {
    localStorage.setItem("protoagent.chat.sessions", JSON.stringify({
      version: 1, currentSessionId: session,
      sessions: [
        { id: session, title: "Release instructions", createdAt: 1, updatedAt: 2, messages: [] },
        { id: origin, title: "Morning planning", createdAt: 3, updatedAt: 3, messages: [] },
      ],
    }));
  }, { session: SESSION, origin: ORIGIN });
  await page.route("**/api/directives**", async (route) => {
    const url = new URL(route.request().url());
    const withdraw = /^\/api\/directives\/([^/]+)\/withdraw$/.exec(url.pathname);
    if (withdraw && route.request().method() === "POST") {
      const id = decodeURIComponent(withdraw[1]);
      withdrawals.push({ id, instanceWide: url.searchParams.get("instance_wide"), reason: url.searchParams.get("reason") });
      const row = orders.find((candidate) => candidate.id === id);
      if (!row) return route.fulfill({ status: 404, json: { detail: `no instance-wide directive '${id}'` } });
      const forced = onWithdraw?.(row);
      if (forced) return route.fulfill({ status: forced.status, json: { detail: "refused" } });
      Object.assign(row, { status: "revoked", closed_by: "operator", closed_at: new Date().toISOString(),
        close_reason: url.searchParams.get("reason") || "revoked by the operator" });
      return route.fulfill({ json: { directive: row } });
    }
    if (url.pathname === "/api/directives" && route.request().method() === "GET") {
      // The console must ask for EVERY row: a status filter would drop closed ones.
      expect(url.searchParams.has("status")).toBe(false);
      return route.fulfill({ json: { directives: orders, in_force: orders.filter((row) => row.status === "in_force").length } });
    }
    return route.fallback();
  });
  await page.route(`**/api/chat/sessions/${SESSION}/directives**`, (route) =>
    route.fulfill({ json: { directives: own, in_force: own.length } }));
  // A healthy session index: Work discovers every chat's ledger through it, and a failed
  // read would leave Your instructions stale/reloading under the assertions below.
  await page.route("**/api/chat/sessions?*", (route) => {
    if (new URL(route.request().url()).pathname !== "/api/chat/sessions") return route.fallback();
    return route.fulfill({ json: { sessions: [SESSION, ORIGIN].map((session_id) =>
      ({ session_id, last_updated: "2026-11-19T08:05:00Z", turn_count: 0 })) } });
  });
  await page.route("**/api/inbox**", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/events**", (route) => route.fulfill({ contentType: "text/event-stream", body: ": idle\n\n" }));
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  return withdrawals;
}

/** Open Work on Your instructions: on the phone through the rail chip's "this chat" scope;
 *  on desktop — which hides the rail's work chips — through /directives. */
async function openInstructions(page: Page) {
  const rail = page.locator('.work-view[data-frame="rail"]');
  if ((page.viewportSize()?.width ?? 1280) < 768) {
    await page.getByTestId("chat-status-chip-instructions").click();
    await page.locator(".chat-status-details").getByRole("button", { name: "Open in Work", exact: true }).click();
  } else {
    await expect(page.getByTestId("chat-status-chip-instructions")).toHaveCount(0);
    const composer = page.getByPlaceholder(/Message protoAgent/i);
    await composer.fill("/directives ");
    await composer.press("Enter");
  }
  await expect(rail.locator(".work-section--directives")).toBeVisible();
  return rail;
}

/** Names every box that scrolls sideways: the page, the Work view, its list and its detail. */
const sidewaysScroll = (page: Page) => page.evaluate(() => {
  const view = document.querySelector('.work-view[data-frame="rail"]')!;
  return [document.documentElement, view, ...view.querySelectorAll(".work-view__list, .work-view__detail")]
    .filter((el) => el.scrollWidth > el.clientWidth)
    .map((el) => `${el.tagName.toLowerCase()}.${el.className} ${el.scrollWidth}>${el.clientWidth}`);
});

for (const width of [1440, 390]) {
  for (const mode of ["light", "dark"]) {
    test(`an order recorded in another chat lists, counts and withdraws everywhere at ${width} ${mode}`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 });
      await page.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode, overrides: {} } } }));
      const orders = [
        order("dir-i1", "Always post a summary to the release channel before you stop for the day", "2026-11-18T09:40:00+00:00"),
        order("dir-i2", "Never push to main without a green gate", "2026-11-19T08:05:00+00:00"),
      ];
      const own: Row[] = [{ ...order("dir-own", OWN_QUOTE, "2026-11-07T17:14:02+00:00"), session_id: SESSION, instance_wide: false }];
      const withdrawals = await boot(page, orders, own);
      await expect(page.locator("html")).toHaveAttribute("data-theme", mode);

      // Both orders were recorded in ANOTHER chat, yet they count in this chat's chip…
      const chip = page.getByTestId("chat-status-chip-instructions");
      if (width === 390) await expect(chip).toHaveText("3", { useInnerText: true });
      const rail = await openInstructions(page);
      // …and in Work's own count, from the same selector.
      await expect(rail.locator("h2.work-heading")).toHaveAttribute("aria-label", "Your instructions, 3");

      // Sorted before the chat's own, oldest first; two badges; the reach leads the meta.
      const rows = rail.locator(".work-view__list .work-row");
      await expect(rows).toHaveCount(3);
      await expect(rows.nth(0)).toContainText("Always post a summary");
      await expect(rows.nth(1)).toContainText("Never push to main");
      await expect(rows.nth(2)).toContainText(OWN_QUOTE);
      await expect(rows.nth(0).locator(".pl-badge")).toHaveText(["Instance-wide", "Instruction"]);
      await expect(rows.nth(2).locator(".pl-badge")).toHaveText(["Instruction"]);
      await expect(rows.nth(0).locator(".work-row__meta")).toHaveText(/^Applies to every chat and unattended turn · You said “Always post a summary/);
      expect(await sidewaysScroll(page)).toEqual([]);
      await page.screenshot({ path: info.outputPath("rows.png"), fullPage: true, animations: "disabled" });

      // The detail: focus stays on its heading, never on Withdraw… (the wasConfirming guard).
      await rows.nth(0).click();
      const detail = rail.locator(".work-view__detail");
      await expect(detail.locator("h2.work-detail__title")).toBeFocused();
      await expect(detail.locator(".work-detail__header .pl-badge")).toHaveText(["Instance-wide", "Instruction"]);
      const field = (label: string) => detail.locator(".work-detail__field").filter({ has: page.locator("dt", { hasText: new RegExp(`^${label}$`) }) }).locator("dd");
      await expect(field("Applies to")).toHaveText("Every chat and every unattended turn on this instance: scheduled jobs, watches, the inbox and webhooks.");
      await expect(field("Your words")).toHaveText(/^You said “Always post a summary.+” in “Morning planning” · .+\d{2}:\d{2} \(your time\)$/);
      await expect(field("Lasts")).toHaveText("Until you withdraw it");
      await expect(detail.getByRole("button", { name: "Edit", exact: true })).toBeVisible();
      await detail.getByText("Technical details", { exact: true }).click();
      const tech = detail.locator(".work-detail__tech-row");
      await expect(tech.locator("dt")).toHaveText(["id", "instance_wide", "recorded in", "status", "created_at"]);
      await expect(tech.locator("dd")).toHaveText(["dir-i1", "true", ORIGIN, "in_force", "2026-11-18T09:40:00+00:00"]);
      expect(await sidewaysScroll(page)).toEqual([]);
      await page.screenshot({ path: info.outputPath("detail.png"), fullPage: true, animations: "disabled" });

      // The confirm: the prompt takes focus; Keep it and Escape both hand it back to Withdraw….
      const withdrawButton = detail.getByRole("button", { name: "Withdraw…" });
      await withdrawButton.click();
      const confirm = detail.getByRole("group", { name: "Withdraw this standing order everywhere?" });
      await expect(confirm.locator(".work-detail__confirm-prompt")).toBeFocused();
      await expect(confirm).toContainText("Every chat and every unattended turn stops following it from its next turn. Anything it already scheduled keeps running until you cancel it.");
      await confirm.getByRole("button", { name: "Keep it" }).click();
      await expect(withdrawButton).toBeFocused();
      await withdrawButton.click();
      await expect(confirm.locator(".work-detail__confirm-prompt")).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(confirm).toHaveCount(0);
      await expect(withdrawButton).toBeFocused();
      expect(withdrawals).toEqual([]);

      // Withdraw everywhere, with a reason.
      await withdrawButton.click();
      await confirm.getByLabel("Reason (optional)").fill("moving the summary to the weekly digest");
      expect(await sidewaysScroll(page)).toEqual([]);
      await page.screenshot({ path: info.outputPath("confirm.png"), fullPage: true, animations: "disabled" });
      await confirm.getByRole("button", { name: "Withdraw everywhere" }).click();
      await expect(page.locator(".pl-toast__msg").filter({ hasText: "Withdrawn." })).toBeVisible();
      expect(withdrawals).toEqual([{ id: "dir-i1", instanceWide: "true", reason: "moving the summary to the weekly digest" }]);

      // The detail closes, the row and the count drop, and focus lands on the NEXT row.
      await expect(detail).toHaveCount(0);
      await expect(rows).toHaveCount(2);
      await expect(rail.locator("h2.work-heading")).toHaveAttribute("aria-label", "Your instructions, 2");
      await expect(rows.nth(0)).toContainText("Never push to main");
      await expect(rows.nth(0)).toBeFocused();
      expect(await sidewaysScroll(page)).toEqual([]);
      await page.screenshot({ path: info.outputPath("withdrawn.png"), fullPage: true, animations: "disabled" });
      // The rail chip drops with it (on the phone Work covers the chat, so step back first).
      if (width === 390) await page.getByRole("button", { name: "Back", exact: true }).click();
      if (width === 390) await expect(chip).toHaveText("2", { useInnerText: true });
      else await expect(chip).toHaveCount(0);
    });
  }
}

test("after a withdraw focus goes to the previous row, then to the group heading", async ({ page }) => {
  const orders = [
    order("dir-i1", "Always post a summary before you stop", "2026-11-18T09:40:00+00:00"),
    order("dir-i2", "Never push to main without a green gate", "2026-11-19T08:05:00+00:00"),
  ];
  await boot(page, orders, []);
  const rail = await openInstructions(page);
  const rows = rail.locator(".work-view__list .work-row");
  const detail = rail.locator(".work-view__detail");
  const withdraw = async () => {
    await detail.getByRole("button", { name: "Withdraw…" }).click();
    await detail.getByRole("button", { name: "Withdraw everywhere" }).click();
    await expect(detail).toHaveCount(0);
  };

  // The LAST row leaves: nothing follows it, so the previous row takes focus.
  await rows.nth(1).click();
  await withdraw();
  await expect(rows).toHaveCount(1);
  await expect(rows.nth(0)).toBeFocused();

  // The group empties: its heading takes focus (tabindex -1) — never body.
  await rows.nth(0).click();
  await withdraw();
  await expect(rows).toHaveCount(0);
  const heading = rail.locator("h2.work-heading");
  await expect(heading).toHaveAttribute("aria-label", "Your instructions, 0");
  await expect(heading).toHaveAttribute("tabindex", "-1");
  await expect(heading).toBeFocused();
});

test("a 409 keeps the detail read-only with the closing sentence; a failed request offers Try again", async ({ page }) => {
  const orders = [order("dir-i1", "Always post a summary before you stop", "2026-11-18T09:40:00+00:00")];
  let mode: "fail" | "conflict" = "fail";
  await boot(page, orders, [], (row) => {
    if (mode === "fail") return { status: 503 };
    // Closed from another device between the list load and this request.
    Object.assign(row, { status: "revoked", closed_by: "operator", closed_at: new Date().toISOString(), close_reason: "revoked by the operator" });
    return { status: 409 };
  });
  const rail = await openInstructions(page);
  await rail.locator(".work-view__list .work-row").first().click();
  const detail = rail.locator(".work-view__detail");
  await detail.getByRole("button", { name: "Withdraw…" }).click();
  await detail.getByRole("button", { name: "Withdraw everywhere" }).click();
  await expect(detail).toContainText("Couldn't withdraw it. It's still in force everywhere.");
  await expect(rail.locator(".work-view__list .work-row")).toHaveCount(1);

  mode = "conflict";
  await detail.getByRole("button", { name: "Try again" }).click();
  await expect(detail).toContainText(/This was already closed: Withdrawn by you · today at .+ \(your time\)\. Nothing changed\./);
  await expect(detail.locator(".work-instruction-detail")).toBeFocused();
  await expect(detail.getByRole("button", { name: "Withdraw…" })).toHaveCount(0);
  await expect(rail.locator(".work-view__list .work-row")).toHaveCount(0);
});
