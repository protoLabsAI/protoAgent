import { expect, test } from "@playwright/test";

// Per-tool output renderers: each starter tool's known output string renders as
// a purpose-built component, never a raw blob. The mock picks the tool scenario
// from the prompt keyword (see e2e/fixtures.mjs).

async function run(page, prompt: string) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
  const card = page.locator(".tool-card").first();
  await expect(card).toBeVisible();
  await expect(card.locator(".tool-card-status.done")).toBeVisible();
  await card.locator(".tool-card-head").click();
  await expect(card.locator(".tool-card-body")).toBeVisible();
  return card.locator(".tool-card-body");
}

test("calculator renders expression = result", async ({ page }) => {
  const body = await run(page, "CALC compute it");
  const calc = body.locator(".tool-calc");
  await expect(calc).toBeVisible();
  await expect(calc.locator("code")).toHaveText("19 * 23");
  await expect(calc.locator("strong")).toHaveText("437");
  await expect(body.locator("pre")).toHaveCount(0);
  // Header shows the tool-specific icon (not the generic wrench).
  await expect(page.locator(".tool-card-head .tool-card-icon.lucide-calculator")).toBeVisible();
});

test("each tool gets its own header icon", async ({ page }) => {
  for (const [prompt, icon] of [
    ["TIME now", "lucide-clock"],
    ["FETCH it", "lucide-globe"],
    ["search for things", "lucide-search"],
  ] as const) {
    await run(page, prompt);
    await expect(page.locator(`.tool-card-head .tool-card-icon.${icon}`).first()).toBeVisible();
  }
});

test("current_time renders the timestamp and human line", async ({ page }) => {
  const body = await run(page, "TIME in tokyo");
  const time = body.locator(".tool-time");
  await expect(time).toBeVisible();
  await expect(time.locator(".tool-mono")).toContainText("Asia/Tokyo");
  await expect(time.locator(".tool-time-human")).toContainText("Thursday, May 29 2026");
});

test("fetch_url renders a status badge, link, and body", async ({ page }) => {
  const body = await run(page, "FETCH the page");
  const fetch = body.locator(".tool-fetch");
  await expect(fetch.getByText("200", { exact: true })).toBeVisible(); // DS Badge (#832)
  await expect(fetch.locator("a.tool-link")).toHaveAttribute("href", "https://example.com");
  await expect(fetch.locator(".tool-fetch-body")).toContainText("Example Domain");
});

test("a tool error renders as an error block, not a result", async ({ page }) => {
  const body = await run(page, "TOOLERR force a failure");
  await expect(body.locator(".tool-error")).toBeVisible();
  await expect(body.locator(".tool-error")).toContainText("rate limited");
  // The "Error:" prefix is stripped; no result cards.
  await expect(body.locator(".tool-result")).toHaveCount(0);
});

test("scalar input values render inline, objects as key/value fields", async ({ page }) => {
  const body = await run(page, "FETCH the page");
  // fetch_url input { url: "https://example.com" } → one row, value linkified.
  await expect(body.locator(".tool-kv-row")).toHaveCount(1);
  await expect(body.locator(".tool-kv-key", { hasText: "url" })).toBeVisible();
  await expect(body.locator(".tool-kv-val a.tool-link")).toHaveAttribute("href", "https://example.com");
});

test("a tool whose end frame never arrives still finishes when the turn completes", async ({ page }) => {
  // Reproduces the workflow "spun on active" bug: a tool_start with no tool_end
  // (end raced the terminal done). The card must flip running→done, not spin.
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("NOEND run the workflow");
  await composer.press("Enter");

  const card = page.locator(".tool-card").first();
  await expect(card).toBeVisible();
  // The assistant's final text arrives (turn completed).
  await expect(page.getByText("Workflow finished.")).toBeVisible();
  // The card resolved to done — no lingering spinner.
  await expect(card).toHaveClass(/tool-card-done/);
  await expect(card.locator(".tool-card-status.running")).toHaveCount(0);
});
