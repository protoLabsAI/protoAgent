import { expect, test } from "@playwright/test";

// Per-tool output renderers: each starter tool's known output string renders as
// a purpose-built component, never a raw blob. The mock picks the tool scenario
// from the prompt keyword (see e2e/fixtures.mjs).

async function run(page, prompt: string) {
  await page.goto("/app/", { waitUntil: "networkidle" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Control+Enter");
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
  await expect(fetch.locator(".tool-badge")).toHaveText("200");
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
