import { expect, test, type Page } from "@playwright/test";

// "Unattended spend today" at the top of Work (docs/design/work-view-additions.md §1, §4):
// the block sits above the four groups, nothing scrolls sideways at 390, and Resume answers
// the spend-cap decision with option `resume`, then moves focus to the block's heading.

const DECISION = "dec-spend-cap-e2e";

type Reading = {
  day: string; cost_usd: number | null; total_tokens: number | null; enabled: boolean; meter_available: boolean;
  daily_cost_hard_limit: number; daily_token_hard_limit: number; daily_cost_soft_limit?: number;
  daily_token_soft_limit?: number; approved_today: boolean; decision_id: string | null; paused: boolean;
};
const reading = (patch: Partial<Reading>): Reading => ({
  day: new Date().toISOString().slice(0, 10), cost_usd: 6.35, total_tokens: 541000, enabled: true,
  meter_available: true, daily_cost_hard_limit: 10, daily_token_hard_limit: 1000000, daily_cost_soft_limit: 5,
  daily_token_soft_limit: 600000, approved_today: false, decision_id: null, paused: false, ...patch,
});

/** Serve `state.reading` from the spend-cap route, and the spend-cap decision with its answer
 *  route. Returns the option ids the page answered with. */
async function serve(page: Page, state: { reading: Reading }) {
  const answers: string[] = [];
  const decision = {
    id: DECISION, session_id: "", agent: "protoAgent", question: "Unattended work reached today's spend cap. Resume it?",
    options: [{ id: "resume", label: "Resume unattended work for today" }], created_at: new Date().toISOString(),
    state: "pending", answer: null as string | null, answered_at: null as string | null, expires_at: null,
    default_option: null, thread_id: "", interrupt_id: "", context: "", default_action: null, withdraw_reason: null,
    answered_by: null, answered_surface: null, default_action_text: "", answer_label: null as string | null,
  };
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.route("**/api/telemetry/spend-cap", (route) => route.fulfill({ json: state.reading }));
  await page.route("**/api/decisions**", async (route) => {
    const url = new URL(route.request().url());
    const listed = state.reading.decision_id ? [decision] : [];
    if (url.pathname === `/api/decisions/${DECISION}/answer` && route.request().method() === "POST") {
      const option = (route.request().postDataJSON() as { option_id: string }).option_id;
      answers.push(option);
      Object.assign(decision, { state: "answered", answer: option, answered_at: new Date().toISOString(),
        answer_label: "Resume unattended work for today" });
      state.reading = { ...state.reading, approved_today: true, paused: false };
      return route.fulfill({ json: { decision, resumed: false } });
    }
    if (url.pathname === `/api/decisions/${DECISION}`) return route.fulfill({ json: decision });
    if (url.pathname === "/api/decisions") {
      const wanted = url.searchParams.get("state");
      const rows = wanted ? listed.filter((row) => row.state === wanted) : listed;
      return route.fulfill({ json: { decisions: rows, pending: listed.filter((row) => row.state === "pending").length } });
    }
    return route.fallback();
  });
  return answers;
}

/** Names every box that scrolls sideways: the page, the Work view and its list. */
const sidewaysScroll = (page: Page) => page.evaluate(() => {
  const view = Array.from(document.querySelectorAll<HTMLElement>(".work-view")).find((el) => el.getClientRects().length)!;
  return [document.documentElement, view, ...view.querySelectorAll(".work-view__list, .spend-today, .spend-today *")]
    .filter((el) => el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).display !== "inline")
    .map((el) => `${el.tagName.toLowerCase()}.${el.className} ${el.scrollWidth}>${el.clientWidth}`);
});

