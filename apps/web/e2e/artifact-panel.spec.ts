import { expect, test, type Frame, type Page } from "@playwright/test";

// The Artifact panel renders each artifact into a sandboxed srcdoc iframe built by
// plugins/artifact/shell.js. The mock serves that REAL shell against a canned store
// (fixtures.mjs ARTIFACT_STORE), so these specs exercise the actual srcdoc builder in a real
// browser, across every artifact kind.
//
// The contract pinned here: an html artifact that is a FULL document keeps its own prologue.
// The shell used to prepend the design-system <link> + its base <style>/<script> AHEAD of the
// artifact's `<!doctype html>`; a doctype (or a `<head>` tag) that follows content is a parse
// error the parser discards, so the panel showed the document with no doctype
// (document.doctype null) and without its <head> attributes. Note what it did NOT do: a srcdoc
// document is never in quirks mode (HTML spec, "initial" insertion mode), so the panel was
// already in standards mode — the assertions below hold compatMode to that as a guard.

const VIEW = "/plugins/artifact/view";
const ARTIFACT_COUNT = 7;

// Facts read INSIDE the artifact frame. `--pl-space-2` is defined only by the injected
// /_ds/plugin-kit.css (base() inlines just four colour tokens), so it proves the DS sheet applied.
function probe(ready: string) {
  const w = window as unknown as { __artErr?: unknown; protoArtifact?: { ask?: unknown } };
  const body = document.body ? getComputedStyle(document.body) : null;
  return {
    ready: !!document.querySelector(ready),
    compatMode: document.compatMode,
    doctype: document.doctype ? document.doctype.name : null,
    headTemplate: document.head?.getAttribute("data-template") ?? null,
    dsToken: getComputedStyle(document.documentElement).getPropertyValue("--pl-space-2").trim(),
    dsLinkInHead: !!document.head?.querySelector('link[href$="/_ds/plugin-kit.css"]'),
    harness: typeof w.__artErr === "function" && typeof w.protoArtifact?.ask === "function",
    renderError: document.getElementById("__arterr")?.textContent ?? null,
    bodyHeight: document.body ? document.body.getBoundingClientRect().height : 0,
    viewportHeight: window.innerHeight,
    lang: document.documentElement.lang,
    bodyFont: body ? body.fontFamily : "",
    bodyFontSize: body ? body.fontSize : "",
  };
}
type Probe = ReturnType<typeof probe>;

async function artifactFrame(page: Page): Promise<Frame | null> {
  const handle = await page.locator("#frame").elementHandle();
  return handle ? handle.contentFrame() : null;
}

// Select an artifact in the picker and wait until ITS content is in the frame (a srcdoc swap is
// an async navigation — evaluating mid-swap throws, so that counts as "not yet").
async function show(page: Page, id: string, ready: string, opts: { ds?: boolean } = {}): Promise<Probe> {
  await page.selectOption("#art", id);
  let last: Probe | null = null;
  await expect
    .poll(
      async () => {
        try {
          const frame = await artifactFrame(page);
          last = frame ? await frame.evaluate(probe, ready) : null;
        } catch {
          last = null;
        }
        // DS kinds: also wait for the stylesheet — it loads after the marker can exist.
        return !!last && last.ready && (!opts.ds || last.dsToken !== "");
      },
      { timeout: 20_000, message: `artifact ${id} never rendered ${ready}` },
    )
    .toBe(true);
  return last as unknown as Probe;
}

test.beforeEach(async ({ page }) => {
  await page.goto(VIEW);
  // The picker fills from /api/plugins/artifact/history once the shell boots.
  await expect(page.locator("#art option")).toHaveCount(ARTIFACT_COUNT);
});

test("a full HTML document keeps its doctype and <head> in the panel", async ({ page }) => {
  const p = await show(page, "art-doc", "#resume", { ds: true });
  expect(p.doctype).toBe("html");
  expect(p.headTemplate).toBe("resume"); // the author's <head> tag was honoured, not discarded
  expect(p.lang).toBe("en");
  expect(p.compatMode).toBe("CSS1Compat");
  // Still themed: the DS sheet + base block were injected INTO the head, not dropped.
  expect(p.dsToken).toBe("8px");
  expect(p.dsLinkInHead).toBe(true);
  expect(p.harness).toBe(true); // protoArtifact.ask bridge + error overlay still installed
  expect(p.renderError).toBeNull();
  // Cascade order preserved: the injection sits AHEAD of the author's own styles, so the
  // resume's body font beats plugin-kit.css's body typography, as before.
  expect(p.bodyFont).toContain("Georgia");
  expect(p.bodyFontSize).toBe("15px");
  // No quirks body-stretch: the body wraps its short content rather than filling the viewport.
  expect(p.bodyHeight).toBeLessThan(p.viewportHeight / 2);
});

test("a doctype-only document (leading comment, no <html>/<head>) keeps its doctype", async ({ page }) => {
  const p = await show(page, "art-doc-bare", "#bare", { ds: true });
  expect(p.doctype).toBe("html");
  expect(p.compatMode).toBe("CSS1Compat");
  expect(p.dsToken).toBe("8px");
  expect(p.harness).toBe(true);
});

test("an HTML fragment still gets the design-system styles", async ({ page }) => {
  const p = await show(page, "art-frag", "#frag", { ds: true });
  expect(p.dsToken).toBe("8px");
  expect(p.harness).toBe(true);
  expect(p.renderError).toBeNull();
  // Unchanged: a fragment has no doctype, and a srcdoc document is standards mode regardless.
  expect(p.doctype).toBeNull();
  expect(p.compatMode).toBe("CSS1Compat");
});

test("markdown, svg, mermaid and react artifacts still render", async ({ page }) => {
  test.setTimeout(90_000);
  const cases: Array<[id: string, ready: string, ds: boolean]> = [
    ["art-md", "#md h1", true],
    ["art-svg", "#__vp #dot", false],
    ["art-mermaid", "#__vp svg", false],
    ["art-react", "#root #hello", true],
  ];
  for (const [id, ready, ds] of cases) {
    const p = await show(page, id, ready, { ds });
    expect(p.compatMode, id).toBe("CSS1Compat");
    expect(p.doctype, id).toBe("html");
    expect(p.renderError, id).toBeNull();
    if (ds) expect(p.dsToken, id).toBe("8px");
  }
});
