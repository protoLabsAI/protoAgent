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
  await page.goto("/app/", { waitUntil: "networkidle" });
  // Setup wizard must not block — the mock reports setup_complete:true.
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("Enter sends; Ctrl+Enter inserts a newline", async ({ page }) => {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.fill("line one");
  await composer.press("Control+Enter"); // newline, not send
  await expect(composer).toHaveValue("line one\n");
  await expect(page.locator(".message-user")).toHaveCount(0);

  await composer.fill("hello there"); // plain Enter sends
  await composer.press("Enter");
  await expect(page.locator(".message-user")).toHaveText(/hello there/);
});

test("tool-call card is collapsed by default and renders structured components", async ({ page }) => {
  await send(page, "search for AI coding agents");

  const card = page.locator(".tool-card").first();
  await expect(card).toBeVisible();
  await expect(card.locator(".tool-card-name")).toHaveText("web_search");

  // Stable default: collapsed — no body rendered until the user opens it.
  await expect(page.locator(".tool-card-body")).toHaveCount(0);

  // The tool finishes → done glyph (not the running spinner).
  await expect(card.locator(".tool-card-status.done")).toBeVisible();

  // A duration pill is stamped on completion (mock gaps frames ~40ms).
  await expect(card.locator(".tool-card-dur")).toHaveText(/^\d+ms$|^\d+\.\d+s$/);

  await card.locator(".tool-card-head").click();
  const body = card.locator(".tool-card-body");
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

  // Final answer renders through the markdown pipeline.
  const md = page.locator(".message-assistant .markdown");
  await expect(md.locator("h2")).toHaveText("Summary");
  await expect(md.locator("strong")).toHaveText("key");
  await expect(md.locator("li")).toHaveCount(2);
  await expect(md.locator("pre code")).toContainText("const x = 1;");
});

test("long tool values do not overflow the chat horizontally", async ({ page }) => {
  await send(page, "OVERFLOW: trigger a long token");

  const card = page.locator(".tool-card").first();
  await expect(card).toBeVisible();
  await card.locator(".tool-card-head").click();
  await expect(card.locator(".tool-card-body")).toBeVisible();

  const metrics = await page.evaluate(() => {
    const body = document.querySelector(".message-assistant .message-body");
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
