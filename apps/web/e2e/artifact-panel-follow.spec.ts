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
