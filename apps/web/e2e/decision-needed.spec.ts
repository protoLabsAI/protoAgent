import { expect, test, type Page } from "@playwright/test";
import type { DecisionRow } from "../src/lib/api/decisions";

const decision = (id = "decision-one", agent = "protoEngineer"): DecisionRow => ({
  id, agent, session_id: "chat-decisions", question: "Ship the release after the checks pass?",
  options: [{ id: "ship", label: "Ship the release" }, { id: "hold", label: "Keep holding" }],
  created_at: "2026-11-09T14:00:00Z", state: "pending", answer: null, answered_at: null,
  expires_at: null, default_option: null, thread_id: "thread-one", interrupt_id: "interrupt-one",
  context: "", default_action: null, withdraw_reason: null, answered_by: null,
  answered_surface: null, default_action_text: "kept holding the release", answer_label: null,
});

async function setup(page: Page, rows: DecisionRow[], result: "sent" | "answered" | "expired" | "withdrawn" | "cancelled" | "error" = "sent") {
  let pending = [...rows];
  const answers: unknown[] = [];
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.route("**/api/events**", (route) => route.fulfill({ contentType: "text/event-stream", body: ": idle\n\n" }));
  await page.route("**/api/decisions?**", (route) => route.fulfill({ json: { decisions: pending, pending: pending.length } }));
  await page.route("**/api/decisions/*/answer", (route) => {
    answers.push(route.request().postDataJSON());
    if (result === "error") return route.fulfill({ status: 503, json: { detail: "Unavailable" } });
    const row = rows.find((d) => route.request().url().includes(`/${d.id}/`))!;
    const settled = { ...row, state: result === "sent" ? "answered" : result,
      answer: result === "expired" || result === "withdrawn" || result === "cancelled" ? null : "ship",
      answer_label: result === "expired" || result === "withdrawn" || result === "cancelled" ? null : "Ship the release",
      expires_at: result === "expired" ? "2026-11-09T14:04:00Z" : null,
      default_action_text: "paused deployment until tomorrow",
      withdraw_reason: "The release was superseded.",
      answered_at: "2026-11-09T14:05:00Z", answered_surface: "chat" };
    pending = pending.filter((d) => d.id !== row.id);
    return route.fulfill({ status: result === "sent" ? 200 : 409,
      json: result === "sent" ? { decision: settled, resumed: true } : { detail: settled } });
  });
  return answers;
}

async function screenshot(page: Page, name: string) {
  // Opt-in tracked review evidence; ordinary CI runs only assertions.
  if (process.env.DECISION_SCREENSHOTS) {
    await page.screenshot({ path: `e2e/screenshots/decision-needed/${name}.png` });
  }
}

for (const mode of ["light", "dark"] as const) {
  test.describe(mode, () => {
    test.use({ timezoneId: "UTC", locale: "en-US" });
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode, overrides: {} } } }));
    });

    test("quiet pill and empty Work Decisions section", async ({ page }) => {
      await setup(page, []);
      await page.goto("/app/");
      const pill = page.getByRole("button", { name: "Decisions, nothing waiting", exact: true });
      await expect(pill).toBeVisible();
      await expect(pill).not.toContainText("0");
      await screenshot(page, `${mode}-quiet`);
      await pill.click();
      const work = page.locator('.work-view[data-frame="modal"]');
      await expect(work.getByRole("button", { name: /Work sections/ })).toContainText("Decisions");
      await expect(work).toContainText("No decisions to show");
      await screenshot(page, `${mode}-empty`);
    });

    test("three agents and hover preview", async ({ page }) => {
      await setup(page, [decision(), decision("two", "protoDesigner"), decision("three", "protoReviewer")]);
      await page.goto("/app/");
      const pill = page.getByRole("button", { name: "Decisions, 3 waiting on you" });
      await pill.hover();
      await expect(page.getByRole("tooltip")).toContainText("and 2 more");
      await pill.click();
      const work = page.locator('.work-view[data-frame="modal"]');
      await expect(work.getByRole("button", { name: /Work sections/ })).toContainText("Decisions");
      await expect(work.locator(".work-row")).toHaveCount(3);
      await work.locator(".work-row").first().click();
      await expect(work.getByRole("article")).toHaveCount(1);
      await screenshot(page, `${mode}-three`);
    });

    for (const result of ["sent", "answered", "expired", "withdrawn", "cancelled", "error"] as const) {
      test(`answer: ${result}`, async ({ page }) => {
        await page.clock.setFixedTime(new Date("2026-11-10T12:00:00Z"));
        const answers = await setup(page, [decision()], result);
        await page.goto("/app/");
        const pill = page.getByRole("button", { name: "Decisions, 1 waiting on you" });
        await pill.hover();
        await expect(page.getByRole("tooltip")).toContainText("Ship the release after the checks pass?");
        if (result === "sent") await screenshot(page, `${mode}-pending-hover`);
        await pill.click();
        const dialog = page.locator('.work-view[data-frame="modal"]');
        await expect(dialog.getByRole("button", { name: /Work sections/ })).toContainText("Decisions");
        await dialog.locator(".work-row").click();
        await dialog.getByRole("button", { name: "Ship the release", exact: true }).click();
        const copy = { sent: 'Sent: "Ship the release".', answered: 'Already answered:',
          expired: "went ahead", withdrawn: "doesn't need this any more", cancelled: "doesn't need this any more", error: "Couldn't send your answer." };
        await expect(dialog).toContainText(copy[result]);
        if (result === "expired" || result === "withdrawn" || result === "cancelled") {
          const card = dialog.getByRole("article");
          await expect(card).not.toContainText("Already answered:");
          await expect(card.getByRole("group", { name: "Answer", exact: true })).toHaveCount(0);
          await expect(card.locator('[aria-live="polite"][tabindex="-1"]')).toBeFocused();
          if (result === "expired") {
            await expect(card).toContainText("No answer by yesterday at 2:04 PM, so protoEngineer went ahead and paused deployment until tomorrow.");
          } else {
            await expect(card).toContainText("protoEngineer doesn't need this any more. The release was superseded.");
          }
        }
        expect(answers).toEqual([{ option_id: "ship", surface: "console" }]);
        if (result === "error") await expect(dialog.getByRole("button", { name: "Ship the release", exact: true })).toBeEnabled();
        else await expect(page.getByRole("button", { name: "Decisions, nothing waiting", exact: true })).toBeVisible();
        await screenshot(page, `${mode}-${result}`);
      });
    }
  });
}

