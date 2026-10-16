import { expect, test, type Frame, type Page } from "@playwright/test";

// Inline image/video for `file` artifacts (#4257), in the REAL shell (plugins/artifact/shell.js,
// served by the mock): a PNG shows as an <img> (click toggles full size) and a video plays in a
// <video controls> — not the "(image · image/png)" / "(binary file … download to open)" text.
// The frame is an opaque sandbox with no bearer, so it asks the shell; the shell mints a ticket
// on the gated media-ticket route and posts back a URL on the public media route (both stubbed
// here — tests/test_artifact_media.py covers the real routes, Range included). Also pinned: an
// artifact that touches localStorage no longer throws a SecurityError pageerror in the sandbox.

const VIEW = "/plugins/artifact/view";
// 2 s, 32x24 VP8 WebM (Playwright's Chromium has no H.264, so the e2e plays WebM) and a 48x32 PNG.
const WEBM = Buffer.from("GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAMIEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHWTbuMU6uEElTDZ1OsggEjTbuMU6uEHFO7a1OsggLg7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsCrXsYMPQkBNgIxMYXZmNjIuMy4xMDBXQYxMYXZmNjIuMy4xMDBEiYhAn0AAAAAAABZUrmvIrgEAAAAAAAA/14EBc8WImyk6HsHdWoicgQAitZyDdW5kiIEAhoVWX1ZQOIOBASPjg4QL68IA4JCwgSC6gRiagQJVsIRVuYEBElTDZ/tzc59jwIBnyJlFo4dFTkNPREVSRIeMTGF2ZjYyLjMuMTAwc3PWY8CLY8WImyk6HsHdWohnyKFFo4dFTkNPREVSRIeUTGF2YzYyLjExLjEwMCBsaWJ2cHhnyKFFo4hEVVJBVElPTkSHkzAwOjAwOjAyLjAwMDAwMDAwMAAfQ7Z1QTfngQCjvIEAAIDQAgCdASogABgAAEcIhYWIhYSIAgICdaoD+AIIISmecP77aJf/PTNeYP8nP/25H+3I/25H/7bCAKOVgQDIALEBAAEQEAAYABhYL/QACHAAo5WBAZAAsQEAARAQABgAGFgv9AAIcACjlYECWACxAQABEBAAGAAYWC/0AAhwAKOVgQMgALEBAAEQEAAYABhYL/QACHAAo7yBA+iA0AIAnQEqIAAYAABHCIWFiIWEiAICAnWqA/gCCCEpnnD++2iX/z0zXmD/Jz/9uR/tyP9uR/+2wgCjlYEEsACxAQABEBAAGAAYWC/0AAhwAKOVgQV4ALEBAAEQEAAYABhYL/QACHAAo5WBBkAAsQEAARAQABgAGFgv9AAIcACjlYEHCACxAQABEBAAGAAYWC/0AAhwABxTu2uju4+zgQC3iveBAfGCAaPwgQO7kLOCA+i3iveBAfGCAaPwgZ0=", "base64");
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAADAAAAAgCAIAAADbtmxLAAAAMUlEQVR42u3OAQ0AAAQAMKTRP4VYapj9CZ7THZdUHCMkJCQkJCQkJCQkJCQkJCT0O7Qo2gFY3o3zaQAAAABJRU5ErkJggg==", "base64");

const fileArt = (id: string, filename: string, mime: string, size: number, code: string) => ({
  id,
  kind: "file",
  title: filename,
  versions: [{ code, ts: 1, by: "agent", file: { filename, mime, size, thumb: "" }, blob: id + ".bin" }],
  version_count: 1,
  created: 1,
  updated: 1,
});

