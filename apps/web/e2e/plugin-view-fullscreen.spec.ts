import { expect, test, type Frame, type FrameLocator, type Page } from "@playwright/test";

import { ARTIFACT_PLUGIN_STATUS } from "./artifactPanel";

// Fullscreen inside console plugin views (#4286). A frame can only enter fullscreen when every
// frame above it is allowed the `fullscreen` Permissions-Policy feature, whose default allowlist
// is 'self'. The plugin-view iframe granted clipboard only, so wherever the frame is cross-origin
// to the console (the desktop app) a <video controls> fullscreen button did nothing and
// requestFullscreen() rejected. Pinned here in the REAL console against the mock:
//   • a plain plugin view (the mock's "Board") can requestFullscreen() from inside its frame,
//     same-origin AND cross-origin (the desktop shape);
//   • the Artifact panel's inline video (#4257), two frames down (console → plugin page →
//     the shell's sandboxed srcdoc frame), can too: the shell grants its nested frame
//     allow="fullscreen" only for its own video document;
//   • model-generated HTML in that same nested frame is still refused (no grant).
// Playwright evaluates with a user gesture, which the Fullscreen API requires.

// 2 s, 32x24 VP8 WebM (same clip as artifact-media.spec.ts; Chromium here has no H.264).
const WEBM = Buffer.from("GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAMIEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHWTbuMU6uEElTDZ1OsggEjTbuMU6uEHFO7a1OsggLg7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsCrXsYMPQkBNgIxMYXZmNjIuMy4xMDBXQYxMYXZmNjIuMy4xMDBEiYhAn0AAAAAAABZUrmvIrgEAAAAAAAA/14EBc8WImyk6HsHdWoicgQAitZyDdW5kiIEAhoVWX1ZQOIOBASPjg4QL68IA4JCwgSC6gRiagQJVsIRVuYEBElTDZ/tzc59jwIBnyJlFo4dFTkNPREVSRIeMTGF2ZjYyLjMuMTAwc3PWY8CLY8WImyk6HsHdWohnyKFFo4dFTkNPREVSRIeUTGF2YzYyLjExLjEwMCBsaWJ2cHhnyKFFo4hEVVJBVElPTkSHkzAwOjAwOjAyLjAwMDAwMDAwMAAfQ7Z1QTfngQCjvIEAAIDQAgCdASogABgAAEcIhYWIhYSIAgICdaoD+AIIISmecP77aJf/PTNeYP8nP/25H+3I/25H/7bCAKOVgQDIALEBAAEQEAAYABhYL/QACHAAo5WBAZAAsQEAARAQABgAGFgv9AAIcACjlYECWACxAQABEBAAGAAYWC/0AAhwAKOVgQMgALEBAAEQEAAYABhYL/QACHAAo7yBA+iA0AIAnQEqIAAYAABHCIWFiIWEiAICAnWqA/gCCCEpnnD++2iX/z0zXmD/Jz/9uR/tyP9uR/+2wgCjlYEEsACxAQABEBAAGAAYWC/0AAhwAKOVgQV4ALEBAAEQEAAYABhYL/QACHAAo5WBBkAAsQEAARAQABgAGFgv9AAIcACjlYEHCACxAQABEBAAGAAYWC/0AAhwABxTu2uju4+zgQC3iveBAfGCAaPwgQO7kLOCA+i3iveBAfGCAaPwgZ0=", "base64");

const STORE = {
  current: "art-webm",
  artifacts: [
    {
      id: "art-webm",
      kind: "file",
      title: "clip.webm",
      versions: [{ code: "(binary file)", ts: 1, by: "agent", file: { filename: "clip.webm", mime: "video/webm", size: WEBM.length, thumb: "" }, blob: "art-webm.bin" }],
      version_count: 1,
      created: 1,
      updated: 1,
    },
    {
      id: "art-html",
      kind: "html",
      title: "Untrusted page",
      versions: [{ code: '<p id="ready">hi</p>', ts: 1, by: "agent" }],
      version_count: 1,
      created: 1,
      updated: 1,
    },
  ],
};

// Enter fullscreen on `selector` inside the frame, report the outcome, and leave fullscreen again.
async function tryFullscreen(frame: Frame, selector: string) {
  return frame.evaluate(async (sel) => {
    const el = document.querySelector(sel) as HTMLElement | null;
    if (!el) return "missing";
    try {
      await el.requestFullscreen();
    } catch (e) {
      return "rejected: " + String((e as Error)?.message ?? e);
    }
    const ok = document.fullscreenElement === el;
    await document.exitFullscreen().catch(() => {});
    return ok ? "entered" : "resolved without a fullscreen element";
  }, selector);
}

async function frameOf(page: Page | Frame, selector: string): Promise<Frame> {
  const handle = await page.locator(selector).elementHandle();
  const f = handle ? await handle.contentFrame() : null;
  if (!f) throw new Error(`no frame at ${selector}`);
  return f;
}

