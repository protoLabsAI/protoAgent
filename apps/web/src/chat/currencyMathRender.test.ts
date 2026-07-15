// Render-level proof: mount the REAL <Markdown> (DS → streamdown → remark-math → KaTeX) and
// assert currency amounts reach the DOM as plain text (no `.katex` element), while genuine
// `$$…$$` display math still renders KaTeX. The pure escape logic is covered in
// currencyMath.test.ts; this covers the wiring through the DS component the screenshot bug
// came from.
import { afterEach, describe, expect, it } from "vitest";
import { act } from "react";
import { createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import { Markdown } from "./Markdown";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(md: string): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(createElement(Markdown, null, md));
  });
  return host;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

describe("<Markdown> currency is not rendered as math", () => {
  it("the screenshot repro: paired currency amounts render as text, no KaTeX span", async () => {
    const el = await render("Funding: ~A$180M total raised. Latest round A$63–90M. Valuation likely $600M–$1.1B.");
    expect(el.querySelector(".katex")).toBeNull();
    // The literal dollars-and-digits survive (backslash escape consumed by CommonMark).
    expect(el.textContent).toContain("$180M");
    expect(el.textContent).toContain("$600M");
  });

  it("genuine `$$…$$` display math still renders KaTeX", async () => {
    const el = await render("Euler's identity: $$e^{i\\pi} + 1 = 0$$");
    expect(el.querySelector(".katex")).not.toBeNull();
  });
});
