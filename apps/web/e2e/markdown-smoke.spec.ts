import { expect, test } from "@playwright/test";

// Full-surface markdown smoke test. Renders the MARKDOWN_SMOKE fixture (every markdown
// construct) through the DS `<Markdown>` renderer (@protolabsai/ui, adopted in #1330) and
// asserts structure + chrome + KaTeX math render. A screenshot of the rendered message is
// saved for visual brand review. Mermaid renders as a themed code block by default (DS
// `renderMermaid` is opt-in — it's heavy), so SVG diagrams aren't expected here.

test("full markdown surface renders (structure + chrome)", async ({ page }, testInfo) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill("MARKDOWN_SMOKE: render the full surface");
  await composer.press("Enter");

  const md = page.locator(".pl-message--assistant .markdown");
  await expect(md).toBeVisible();

  // ── Structure: streamdown emits these as real semantic tags ──────────────────
  await expect(md.locator("h1")).toContainText("Markdown smoke test");
  await expect(md.locator("h2")).toContainText("H2 heading");
  await expect(md.locator('[data-streamdown="strong"]')).toContainText("bold");
  await expect(md.locator("ul li").first()).toBeVisible();
  await expect(md.locator("ol li")).toHaveCount(2);
  await expect(md.locator('[data-streamdown="blockquote"]').first()).toBeVisible();
  // NB: two code blocks render (the ts block + the mermaid block, which falls back to a code
  // block because mermaid isn't wired — see the audit annotations), so target by text.
  await expect(md.locator("pre code").filter({ hasText: "export const add" })).toBeVisible();
  await expect(md.locator("table")).toBeVisible();
  await expect(md.locator("table td").first()).toContainText("alpha");
  await expect(md.locator('[data-streamdown="horizontal-rule"]')).toBeVisible();

  // ── DS-themed chrome + math: the DS renderer copy button + table wrapper are present
  //    (themed via [data-streamdown]); table download/fullscreen are off by default for a
  //    chat bubble, so the table carries a single (copy) control. KaTeX math now renders. ──
  await expect(md.locator('[data-streamdown="code-block-copy-button"]').first()).toBeAttached();
  await expect(md.locator('[data-streamdown="table-wrapper"]').first()).toBeAttached();
  await expect(md.locator(".katex").first()).toBeVisible();

  // Audit annotations (non-failing) — the render state of the harder constructs, for the
  // brand-review screenshot below.
  const tableButtons = await md.locator('[data-streamdown="table-wrapper"] button').count();
  const taskboxes = await md.locator('li input[type="checkbox"]').count();
  const katex = await md.locator(".katex").count();
  const mermaid = await md.locator('[data-streamdown="mermaid"] svg, [data-streamdown="mermaid-block"] svg').count();
  testInfo.annotations.push(
    { type: "audit", description: `table chrome buttons: ${tableButtons} (copy-only — download/fullscreen off for chat)` },
    { type: "audit", description: `task-list checkboxes: ${taskboxes} (expect 2)` },
    { type: "audit", description: `KaTeX math nodes: ${katex} (math wired via the DS renderer)` },
    { type: "audit", description: `mermaid SVG nodes: ${mermaid} (0 = themed code block; DS renderMermaid is opt-in)` },
  );

  // Visual artifact for the brand-style audit (attached to the Playwright report).
  await md.screenshot({ path: testInfo.outputPath("markdown-surface.png") });
  await testInfo.attach("markdown-surface", {
    path: testInfo.outputPath("markdown-surface.png"),
    contentType: "image/png",
  });
});
