import { expect, test, type Frame, type Page } from "@playwright/test";

import { artifactFrame, routeLinksStore } from "./artifactLinks";

// The Artifact panel's navigable viewport + code-linked mermaid diagrams (ADR 0038 amendment),
// driven against the REAL plugins/artifact shell served by the mock.
//
// The shell is loaded inside a same-origin HOST page (an iframe, like the console's PluginView),
// so what it forwards to its embedder — `protoagent:code:open` — can be recorded exactly. The
// contract pinned: zoom moves the svg's viewBox (never a CSS transform — the WKWebView blur);
// a click on a linked element forwards EXACTLY the version's stored target; an unlinked element
// forwards nothing; and a forged post from the sandboxed frame can't name a path of its own.

const HOST = "/__mm-host.html";
const hostHtml = (w: number) => `<!doctype html><html><body style="margin:0">
<iframe id="shell" src="/plugins/artifact/view" style="width:${w}px;height:760px;border:0"></iframe>
<script>
  window.__opened = [];
  addEventListener("message", (e) => {
    const f = document.getElementById("shell");
    if (e.source === f.contentWindow && e.data && e.data.type === "protoagent:code:open") window.__opened.push(e.data);
  });
</script></body></html>`;

async function openHost(page: Page, width = 1000): Promise<Frame> {
  await routeLinksStore(page);
  await page.route(`**${HOST}`, (route) => route.fulfill({ contentType: "text/html", body: hostHtml(width) }));
  await page.goto(HOST);
  const shell = page.frameLocator("#shell");
  await expect(shell.locator("#art option")).toHaveCount(4);
  const h = await page.locator("#shell").elementHandle();
  const f = h ? await h.contentFrame() : null;
  if (!f) throw new Error("no shell frame");
  return f;
}

// Select an artifact and wait until its viewport controller has mounted (the toolbar exists).
async function show(shell: Frame, id: string, ready = ".__vptb"): Promise<Frame> {
  await shell.selectOption("#art", id);
  let frame: Frame | null = null;
  await expect
    .poll(
      async () => {
        try {
          frame = await artifactFrame(shell);
          return await frame.evaluate((sel) => !!document.querySelector(sel), ready);
        } catch {
          return false;
        }
      },
      { timeout: 20_000, message: `${id} never mounted ${ready}` },
    )
    .toBe(true);
  return frame as unknown as Frame;
}

const opened = (page: Page) => page.evaluate(() => (window as unknown as { __opened: unknown[] }).__opened);
const viewBox = (f: Frame) => f.evaluate(() => document.querySelector(".__vpsvg")!.getAttribute("viewBox"));

test("zoom and pan move the svg viewBox, never a CSS transform", async ({ page }) => {
  const shell = await openHost(page);
  const f = await show(shell, "art-flow");
  const vb0 = await viewBox(f);
  const box = f.locator("#__vp");
  const r = (await box.boundingBox())!;
  // Wheel zoom toward a point.
  await page.mouse.move(r.x + r.width / 2, r.y + r.height / 2);
  await page.mouse.wheel(0, -400);
  await expect.poll(() => viewBox(f)).not.toBe(vb0);
  const zoomed = (await viewBox(f))!.split(" ").map(Number);
  expect(zoomed[2]).toBeLessThan(Number(vb0!.split(" ")[2])); // a narrower window = zoomed in
  // No transform anywhere on the way down — the svg is laid out as a vector at every zoom.
  const transforms = await f.evaluate(() => {
    const svg = document.querySelector(".__vpsvg")!;
    const chain: string[] = [];
    for (let el: Element | null = svg; el; el = el.parentElement) chain.push(getComputedStyle(el).transform);
    return { chain, willChange: getComputedStyle(svg).willChange };
  });
  expect(transforms.chain.every((t) => t === "none")).toBe(true);
  expect(transforms.willChange).toBe("auto");
  // Drag pans (the viewBox origin moves; its size doesn't).
  await page.mouse.move(r.x + 200, r.y + 200);
  await page.mouse.down();
  await page.mouse.move(r.x + 300, r.y + 260, { steps: 5 });
  await page.mouse.up();
  const panned = (await viewBox(f))!.split(" ").map(Number);
  expect(panned[2]).toBeCloseTo(zoomed[2], 3);
  expect(panned[0]).not.toBeCloseTo(zoomed[0], 1);
  // Toolbar: + / − / Reset; keyboard 0 resets too.
  await f.getByRole("button", { name: "Reset to the start view (0)" }).click();
  await expect.poll(() => viewBox(f)).toBe(vb0);
  await f.getByRole("button", { name: "Zoom in (+)" }).click();
  await expect.poll(() => viewBox(f)).not.toBe(vb0);
  await box.focus();
  await page.keyboard.press("0");
  await expect.poll(() => viewBox(f)).toBe(vb0);
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => viewBox(f)).not.toBe(vb0);
  // Panning never opened anything.
  expect(await opened(page)).toEqual([]);
});

