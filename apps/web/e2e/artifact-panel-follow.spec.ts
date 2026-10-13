import { expect, test, type Page } from "@playwright/test";

// The Artifact panel against a LIVE-changing store (#4076, ADR 0118), on the REAL shell.js:
//   • follow-newest follows panel-placed artifacts only — an inline answer (D2: no side effect on
//     the panel), including one another session made, becomes the store's `current` but must not
//     move the panel; the next panel artifact is still followed;
//   • a live theme switch flips the frame's color-scheme (native controls, scrollbars), not only
//     its tokens, and the shell page's own color-scheme follows the console, not the OS;
//   • a send() refusal the artifact doesn't catch is not reported as an uncaught rejection.

const VIEW = "/plugins/artifact/view";
const DARK = "#0a0a0c", LIGHT = "#f6f7f9";

type Art = { id: string; kind: string; title: string; placement?: string; versions: { code: string; ts: number; by: string }[] };
const art = (id: string, kind: string, code: string, ts: number, inline = false): Art => ({
  id, kind, title: id, ...(inline ? { placement: "inline" } : {}), versions: [{ code, ts, by: "agent" }],
});
const doc = (id: string, ts: number) => art(id, "markdown", `# ${id}`, ts);
const svg = (id: string, ts: number) =>
  art(id, "svg", `<svg id="${id}" xmlns="http://www.w3.org/2000/svg" width="40" height="40"/>`, ts, true);

// A mutable /history the test advances, like the agent writing to the store between polls.
async function liveStore(page: Page, first: { artifacts: Art[]; current: string }) {
  const store = { ...first };
  let n = 0;
  await page.route("**/api/plugins/artifact/history", (route) =>
    route.fulfill({ json: store, headers: { ETag: `W/"s${n}"` } }),
  );
  return (next: { artifacts: Art[]; current: string }) => { Object.assign(store, next); n++; };
}

const selected = (page: Page) => page.locator("#art");

test("an inline answer — or another session's — never moves the panel; a panel artifact does", async ({ page }) => {
  const a = doc("doc-a", 100);
  const set = await liveStore(page, { artifacts: [a], current: "doc-a" });
  await page.goto(VIEW);
  await expect(selected(page)).toHaveValue("doc-a");

  // The same chat's inline svg answer becomes the store's current.
  const s = svg("svg-inline", 200);
  set({ artifacts: [s, a], current: "svg-inline" });
  await expect(page.locator("#art option")).toHaveCount(2);
  // Two more polls' worth: the panel must still be on the document.
  await page.waitForTimeout(3500);
  await expect(selected(page)).toHaveValue("doc-a");

  // Another session's (e.g. /v1) inline html answer.
  const api = art("api-inline", "html", "<p>from /v1</p>", 300, true);
  set({ artifacts: [api, s, a], current: "api-inline" });
  await expect(page.locator("#art option")).toHaveCount(3);
  await page.waitForTimeout(3500);
  await expect(selected(page)).toHaveValue("doc-a");

  // A new PANEL artifact is still followed.
  const b = doc("doc-b", 400);
  set({ artifacts: [b, api, s, a], current: "doc-b" });
  await expect(selected(page)).toHaveValue("doc-b");
});

async function innerScheme(page: Page): Promise<string | null> {
  const h = await page.locator("#frame").elementHandle();
  const f = h && (await h.contentFrame());
  if (!f) return null;
  try {
    return await f.evaluate(() => getComputedStyle(document.documentElement).colorScheme);
  } catch {
    return null;
  }
}

test("a live theme switch flips color-scheme in the frame and on the shell page", async ({ page }) => {
  await liveStore(page, {
    artifacts: [art("form", "html", '<input id="d" type="date"><p id="ok">ok</p>', 100)],
    current: "form",
  });
  await page.emulateMedia({ colorScheme: "light" }); // the OS says light …
  await page.goto(VIEW);
  const theme = (bg: string) =>
    page.evaluate((v) => document.documentElement.style.setProperty("--pl-color-bg", v), bg);
  await theme(LIGHT);
  await expect.poll(() => innerScheme(page)).toBe("light"); // rendered light first
  await theme(DARK); // … then the console switches to dark LIVE (the OS still says light)
  await expect.poll(() => innerScheme(page)).toBe("dark");
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe("dark");

  await theme(LIGHT);
  await expect.poll(() => innerScheme(page)).toBe("light");
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe("light");

  await theme(DARK);
  await expect.poll(() => innerScheme(page)).toBe("dark");
});

test("an uncaught send() refusal is not reported as an uncaught rejection", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  const infos: string[] = [];
  page.on("console", (m) => { if (m.type() === "info") infos.push(m.text()); });
  await liveStore(page, {
    artifacts: [art("sender", "html", '<p id="ok">ok</p><script>window.protoArtifact.send("hi");</script>', 100)],
    current: "sender",
  });
  await page.goto(VIEW); // standalone: no console host, so the shell refuses the send locally
  await expect.poll(() => infos.some((t) => t.includes("protoArtifact: not sent"))).toBe(true);
  await page.waitForTimeout(300);
  expect(errors.filter((t) => /not sent|unavailable here|Uncaught/i.test(t))).toEqual([]);
});

// ── #4076 follow-up: the panel belongs to ONE chat ──────────────────────────────────────────
// The console posts `protoArtifact:context {session}` (the active chat tab) into the panel; the
// panel then follows only the panel artifacts that chat made or touched (`chat_sessions`). Driven
// here through a host page that EMBEDS the real shell, since the shell trusts the context only
// from its embedder.

