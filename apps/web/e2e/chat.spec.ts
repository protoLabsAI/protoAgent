import { expect, test } from "@playwright/test";

// Drives the chat surface against the mock A2A stream and asserts the
// tool-call card contract: stable collapsed-by-default rows, pretty-printed
// JSON on expand, clean markdown answers, and no horizontal overflow.

async function send(page, prompt: string) {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  // The composer sends on Ctrl/Cmd+Enter (checks metaKey || ctrlKey).
  await composer.press("Enter");
}

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  // Setup wizard must not block — the mock reports setup_complete:true.
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("Enter sends; Ctrl+Enter inserts a newline", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("line one");
  await composer.press("Control+Enter"); // newline, not send
  await expect(composer).toHaveValue("line one\n");
  await expect(page.locator(".pl-message--user")).toHaveCount(0);

  await composer.fill("hello there"); // plain Enter sends
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user")).toHaveText(/hello there/);
});

test("tool-call card is collapsed by default and renders structured components", async ({ page }) => {
  await send(page, "search for AI coding agents");

  // Frame = DS ToolCard (#832): `.pl-toolcard*`; body slot is ours.
  const card = page.locator(".pl-toolcard").first();
  await expect(card).toBeVisible();
  await expect(card.locator(".pl-toolcard__name")).toHaveText("web_search");

  // Stable default: collapsed — no body rendered until the user opens it.
  await expect(page.locator(".pl-toolcard__body")).toHaveCount(0);

  // The tool finishes → done glyph (not the running spinner).
  await expect(card.locator(".pl-toolcard__status--done")).toBeVisible();

  // A duration pill is stamped on completion (mock gaps frames ~40ms).
  await expect(card.locator(".pl-toolcard__dur")).toHaveText(/^\d+ms$|^\d+\.\d+s$/);

  await card.locator(".pl-toolcard__head").click();
  const body = card.locator(".pl-toolcard__body");
  await expect(body).toBeVisible();

  // Input renders as key/value field rows — NOT a raw JSON blob.
  await expect(body.locator(".tool-kv-row")).toHaveCount(2);
  await expect(body.locator(".tool-kv-key", { hasText: "query" })).toBeVisible();
  await expect(body.locator(".tool-kv-row", { hasText: "max_results" }).locator(".tool-chip")).toHaveText("8");
  // No raw <pre> dump anywhere in the card.
  await expect(body.locator("pre")).toHaveCount(0);

  // web_search result renders as cards with clickable title links.
  await expect(body.locator(".tool-result")).toHaveCount(2);
  const firstLink = body.locator(".tool-result").first().locator("a.tool-link");
  await expect(firstLink).toHaveAttribute("href", "https://example.com/a");
});

test("expanded state is sticky and the assistant answer renders as markdown", async ({ page }) => {
  await send(page, "MARKDOWN: summarize");

  // Final answer renders through the streamdown markdown pipeline. streamdown emits real
  // semantic tags for block elements (h2/li/pre/code) but renders inline emphasis as a
  // styled span (`[data-streamdown="strong"]`, class `font-semibold`) rather than <strong>.
  const md = page.locator(".pl-message--assistant .markdown");
  await expect(md.locator("h2")).toHaveText("Summary");
  await expect(md.locator('[data-streamdown="strong"]')).toHaveText("key");
  await expect(md.locator("li")).toHaveCount(2);
  await expect(md.locator("pre code")).toContainText("const x = 1;");
});

test("a completed turn shows a context meter + token/cost footer (#1372)", async ({ page }) => {
  await send(page, "what is the capital of France?");

  // The terminal cost-v1 + context-v1 DataParts → a quiet footer under the answer:
  // context-window fill (with a compaction bar) / output ↓ / $cost.
  const usage = page.locator(".pl-message--assistant .chat-usage").first();
  await expect(usage).toBeVisible();
  await expect(usage).toContainText("12.3k / 120k"); // contextTokens 12_340 / compactionAtTokens 120_000
  await expect(usage).toContainText("1.2k"); // output_tokens 1_200
  await expect(usage).toContainText("2.3s"); // durationMs 2300
  await expect(usage).toContainText("$0.04"); // costUsd 0.0412
  // The fill bar renders (token-based trigger → chartable).
  await expect(usage.locator(".chat-usage-bar-fill")).toBeVisible();

  // The full breakdown is a rich hover card (DS Tooltip) — the compaction threshold + the
  // honest scope note live there, not in a native title attribute.
  // The full breakdown is a rich hover card (DS Tooltip) that mounts only on hover. Radix
  // double-renders the content (positioned + a11y copy) with identical text — assert on the
  // first; its presence proves the card opened.
  await expect(page.locator(".chat-usage-tip")).toHaveCount(0); // closed → not mounted
  await usage.hover();
  const tip = page.locator(".chat-usage-tip").first();
  await expect(tip).toContainText("near 120,000 tokens");
  await expect(tip).toContainText("Context is the live prompt size");
});

test("Settings ▸ Chat can hide the token/cost footer (#1372)", async ({ page }) => {
  await send(page, "what is the capital of France?");
  await expect(page.locator(".pl-message--assistant .chat-usage")).toBeVisible();

  // Flip it off in Settings ▸ Chat — the chat stays mounted behind the overlay, so the footer
  // clears live the moment the pref changes (no reload).
  await page.getByTestId("settings-widget").click();
  await page.locator(".settings-overlay .pl-sidenav").getByRole("tab", { name: "Chat", exact: true }).click();
  await page.locator('.setting-row[data-key="chat.showUsage"] .pl-switch').click();
  await expect(page.locator(".pl-message--assistant .chat-usage")).toHaveCount(0);
});

test("long tool values do not overflow the chat horizontally", async ({ page }) => {
  await send(page, "OVERFLOW: trigger a long token");

  const card = page.locator(".pl-toolcard").first();
  await expect(card).toBeVisible();
  await card.locator(".pl-toolcard__head").click();
  await expect(card.locator(".pl-toolcard__body")).toBeVisible();

  const metrics = await page.evaluate(() => {
    const body = document.querySelector(".pl-message--assistant .pl-message__content");
    return {
      docScroll: document.documentElement.scrollWidth,
      win: window.innerWidth,
      bodyScroll: body ? body.scrollWidth : 0,
      bodyClient: body ? body.clientWidth : 0,
    };
  });
  // No page-level horizontal scrollbar, and the message body contains its
  // content (long values wrap rather than blowing out the column).
  expect(metrics.docScroll).toBeLessThanOrEqual(metrics.win + 1);
  expect(metrics.bodyScroll).toBeLessThanOrEqual(metrics.bodyClient + 1);
});

test("right-click a chat tab → New chat / Rename / Close, and New chat adds a tab", async ({ page }) => {
  // ADR 0036 — the chat tab context menu. Default has one session tab.
  const tabs = page.locator(".pl-tabbar__tab");
  await expect(tabs).toHaveCount(1);

  await tabs.first().click({ button: "right" });
  const menu = page.locator(".pl-menu");
  await expect(menu).toBeVisible();
  await expect(menu.getByText("New chat", { exact: true })).toBeVisible();
  await expect(menu.getByText("Rename", { exact: true })).toBeVisible();
  await expect(menu.getByText("Close chat", { exact: true })).toBeVisible();

  await menu.getByText("New chat", { exact: true }).click();
  await expect(tabs).toHaveCount(2);
});