test("a big svg starts fitted to the frame; a small diagram is not blown up", async ({ page }) => {
  const shell = await openHost(page);
  const big = await show(shell, "art-big");
  const vbBig = (await viewBox(big))!.split(" ").map(Number);
  expect(vbBig[2]).toBeGreaterThanOrEqual(4000); // the whole 4000-wide drawing is in view
  expect(await big.locator(".__vpz").textContent()).not.toBe("100%");
  const small = await show(shell, "art-flow");
  expect(await small.locator(".__vpz").textContent()).toBe("100%");
});

test("clicking a linked sequence message forwards exactly its stored target", async ({ page }) => {
  const shell = await openHost(page);
  const f = await show(shell, "art-seq", ".__lk");
  // The second message spans two label lines (a <br/>) — one link either way.
  await f.locator('[data-lk="msg:2"]').first().click();
  await expect.poll(() => opened(page)).toHaveLength(1);
  expect((await opened(page))[0]).toEqual({
    type: "protoagent:code:open",
    project: "demo",
    path: "src/tools.py",
    line: 40,
    end_line: 44,
    note: "the tool loop",
  });
  // A participant by display alias; the keyboard path (Enter) opens too.
  await f.locator('[data-lk="participant:Server"]').first().focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => opened(page)).toHaveLength(2);
  expect((await opened(page))[1]).toMatchObject({ path: "src/server.py", line: 3 });
});

