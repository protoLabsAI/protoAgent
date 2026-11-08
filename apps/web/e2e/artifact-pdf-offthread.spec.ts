import { expect, test, type Frame, type Page } from "@playwright/test";

import { HEAVY_PDF_ID, HEAVY_PDF_PAGES, heavyPdf } from "./pdfFixture.mjs";

// A large PDF must not freeze the page it's shown in. Stress test of v0.231.0: warBoss's console
// lagged 400–470 ms on every load with a 30.7 MB rulebook in the Artifact panel. The artifact
// frame is a no-same-origin sandbox, but WebKit (Safari, the desktop app's WKWebView) runs a
// sandboxed iframe on its embedder's main thread, so pdf.js parsing and decoding in the frame
// blocked the whole console. pdf.js now runs in a real dedicated Worker, built from the
// SRI-checked vendored worker source the shell posts in; only canvas drawing stays on the main
// thread.
//
// Measured in the frame itself with a 16 ms setInterval probe (an init script, which runs in every
// frame and isn't subject to the frame's CSP): the longest gap between ticks while the document
// loads and its first pages draw. On the old main-thread path this fixture's longest gap is
// ~220 ms in Chromium; in a Worker it's about one frame.

const VIEW = "/plugins/artifact/view";
const PDF = heavyPdf();

const STORE = {
  current: HEAVY_PDF_ID,
  artifacts: [
    {
      id: HEAVY_PDF_ID,
      kind: "file",
      title: "core-rules.pdf",
      versions: [
        {
          code: "(extracted text)",
          ts: 1,
          by: "agent",
          file: {
            filename: "core-rules.pdf",
            mime: "application/pdf",
            size: PDF.length,
            thumb: "",
            pdf: { render: true, pages: HEAVY_PDF_PAGES },
          },
          blob: HEAVY_PDF_ID + ".bin",
        },
      ],
      version_count: 1,
      created: 1,
      updated: 1,
    },
  ],
};

async function frame(page: Page): Promise<Frame> {
  const handle = await page.locator("#frame").elementHandle();
  const f = handle ? await handle.contentFrame() : null;
  if (!f) throw new Error("no artifact frame");
  return f;
}

type Measured = { engine: string | null | undefined; err: string; lag: number; errors: string[] };

// Load the heavy PDF in the artifact view and measure the frame's longest main-thread gap while
// it loads and draws its first page. `noWorker` removes Worker from the artifact frame only, which
// forces pdf.js onto its main-thread fallback.
async function loadAndMeasure(page: Page, { noWorker = false } = {}): Promise<Measured> {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  // The lag probe, in every frame: the longest gap between 16 ms ticks, past the 16 ms itself.
  await page.addInitScript((drop: boolean) => {
    if (drop && location.href === "about:srcdoc") (window as unknown as { Worker?: unknown }).Worker = undefined;
    const w = window as unknown as { __lag: { max: number; last: number } };
    w.__lag = { max: 0, last: performance.now() };
    setInterval(() => {
      const now = performance.now();
      w.__lag.max = Math.max(w.__lag.max, now - w.__lag.last - 16);
      w.__lag.last = now;
    }, 16);
  }, noWorker);
  // The store is canned here; the blob itself comes from the mock server (pdfFixture.mjs).
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: STORE }));
  await page.goto(VIEW);
  // The first page is drawn: pdf.js parsed the file, decoded its image and ran its operator list.
  await expect
    .poll(
      async () => {
        try {
          return await (await frame(page)).evaluate(() => document.querySelectorAll("#pages canvas").length);
        } catch {
          return 0;
        }
      },
      { timeout: 60_000, message: `the PDF never drew a page (${noWorker ? "fallback" : "worker"} path)` },
    )
    .toBeGreaterThan(0);
  const got = await (await frame(page)).evaluate(() => ({
    engine: document.getElementById("pages")?.getAttribute("data-engine"),
    err: document.getElementById("status")?.textContent ?? "",
    lag: (window as unknown as { __lag: { max: number } }).__lag.max,
  }));
  return { ...got, errors };
}

test("a large PDF renders in a Worker", async ({ page }) => {
  test.setTimeout(90_000);
  const got = await loadAndMeasure(page);
  console.log(`pdf frame: engine=${got.engine} longest main-thread gap=${Math.round(got.lag)} ms`);
  expect(got.err).toBe("");
  expect(got.engine).toBe("worker"); // pdf.js got a dedicated Worker, not the main-thread fallback
  expect(got.errors).toEqual([]);
});

test("with no Worker available, the PDF still renders on the frame's main thread", async ({ page }) => {
  test.setTimeout(90_000);
  const got = await loadAndMeasure(page, { noWorker: true });
  expect({ engine: got.engine, err: got.err }).toEqual({ engine: "main", err: "" });
  expect(got.errors).toEqual([]);
});

// The point of the Worker: the frame's main thread keeps ticking while the document loads. An
// absolute bound can't hold across machines (34 ms locally, 275-432 ms on a CI runner), so this
// compares the two engines on the SAME machine in the same run: the Worker path must hitch at
// most half as long as the main-thread fallback does.
test("the Worker path blocks the main thread far less than the fallback", async ({ browser }) => {
  test.setTimeout(180_000);
  const fallbackPage = await browser.newPage();
  const workerPage = await browser.newPage();
  try {
    const main = await loadAndMeasure(fallbackPage, { noWorker: true });
    const worker = await loadAndMeasure(workerPage);
    console.log(`pdf main-thread gap: worker=${Math.round(worker.lag)} ms fallback=${Math.round(main.lag)} ms`);
    expect(worker.engine).toBe("worker");
    expect(main.engine).toBe("main");
    expect(worker.lag).toBeLessThanOrEqual(main.lag * 0.5);
  } finally {
    await fallbackPage.close();
    await workerPage.close();
  }
});