// The desktop shape: the console runs at tauri://localhost and talks to its server through
// `?__apiPort=` (lib/api/routing.ts), so every plugin frame is CROSS-ORIGIN to the console.
// Fullscreen's default allowlist is 'self', so there the frame was refused outright ("Fullscreen
// API is disabled by permissions policy"); a same-origin frame inherited it. Reproduced here by
// loading the console on `localhost` with the API on `127.0.0.1` (CORS added for the hop).
async function crossOriginConsole(page: Page, baseURL: string) {
  const api = new URL(baseURL);
  api.hostname = "127.0.0.1";
  const consoleOrigin = `http://localhost:${api.port}`;
  await page.route(`${api.origin}/**`, async (route) => {
    const req = route.request();
    const cors = {
      "access-control-allow-origin": consoleOrigin,
      "access-control-allow-headers": "*",
      "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
      "access-control-allow-credentials": "true",
    };
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    if (req.resourceType() === "document") return route.fallback();
    const res = await route.fetch();
    return route.fulfill({ response: res, headers: { ...res.headers(), ...cors } });
  });
  await page.goto(`${consoleOrigin}/app/?__apiPort=${api.port}`, { waitUntil: "load" });
}

for (const shape of ["same-origin (browser)", "cross-origin (desktop)"] as const) {
  test(`a plugin view's frame can enter fullscreen from a <video> — ${shape}`, async ({ page, baseURL }) => {
    const cross = shape === "cross-origin (desktop)";
    if (cross) await crossOriginConsole(page, baseURL!);
    else await page.goto("/app/", { waitUntil: "load" });
    const frameOrigin = await openBoardAndProbe(page);
    expect(frameOrigin !== new URL(page.url()).origin).toBe(cross);
    await page.unrouteAll({ behavior: "ignoreErrors" });
  });
}

async function openBoardAndProbe(page: Page): Promise<string> {
  await page.locator(".pl-rail").getByRole("button", { name: "Board", exact: true }).click();
  const frame = page.locator(".plugin-view-frame");
  await expect(frame).toHaveAttribute("allow", /(^|;\s*)fullscreen(;|$)/);
  const inner = await frameOf(page, ".plugin-view-frame");
  await inner.waitForLoadState();
  await inner.evaluate(() => {
    const v = document.createElement("video");
    v.id = "fs-probe";
    v.controls = true;
    document.body.appendChild(v);
  });
  expect(await tryFullscreen(inner, "#fs-probe")).toBe("entered");
  return new URL(inner.url()).origin;
}

async function openArtifactPanel(page: Page) {
  await page.route("**/api/runtime/status", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    await route.fulfill({ response: res, json: { ...body, plugins: [...(body.plugins ?? []), ARTIFACT_PLUGIN_STATUS] } });
  });
  await page.route("**/api/plugins/artifact/history", (route) => route.fulfill({ json: STORE }));
  await page.route("**/api/plugins/artifact/artifact/*/media-ticket*", (route) =>
    route.fulfill({ json: { ticket: "tk", version: 1, path: "/plugins/artifact/media/art-webm?version=1&t=tk" } }),
  );
  await page.route("**/plugins/artifact/media/**", (route) =>
    route.fulfill({ body: WEBM, contentType: "video/webm", headers: { "accept-ranges": "bytes" } }),
  );
  await page.goto("/app/", { waitUntil: "load" });
  await page.getByRole("button", { name: "Artifact", exact: true }).first().click();
  const host = page.locator('iframe[title="Artifact"]');
  await expect(host).toBeVisible();
  await expect(host).toHaveAttribute("allow", /(^|;\s*)fullscreen(;|$)/);
  return page.frameLocator('iframe[title="Artifact"]');
}

async function shellFrame(page: Page): Promise<Frame> {
  return frameOf(page, 'iframe[title="Artifact"]');
}

async function select(panel: FrameLocator, id: string) {
  await expect(panel.locator("#art option")).toHaveCount(STORE.artifacts.length);
  await panel.locator("#art").selectOption(id);
}

test("the Artifact panel's inline video enters fullscreen through both frames", async ({ page }) => {
  const panel = await openArtifactPanel(page);
  await select(panel, "art-webm");
  await expect(panel.locator("#frame")).toHaveAttribute("allow", "fullscreen *");
  await expect
    .poll(
      async () => {
        try {
          const f = await frameOf(await shellFrame(page), "#frame");
          return await f.evaluate(() => { const v = document.querySelector("video"); return !!v && v.readyState >= 1; });
        } catch {
          return false;
        }
      },
      { timeout: 20_000, message: "video metadata never loaded" },
    )
    .toBe(true);
  const nested = await frameOf(await shellFrame(page), "#frame");
  expect(await tryFullscreen(nested, "video")).toBe("entered");
});

test("model-generated HTML in the artifact frame is NOT granted fullscreen", async ({ page }) => {
  const panel = await openArtifactPanel(page);
  await select(panel, "art-webm");
  await expect(panel.locator("#frame")).toHaveAttribute("allow", "fullscreen *");
  await select(panel, "art-html");
  await expect(panel.frameLocator("#frame").locator("#ready")).toHaveCount(1);
  await expect(panel.locator("#frame")).not.toHaveAttribute("allow", /fullscreen/);
  const nested = await frameOf(await shellFrame(page), "#frame");
  expect(await tryFullscreen(nested, "#ready")).toMatch(/^rejected/);
});