for (const width of [1440, 768, 390]) {
  test(`notification opens Decisions at ${width}; sheets are exclusive`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(() => {
      const notices: { title: string; onclick?: () => void }[] = [];
      Object.defineProperty(window, "decisionNotices", { value: notices });
      Object.defineProperty(document, "hasFocus", { value: () => false });
      Object.defineProperty(window, "Notification", { value: class {
        static permission = "granted";
        title: string;
        constructor(title: string) { this.title = title; notices.push(this); }
      } });
    });
    await setup(page, [decision()]);
    await page.goto("/app/");
    const announcement = page.getByTestId("decision-announcement");
    await expect(announcement).toHaveText("protoEngineer needs a decision.");
    await expect(announcement).toHaveAttribute("role", "status");
    await expect(announcement).toHaveAttribute("aria-live", "polite");
    if (width < 768) {
      await expect(page.getByTestId("decisions-widget")).toHaveCount(0);
      await expect(page.locator(".mshell-head").getByRole("button", { name: /Decisions/ })).toHaveCount(0);
      await page.locator(".mshell-title").click();
      await expect(page.getByRole("dialog")).toBeVisible();
    }
    await page.evaluate(() => {
      const notices = (window as unknown as { decisionNotices: { title: string; onclick?: () => void }[] }).decisionNotices;
      const matching = notices.filter((n) => n.title === "protoEngineer needs a decision");
      if (matching.length !== 1) throw new Error(`Expected one notification, got ${matching.length}`);
      matching[0].onclick?.();
    });
    if (width < 768) {
      const work = page.getByRole("dialog", { name: "Work", exact: true });
      await expect(work).toBeVisible();
      await expect(work).toHaveClass(/pl-drawer--full/);
      await expect(work.getByRole("button", { name: /Work sections/ })).toContainText("Decisions");
      await expect(work.locator(".work-row")).toContainText("Ship the release after the checks pass?");
      await expect(page.getByRole("dialog", { name: "Chats", exact: true })).toHaveCount(0);
    } else {
      await expect(page.getByRole("dialog", { name: "Decisions waiting on you" })).toBeVisible();
      await expect(page.getByRole("button", { name: "Open Work", exact: true })).toHaveCount(0);
    }
    await expect(page.getByRole("dialog")).toHaveCount(1);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });
}

test("refreshing the same pending decision does not announce it again", async ({ page }) => {
  await setup(page, [decision()]);
  let listReads = 0;
  await page.route("**/api/decisions?**", (route) => {
    listReads++;
    return route.fulfill({ json: { decisions: [decision()], pending: 1 } });
  });
  let release = () => {};
  const changed = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/events**", async (route) => {
    await changed;
    return route.fulfill({ contentType: "text/event-stream", body: `data: ${JSON.stringify({
      topic: "decision.changed", data: { id: "decision-one", state: "pending" },
    })}\n\n` });
  });
  await page.goto("/app/");
  const announcement = page.getByTestId("decision-announcement");
  await expect(announcement).toHaveText("protoEngineer needs a decision.");
  await announcement.evaluate((node) => {
    const observer = new MutationObserver(() => node.setAttribute("data-reannounced", "true"));
    observer.observe(node, { childList: true, subtree: true, characterData: true });
  });
  const previousReads = listReads;
  const refreshed = page.waitForResponse((response) => response.url().includes("/api/decisions?"));
  release();
  await refreshed;
  await expect.poll(() => listReads).toBeGreaterThan(previousReads);
  // Give React its paint after the refreshed store snapshot, then check the live region.
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(announcement).not.toHaveAttribute("data-reannounced", "true");
});
