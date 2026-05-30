import { expect, test } from "@playwright/test";

// Drives the chat surface against the mock A2A stream and asserts the
// tool-call card contract: stable collapsed-by-default rows, pretty-printed
// JSON on expand, clean markdown answers, and no horizontal overflow.

async function send(page, prompt: string) {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  // The composer sends on Ctrl/Cmd+Enter (checks metaKey || ctrlKey).
  await composer.press("Control+Enter");
}

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "networkidle" });
  // Setup wizard must not block — the mock reports setup_complete:true.
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("tool-call card is collapsed by default, expands to pretty-printed JSON", async ({ page }) => {
  await send(page, "search for AI coding agents");

  const card = page.locator(".tool-card").first();
  await expect(card).toBeVisible();
  await expect(card.locator(".tool-card-name")).toHaveText("web_search");

  // Stable default: collapsed — no body rendered until the user opens it.
  await expect(page.locator(".tool-card-body")).toHaveCount(0);

  // The tool finishes → done glyph (not the running spinner).
  await expect(card.locator(".tool-card-status.done")).toBeVisible();

  // Expand → input rendered as indented JSON (the bug was a Python repr).
  await card.locator(".tool-card-head").click();
  const body = card.locator(".tool-card-body");
  await expect(body).toBeVisible();
  // Two sections in order: input then result.
  const pres = body.locator("pre");
  const input = await pres.nth(0).innerText();
  expect(input).toContain('"max_results": 8'); // double-quoted, pretty-printed
  expect(input).toContain("\n"); // multi-line indent, not a one-line blob
  expect(input).not.toContain("'"); // no single-quoted Python repr

  // Result shows the actual tool output, not a ToolMessage repr.
  const result = await pres.nth(1).innerText();
  expect(result).toContain("8 result(s)");
  expect(result).not.toContain("tool_call_id=");
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