for (const mode of ["light", "dark"]) {
  test(`phone 390 ${mode}: a paused cap fits the sheet, and Resume moves focus to the heading`, async ({ page }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route("**/api/theme", (route) => route.fulfill({ json: { theme: { mode, overrides: {} } } }));
    // Both limits reached and long values: the widest copy the block can show.
    const state = { reading: reading({ paused: true, decision_id: DECISION, cost_usd: 12345.67, total_tokens: 123456789 }) };
    const answers = await serve(page, state);
    await page.goto("/app/");
    await page.getByRole("button", { name: "Open Work", exact: true }).click();
    const work = page.locator(".work-view:visible");
    await expect(work).toHaveAttribute("data-frame", "sheet");
    const block = work.getByRole("region", { name: "Unattended spend today" });
    await expect(block).toBeVisible();
    // Above the four groups: the block is the first thing in the list.
    await expect(work.locator(".work-view__content > :first-child")).toHaveClass(/spend-today/);
    await expect(block.locator(".pl-badge")).toHaveText("Paused");
    await expect(block).toContainText(/UTC day \d{4}-\d{2}-\d{2} · resets at 00:00 UTC, in \d+ (h|min)/);
    await expect(block).toContainText("It reached the $10.00 and 1,000,000-token daily limits.");
    await expect(block).toContainText("$12,345.67 of $10.00");
    await expect(block).toContainText("Notice at $5.00 · passed");
    const cost = block.getByRole("meter", { name: "Cost" });
    await expect(cost).toHaveAttribute("aria-valuenow", "10");
    await expect(cost).toHaveAttribute("aria-valuetext", "$12,345.67 of $10.00 daily limit, past the notice limit of $5.00");
    await expect(block.getByRole("meter", { name: "Tokens" })).toHaveAttribute("aria-valuemax", "1000000");
    expect(await sidewaysScroll(page)).toEqual([]);
    // The spend-cap decision is still an ordinary Needs you row; the block doesn't replace it.
    await expect(work.locator(".work-row").filter({ hasText: "reached today's spend cap" })).toBeVisible();
    const shot = info.outputPath(`spend-today-390-paused-${mode}.png`);
    await page.screenshot({ path: shot });
    await info.attach(`Paused at 390 ${mode}`, { path: shot, contentType: "image/png" });

    const resume = block.getByRole("button", { name: "Resume unattended work for today", exact: true });
    await expect(resume).toBeEnabled();
    await resume.click();
    await expect(block.locator(".pl-badge")).toHaveText("Resumed for today");
    expect(answers).toEqual(["resume"]);
    await expect(resume).toHaveCount(0);
    await expect(block.getByRole("heading", { level: 2, name: "Unattended spend today" })).toBeFocused();
    await expect(block).toContainText("You resumed unattended work for today.");
    // Its own Resume is not announced: focus is already on the heading.
    await expect(block.getByRole("status")).toHaveText("");
    expect(await sidewaysScroll(page)).toEqual([]);
  });
}

test("window 1440: running past a notice limit shows two meters side by side with the notice", async ({ page }, info) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await serve(page, { reading: reading({}) });
  await page.goto("/app/");
  await expect(page.getByTestId("header-menu")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+Shift+k");
  await page.locator(".pl-cmdk-commands__input").fill("Open Work");
  await page.getByRole("option", { name: "Open Work", exact: true }).click();
  const work = page.getByRole("dialog", { name: /^Work/ }).locator(".work-view");
  const block = work.getByRole("region", { name: "Unattended spend today" });
  await expect(block.locator(".pl-badge")).toHaveText("Running");
  await expect(block).toContainText("Past today's $5.00 notice limit. Unattended work keeps running until $10.00 or 1,000,000 tokens, then pauses.");
  await expect(block.getByRole("button")).toHaveCount(0);
  const [cost, tokens] = await Promise.all(["cost", "tokens"].map((kind) => block.locator(`[data-meter="${kind}"]`).boundingBox()));
  expect(Math.abs(cost!.y - tokens!.y)).toBeLessThan(2);
  expect(tokens!.x).toBeGreaterThan(cost!.x + cost!.width - 1);
  await expect(block.locator('[data-meter="cost"] .pl-progress__fill')).toHaveClass(/pl-progress__fill--warning/);
  await expect(block.locator('[data-meter="tokens"] .pl-progress__fill')).not.toHaveClass(/pl-progress__fill--warning/);
  expect(await sidewaysScroll(page)).toEqual([]);
  const shot = info.outputPath("spend-today-1440-past-notice.png");
  await page.screenshot({ path: shot });
  await info.attach("Past the notice limit at 1440", { path: shot, contentType: "image/png" });
});