test("an unlinked element opens nothing; hover shows the target as text", async ({ page }) => {
  const shell = await openHost(page);
  const f = await show(shell, "art-seq", ".__lk");
  // msg:3 is linked by LABEL (msg:answer) — its note carries markup that must stay text.
  const answer = f.locator('[data-lk="msg:answer"]').first();
  await answer.hover();
  const tip = f.locator(".__lktip");
  await expect(tip).toBeVisible();
  await expect(tip).toContainText('src/agent.py:30');
  await expect(tip).toContainText('<img src=x onerror="window.__xss=1">');
  expect(await f.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
  // The "User" participant has no link: a click is inert.
  const user = f.locator('rect.actor[name="U"]').first();
  await user.click({ position: { x: 10, y: 10 } });
  await page.waitForTimeout(300);
  expect(await opened(page)).toEqual([]);
});

test("a forged post from the artifact frame can't open a path outside the stored links", async ({ page }) => {
  test.setTimeout(60_000);
  const shell = await openHost(page);
  const f = await show(shell, "art-seq", ".__lk");
  // The frame can't bypass the shell by posting the host's own message type to the top window.
  await f.evaluate(() =>
    top!.postMessage({ type: "protoagent:code:open", project: "demo", path: ".env", line: 1 }, "*"),
  );
  // No gesture behind it (a script firing on its own) → nothing, even for a valid key. Playwright
  // runs evaluate() AS a user gesture, so the post is scheduled past the ~5s transient-activation
  // window that gesture opens.
  await f.evaluate(() => {
    setTimeout(() => parent.postMessage({ type: "protoArtifact:openCode", key: "msg:1" }, "*"), 6000);
  });
  await page.waitForTimeout(6600);
  expect(await opened(page)).toEqual([]);
  // Model-authored code in the sandbox posts on a REAL user gesture (so the activation gate
  // passes): an unknown key, then a known key dressed up with a path of its own.
  await f.evaluate(() => {
    const b = document.createElement("button");
    b.id = "forge";
    b.textContent = "forge";
    b.style.cssText = "position:fixed;left:4px;top:4px;z-index:2147483647";
    b.onclick = () => {
      parent.postMessage({ type: "protoArtifact:openCode", key: "nope" }, "*");
      parent.postMessage(
        { type: "protoArtifact:openCode", key: "msg:1", project: "demo", path: "/etc/passwd", line: 1 },
        "*",
      );
    };
    document.body.appendChild(b);
  });
  await f.locator("#forge").click();
  await expect.poll(() => opened(page)).toHaveLength(1);
  // Only the known key opened, and with its STORED target — the payload's path was ignored.
  expect((await opened(page))[0]).toMatchObject({ project: "demo", path: "src/agent.py", line: 12, end_line: 20 });
  await page.waitForTimeout(400);
  expect(await opened(page)).toHaveLength(1);
});

test("the Links list is keyboard-reachable, text-only, and opens the stored target", async ({ page }) => {
  const shell = await openHost(page);
  await show(shell, "art-seq", ".__lk");
  const btn = shell.locator("#links");
  await expect(btn).toHaveText("Links 5");
  await expect(btn).toHaveAttribute("aria-label", "Code links (5)");
  await btn.click();
  const list = shell.locator("#linklist");
  await expect(list.locator("button")).toHaveCount(5);
  // msg:N sorts first, in message order; a key that matched nothing says so.
  await expect(list.locator("button").first()).toContainText("1. ask(question)");
  await expect(list.locator('[data-key="ghost-node"]')).toContainText("not found in the diagram");
  // The markup-bearing note is text in the shell page too (same-origin with the console!).
  await expect(list.locator('[data-key="msg:answer"]')).toContainText("<img src=x");
  expect(await shell.evaluate(() => document.querySelectorAll("#linklist img").length)).toBe(0);
  // Keyboard: focus lands in the list, ArrowDown moves, Enter opens, Escape closes.
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect.poll(() => opened(page)).toHaveLength(1);
  expect((await opened(page))[0]).toMatchObject({ path: "src/tools.py", line: 40 });
  await page.keyboard.press("Escape");
  await expect(shell.locator("#linkpanel")).toBeHidden();
  // v1 of the same artifact carries no links: the button goes away with it, and comes back.
  await shell.locator("#vprev").click();
  await expect(btn).toBeHidden();
  await shell.locator("#vnext").click();
  await expect(btn).toBeVisible();
});

test("mermaid inside markdown gets an inline zoomable box that doesn't hijack page scroll", async ({ page }) => {
  const shell = await openHost(page);
  const f = await show(shell, "art-md", ".__vpbox .__vptb");
  const vb0 = await f.evaluate(() => document.querySelector(".__vpbox svg")!.getAttribute("viewBox"));
  const r = (await f.locator(".__vpbox").boundingBox())!;
  await page.mouse.move(r.x + r.width / 2, r.y + r.height / 2);
  await page.mouse.wheel(0, -300); // plain wheel: page scroll, not zoom
  await page.waitForTimeout(150);
  expect(await f.evaluate(() => document.querySelector(".__vpbox svg")!.getAttribute("viewBox"))).toBe(vb0);
  await f.getByRole("button", { name: "Zoom in (+)" }).click();
  await expect
    .poll(() => f.evaluate(() => document.querySelector(".__vpbox svg")!.getAttribute("viewBox")))
    .not.toBe(vb0);
});

test("a broken mermaid diagram still reports its render error (the feedback loop is intact)", async ({ page }) => {
  const posts: string[] = [];
  await page.route("**/api/plugins/artifact/history", (route) =>
    route.fulfill({
      json: {
        current: "bad",
        artifacts: [
          {
            id: "bad",
            kind: "mermaid",
            title: "bad",
            version_count: 1,
            versions: [
              {
                code: "flowchart TD\n  A --> (",
                ts: 1,
                by: "agent",
                links: { A: { project: "demo", path: "a.py", line: 1 } },
              },
            ],
          },
        ],
      },
    }),
  );
  await page.route("**/api/plugins/artifact/render-status", async (route) => {
    posts.push(route.request().postData() || "");
    await route.fulfill({ json: { ok: true, recorded: true } });
  });
  await page.goto("/plugins/artifact/view");
  const errText = async () => {
    try {
      return await (await artifactFrame(page)).evaluate(() => document.getElementById("__arterr")?.textContent ?? "");
    } catch {
      return "";
    }
  };
  await expect.poll(errText, { timeout: 20_000 }).toContain("Parse error");
  await expect.poll(() => posts.length).toBeGreaterThan(0);
  expect(JSON.parse(posts[0])).toMatchObject({ id: "bad", ok: false });
});

test("a narrow dock: the diagram opens readable at the top, and the toolbar stays one clean row", async ({ page }) => {
  const shell = await openHost(page, 480);
  const f = await show(shell, "art-seq", ".__lk");
  // Labels render at a readable size (fit-to-contain would shrink them), from the top-left,
  // below the zoom toolbar rather than under it.
  const first = await f.evaluate(() => {
    const t = document.querySelector("text.messageText")!.getBoundingClientRect();
    const a = [...document.querySelectorAll("rect.actor")].map((r) => r.getBoundingClientRect());
    const top = a.reduce((m, r) => (r.top < m.top ? r : m));
    return { fontPx: t.height, actorTop: top.top, actorLeft: top.left, vh: innerHeight };
  });
  expect(first.fontPx).toBeGreaterThanOrEqual(11);
  const tb = await f.evaluate(() => document.querySelector(".__vptb")!.getBoundingClientRect().bottom);
  expect(first.actorTop).toBeGreaterThanOrEqual(tb);
  expect(first.actorTop).toBeLessThan(first.vh / 3);
  expect(first.actorLeft).toBeGreaterThanOrEqual(0);
  // Fit still shows the whole diagram (smaller), Reset returns to the readable start.
  const start = await viewBox(f);
  await f.getByRole("button", { name: "Fit the whole diagram (f)" }).click();
  await expect.poll(() => viewBox(f)).not.toBe(start);
  await f.getByRole("button", { name: "Reset to the start view (0)" }).click();
  await expect.poll(() => viewBox(f)).toBe(start);
  // Toolbar: one row; Edit / Download / Delete fold into the ⋯ menu and still work.
  const bar = shell.locator("#bar");
  await expect(bar).toHaveClass(/compact/);
  const rows = await shell.evaluate(() => {
    const tops = new Set([...document.querySelectorAll("#bar > *")]
      .filter((e) => (e as HTMLElement).offsetParent !== null)
      .map((e) => { const r = e.getBoundingClientRect(); return Math.round((r.top + r.bottom) / 4); }));
    const links = document.getElementById("links")!.getBoundingClientRect();
    return { rows: tops.size, linksH: links.height };
  });
  expect(rows.rows).toBe(1);
  expect(rows.linksH).toBeLessThan(34); // "Links 5" on one line
  await expect(shell.locator("#dl")).toBeHidden();
  await shell.locator("#more").click();
  await expect(shell.locator("#moremenu #dl")).toBeVisible();
  await shell.locator("#moremenu #del").click();
  await expect(shell.locator("#moremenu #del")).toHaveText("Confirm?"); // menu stays open to confirm
  await page.keyboard.press("Escape");
  await expect(shell.locator("#moremenu")).toBeHidden();
});