type Stamped = Art & { chat_sessions?: string[] };
const stamped = (id: string, ts: number, chats?: string[]): Stamped => ({
  ...doc(id, ts), ...(chats ? { chat_sessions: chats } : {}),
});

async function embeddedPanel(page: Page) {
  await page.route("**/e2e-panel-host", (route) =>
    route.fulfill({ contentType: "text/html", body: `<!doctype html><iframe id="p" src="${VIEW}" style="width:800px;height:600px"></iframe>` }),
  );
  await page.goto("/e2e-panel-host");
  const panel = page.frameLocator("#p");
  const context = (session: string) =>
    page.evaluate((s) => (document.getElementById("p") as HTMLIFrameElement).contentWindow!.postMessage(
      { type: "protoArtifact:context", session: s }, "*"), session);
  return { sel: panel.locator("#art"), options: panel.locator("#art option"), context };
}

test("another chat's — or a /v1, A2A, legacy — panel artifact never takes over the panel", async ({ page }) => {
  const a = stamped("doc-a", 100, ["chat-a"]);
  const set = await liveStore(page, { artifacts: [a], current: "doc-a" });
  const p = await embeddedPanel(page);
  await expect(p.sel).toHaveValue("doc-a");
  await p.context("chat-a");

  // Chat B (another tab) writes a panel doc: the store's current, not this panel's.
  const b = stamped("doc-b", 200, ["chat-b"]);
  set({ artifacts: [b, a], current: "doc-b" });
  await expect(p.options).toHaveCount(2); // it is in the picker …
  await page.waitForTimeout(3500);
  await expect(p.sel).toHaveValue("doc-a"); // … but the panel stays on chat A's doc

  // A /v1 / A2A / ACP client (no console chat → no chat_sessions), and a pre-stamp record.
  const api = stamped("doc-api", 300);
  set({ artifacts: [api, b, a], current: "doc-api" });
  await expect(p.options).toHaveCount(3);
  await page.waitForTimeout(3500);
  await expect(p.sel).toHaveValue("doc-a");

  // Chat A's next panel artifact is followed.
  const a2 = stamped("doc-a2", 400, ["chat-a"]);
  set({ artifacts: [a2, api, b, a], current: "doc-a2" });
  await expect(p.sel).toHaveValue("doc-a2");

  // A tab switch: the panel follows chat B's newest panel artifact, and back again.
  await p.context("chat-b");
  await expect(p.sel).toHaveValue("doc-b");
  await p.context("chat-a");
  await expect(p.sel).toHaveValue("doc-a2");
});

test("a context posted by the artifact's own frame is ignored", async ({ page }) => {
  // Chat A's newest panel artifact is model code that claims to be in chat X.
  const evil: Stamped = {
    ...art("evil", "html", '<p id="ok">ok</p><script>setInterval(function(){parent.postMessage({type:"protoArtifact:context",session:"chat-x"},"*")},100)</script>', 100),
    chat_sessions: ["chat-a"],
  };
  const set = await liveStore(page, { artifacts: [evil], current: "evil" });
  const p = await embeddedPanel(page);
  await expect(p.sel).toHaveValue("evil"); // booted (listening) before the context is posted
  await p.context("chat-a");
  await expect(page.frameLocator("#p").frameLocator("#frame").locator("#ok")).toHaveText("ok");
  // Chat X's doc arrives: had the frame's claim been believed, the panel would jump to it.
  set({ artifacts: [stamped("doc-x", 300, ["chat-x"]), evil], current: "doc-x" });
  await expect(p.options).toHaveCount(2);
  await page.waitForTimeout(3500);
  await expect(p.sel).toHaveValue("evil");
});

for (const moduleUnavailable of [false, true]) {
  test(`artifact controls retain spacing and author overrides with embed module ${moduleUnavailable ? "unavailable" : "available"}`, async ({ page }) => {
    let moduleBlocked = false;
    if (moduleUnavailable) {
      await page.route("**/plugins/artifact/vendor/pl-design-embed.mjs", (route) => {
        moduleBlocked = true;
        return route.abort();
      });
    }
    await liveStore(page, {
      artifacts: [art("controls", "html", '<style>body{font-size:16px}#custom{padding:3px}</style><input id="field"><button id="plain">Plain</button><button id="custom">Custom</button>', 100)],
      current: "controls",
    });
    await page.goto(VIEW);
    const frame = page.frameLocator("#frame");
    await expect(frame.locator("#field")).toHaveCSS("padding", "6px 8px");
    await expect(frame.locator("#plain")).toHaveCSS("padding", "6px 12px");
    const lineHeight = moduleUnavailable ? "1.5" : await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue("--pl-font-line-height-body").trim());
    await expect(frame.locator("body")).toHaveCSS("line-height", `${16 * Number(lineHeight)}px`);
    if (moduleUnavailable) expect(moduleBlocked).toBe(true);
    await expect(frame.locator("#custom")).toHaveCSS("padding", "3px");
    if (!moduleUnavailable) {
      await page.evaluate(() => document.documentElement.style.setProperty("--pl-space-1_5", "11px"));
      await expect(frame.locator("#plain")).toHaveCSS("padding", "11px 12px");
      await expect(frame.locator("#custom")).toHaveCSS("padding", "3px");
    }
  });
}
