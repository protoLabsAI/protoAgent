import { expect, test } from "@playwright/test";

// Full-surface markdown smoke test. Renders the MARKDOWN_SMOKE fixture (every markdown
// construct) through the real streamdown chat pipeline and asserts the structure + the
// interactive chrome render. Doubles as the brand/style audit surface for the DS work in
// protoContent#297 (chrome off-brand) and #298 (DS markdown renderer): a screenshot of the
// rendered message is saved for visual review, and the elements that don't render
// deterministically (task-list checkboxes, KaTeX math, mermaid) are annotated, not failed.

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

  // ── Interactive chrome (the protoContent#297 surface): these MUST render so the
  //    DS has something to theme; if streamdown stops emitting them the audit is stale ──
  await expect(md.locator('[data-streamdown="code-block-copy-button"]').first()).toBeAttached();
  await expect(md.locator('[data-streamdown="table-wrapper"]').first()).toBeAttached();

  // ── Audit annotations (non-failing): the current render state of the constructs the DS
  //    must own/theme — captured so the report documents gaps without flaking the guard ──
  const tableButtons = await md.locator('[data-streamdown="table-wrapper"] button').count();
  const taskboxes = await md.locator('li input[type="checkbox"]').count();
  const katex = await md.locator(".katex").count();
  const mermaid = await md.locator('[data-streamdown="mermaid"] svg, [data-streamdown="mermaid-block"] svg').count();
  testInfo.annotations.push(
    { type: "audit", description: `table chrome buttons: ${tableButtons} (off-brand — protoContent#297)` },
    { type: "audit", description: `task-list checkboxes: ${taskboxes} (expect 2)` },
    { type: "audit", description: `KaTeX math nodes: ${katex} (0 ⇒ math NOT wired — protoContent#298)` },
    { type: "audit", description: `mermaid SVG nodes: ${mermaid} (0 ⇒ mermaid NOT wired — protoContent#298)` },
  );

  // Visual artifact for the brand-style audit (attached to the Playwright report).
  await md.screenshot({ path: testInfo.outputPath("markdown-surface.png") });
  await testInfo.attach("markdown-surface", {
    path: testInfo.outputPath("markdown-surface.png"),
    contentType: "image/png",
  });
});
