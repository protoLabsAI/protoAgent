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

  // ── Multi-line code fence keeps its line structure ───────────────────────────
  // Regression guard for the [data-streamdown] schema drift that dropped white-space:pre and
  // collapsed a whole 4-line fenced block onto ONE forever-scrolling line. The newlines live in
  // the DOM either way, so the collapse is purely a CSS-render fact — assert the block body's
  // <pre> COMPUTES to a whitespace-preserving mode (a future drift that loses the selector's grip
  // reverts it to `normal` and trips this), and that streamdown still emits one span per source
  // line (the 4-line ts fixture → 4 line spans).
  //
  // #2659: streamdown's code block renders through a Suspense boundary — a synchronous raw
  // fallback `<pre>` (streamdown/dist/chunk*.js `st`), swapped ONCE for a freshly-mounted
  // `<pre>` from the lazy-loaded HighlightedCodeBlockBody (streamdown/dist/highlighted-body*.js)
  // as soon as that chunk resolves, which then updates its own children in place as Shiki's
  // async `highlight()` (@streamdown/code) resolves real tokens. Reading whiteSpace/innerText/
  // span-count as SEPARATE Playwright round-trips can straddle that swap and catch an
  // already-detached node (empty computed style, or an innerText collapsed because a detached
  // element has no layout box for the browser's line-break algorithm to key off). There's no
  // "highlighting done" DOM attribute/event exposed, but the fallback tokens are recognizable —
  // every fallback token's inline `--sdm-c` custom property is literally the string "inherit"
  // (chunk*.js `st`'s synthetic token list), while real Shiki tokens always carry a concrete
  // theme color. Poll INSIDE the page for the first REAL (non-fallback) render, then require a
  // few consecutive identical reads before trusting it's settled, and take every measurement
  // (whiteSpace, innerText, span count) from that SAME browser-side snapshot — one coherent
  // final read instead of a sequence of reads that can straddle the node swap.
  const finalSnapshot = await page.waitForFunction(
    () => {
      const w = window as unknown as { __mdSmokePrevJson?: string; __mdSmokeStableCount?: number };
      const candidates = Array.from(document.querySelectorAll("pre")).filter((el) =>
        (el.textContent || "").includes("export const add"),
      );
      if (candidates.length !== 1 || !candidates[0].isConnected) {
        w.__mdSmokeStableCount = 0;
        return null; // zero or >1 matches (or a detached node) — mid-swap, keep polling
      }
      const pre = candidates[0] as HTMLElement;
      const firstToken = pre.querySelector("code > span > span");
      const colorMatch = /--sdm-c:\s*([^;]+)/.exec(firstToken?.getAttribute("style") || "");
      const isRealHighlight = !!colorMatch && colorMatch[1].trim() !== "inherit";
      if (!isRealHighlight) {
        w.__mdSmokeStableCount = 0;
        return null; // still the synthetic pre-highlight fallback — keep polling
      }
      const snapshot = {
        whiteSpace: getComputedStyle(pre).whiteSpace,
        innerText: pre.innerText,
        spanCount: pre.querySelectorAll("code > span").length,
      };
      const json = JSON.stringify(snapshot);
      w.__mdSmokeStableCount = json === w.__mdSmokePrevJson ? (w.__mdSmokeStableCount || 0) + 1 : 1;
      w.__mdSmokePrevJson = json;
      // Require 3 consecutive identical reads (not just "highlighted once") so a stray
      // in-place re-render right after the swap can't slip a half-updated snapshot through.
      return w.__mdSmokeStableCount >= 3 ? snapshot : null;
    },
    undefined,
    { timeout: 15_000, polling: 25 },
  );
  const { whiteSpace: tsWhiteSpace, innerText: rendered, spanCount: tsSpanCount } =
    (await finalSnapshot.jsonValue()) as { whiteSpace: string; innerText: string; spanCount: number };
  expect(tsWhiteSpace).toMatch(/^pre/);
  // …but computed `white-space` alone cannot catch #2612: streamdown emits one <span> per
  // LINE with no newline text nodes between them, so the lines flow together while
  // `white-space` still reports `pre` and the span count still matches. Assert the property
  // that actually matters — the RENDERED text keeps its line breaks AND its indentation.
  // (The fixture block is indented on purpose; a flush-left fixture cannot fail this.)
  expect(rendered).toContain("function demo(items: string[]) {\n");
  expect(rendered).toContain("\n  if (items.length) {\n");
  expect(rendered).toContain("\n    return items.map((s) => s.trim());\n");
  expect(tsSpanCount).toBe(10); // 6 indented lines + 4 exports
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

  // ── Image chrome (#1960): the DS MarkdownImage action cluster + Lightbox ─────
  // Two fixture images: the loadable /media/ one (mock-served PNG) carries the live
  // chrome; the unreachable example.com one exercises the broken-image fallback.
  const imgWrap = md
    .locator('[data-streamdown="image-wrapper"]')
    .filter({ has: page.locator('[data-streamdown="image"][src*="/media/"]') });
  await expect(imgWrap.locator('[data-streamdown="image"]')).toBeVisible();
  await expect(md.locator('[data-streamdown="image-fallback"]')).toBeVisible();
  const cluster = imgWrap.locator(".pl-md-img-actions");
  await expect(cluster.getByRole("button", { name: "Download image" })).toBeAttached();
  await expect(cluster.getByRole("button", { name: "Open in new tab" })).toBeAttached();
  await imgWrap.hover(); // cluster is hover/focus-revealed (always-on only for touch)
  await imgWrap.screenshot({ path: testInfo.outputPath("image-cluster-hover.png") });
  await testInfo.attach("image-cluster-hover", {
    path: testInfo.outputPath("image-cluster-hover.png"),
    contentType: "image/png",
  });
  await cluster.getByRole("button", { name: "View fullscreen" }).click();
  const lightbox = page.locator(".pl-lightbox");
  await expect(lightbox).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("image-lightbox.png") });
  await testInfo.attach("image-lightbox", {
    path: testInfo.outputPath("image-lightbox.png"),
    contentType: "image/png",
  });
  await page.keyboard.press("Escape"); // Lightbox dismisses like every pl-overlay
  await expect(lightbox).toHaveCount(0);

  // Visual artifact for the brand-style audit (attached to the Playwright report).
  await md.screenshot({ path: testInfo.outputPath("markdown-surface.png") });
  await testInfo.attach("markdown-surface", {
    path: testInfo.outputPath("markdown-surface.png"),
    contentType: "image/png",
  });
});