const STORE = {
  current: "art-png",
  artifacts: [
    fileArt("art-png", "review-sheet.png", "image/png", PNG.length, "(image · image/png)"),
    fileArt("art-webm", "clip.webm", "video/webm", WEBM.length, "(binary file · video/webm · 824 bytes — download to open)"),
    {
      id: "art-ls",
      kind: "html",
      title: "Remembers a tab",
      versions: [
        {
          code: '<p id="ls">…</p><script>localStorage.setItem("tab","2");sessionStorage.setItem("s","1");document.getElementById("ls").textContent="tab="+localStorage.getItem("tab")+" s="+sessionStorage.getItem("s");</script>',
          ts: 1,
          by: "agent",
        },
      ],
      version_count: 1,
      created: 1,
      updated: 1,
    },
  ],
};

async function setup(page: Page, errors: string[]) {
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: STORE }));
  await page.route("**/api/plugins/artifact/artifact/*/media-ticket*", (route) => {
    const id = new URL(route.request().url()).pathname.split("/").slice(-2)[0];
    return route.fulfill({ json: { ticket: "tk", version: 1, path: `/plugins/artifact/media/${id}?version=1&t=tk` } });
  });
  await page.route("**/plugins/artifact/media/**", (route) => {
    const id = new URL(route.request().url()).pathname.split("/").pop();
    const [body, type] = id === "art-webm" ? [WEBM, "video/webm"] : [PNG, "image/png"];
    return route.fulfill({ body, contentType: type, headers: { "accept-ranges": "bytes" } });
  });
  await page.goto(VIEW);
  await expect(page.locator("#art option")).toHaveCount(STORE.artifacts.length);
}

async function frame(page: Page): Promise<Frame> {
  const handle = await page.locator("#frame").elementHandle();
  const f = handle ? await handle.contentFrame() : null;
  if (!f) throw new Error("no artifact frame");
  return f;
}

async function poll<T>(page: Page, fn: (f: Frame) => Promise<T>, want: T, message: string) {
  await expect
    .poll(
      async () => {
        try {
          return await fn(await frame(page));
        } catch {
          return null;
        }
      },
      { timeout: 20_000, message },
    )
    .toEqual(want);
}

test("a PNG file artifact shows inline and toggles full size", async ({ page }) => {
  const errors: string[] = [];
  await setup(page, errors);
  await page.selectOption("#art", "art-png");
  await poll(page, (f) => f.evaluate(() => (document.getElementById("m") as HTMLImageElement | null)?.naturalWidth ?? 0), 48, "image never loaded");
  const f = await frame(page);
  expect(await f.evaluate(() => document.body.textContent?.includes("(image · image/png)"))).toBe(false);
  await f.locator("label.stage").click();
  expect(await f.evaluate(() => (document.getElementById("z") as HTMLInputElement).checked)).toBe(true);
  expect(errors).toEqual([]);
});

test("a video file artifact plays inline and seeks", async ({ page }) => {
  const errors: string[] = [];
  await setup(page, errors);
  await page.selectOption("#art", "art-webm");
  await poll(
    page,
    (f) => f.evaluate(() => { const v = document.querySelector("video"); return !!v && v.controls && v.readyState >= 1 && v.duration > 1.5; }),
    true,
    "video metadata never loaded",
  );
  const f = await frame(page);
  expect(await f.evaluate(() => document.body.textContent?.includes("download to open"))).toBe(false);
  const seeked = await f.evaluate(
    () =>
      new Promise<number>((resolve) => {
        const v = document.querySelector("video") as HTMLVideoElement;
        v.addEventListener("seeked", () => resolve(v.currentTime), { once: true });
        v.currentTime = 1;
      }),
  );
  expect(seeked).toBeGreaterThan(0.5);
  expect(errors).toEqual([]);
});

test("an artifact that uses localStorage runs in the sandbox without a pageerror", async ({ page }) => {
  const errors: string[] = [];
  await setup(page, errors);
  await page.selectOption("#art", "art-ls");
  await poll(page, (f) => f.evaluate(() => document.getElementById("ls")?.textContent ?? ""), "tab=2 s=1", "artifact never ran");
  expect(await (await frame(page)).evaluate(() => document.getElementById("__arterr")?.textContent ?? null)).toBeNull();
  expect(errors).toEqual([]);
});
