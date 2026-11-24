import { expect, test, type BrowserContext, type Page, type Worker } from "@playwright/test";

// The installable console (ADR 0135 W2): the web manifest and the push worker under /app/.
//
// Nothing in the app registers the worker yet, so each test registers it itself. A push is
// delivered through the browser (CDP `ServiceWorker.deliverPushMessage`), so the worker's real
// `push` listener runs. Headless Chromium grants no notification permission, so what the worker
// SHOWS is recorded at `registration.showNotification`; a click is handed to the worker's own
// `notificationclick` handler, with focusing and opening a window recorded the same way (a
// browser only allows either from a real click).

const NOTICE = {
  v: 1,
  id: "ntc_7Qm2x",
  kind: "question",
  importance: "high",
  created_at: "2026-11-18T12:00:00Z",
  expires_at: "2026-11-19T12:00:00Z",
  title: "protoAgent needs your attention",
  body: "Open the console to view a decision",
  route: "/app/?notice=ntc_7Qm2x",
};
const FALLBACK = { title: "protoAgent needs your attention", body: "Open the console to view an update", tag: "protoagent-notice" };

type Shown = { title: string; body: string; tag: string; data: { id: string; route: string } };
type Opened = { focused: string[]; opened: string[]; messages: Array<{ type: string; id: string; route: string }> };

/** Register /app/sw.js from `page`, record what it shows, and return a way to push to it. */
async function startWorker(page: Page, context: BrowserContext, path = "/app/") {
  // Any request the WORKER makes is a failure: it must never call the hub.
  const workerRequests: string[] = [];
  context.on("request", (r) => {
    if (r.serviceWorker() && !r.url().endsWith("/app/sw.js")) workerRequests.push(r.url());
  });
  await page.goto(path);
  const cdp = await context.newCDPSession(page);
  const registrations: Array<{ registrationId: string }> = [];
  cdp.on("ServiceWorker.workerRegistrationUpdated", (e) => registrations.push(...e.registrations));
  await cdp.send("ServiceWorker.enable");
  const scope = await page.evaluate(async () => {
    const reg = await navigator.serviceWorker.register("/app/sw.js");
    await navigator.serviceWorker.ready;
    return new URL(reg.scope).pathname;
  });
  expect(scope).toBe("/app/");
  const worker: Worker = context.serviceWorkers()[0] ?? (await context.waitForEvent("serviceworker"));
  await expect.poll(() => registrations.length).toBeGreaterThan(0);
  await worker.evaluate(() => {
    type Open = { tag: string; close: () => void };
    const g = self as unknown as {
      __shown: unknown[];
      __open: Open[];
      __queried: string[];
      registration: { showNotification: unknown; getNotifications: unknown };
    };
    g.__shown = [];
    // The notifications still on screen: a repeat tag replaces, `close()` removes.
    g.__open = [];
    g.__queried = [];
    g.registration.showNotification = (title: string, o: Record<string, unknown>) => {
      g.__shown.push({ title, body: o.body, tag: o.tag, data: o.data });
      const tag = String(o.tag);
      const open: Open = { tag, close: () => void (g.__open = g.__open.filter((n) => n !== open)) };
      g.__open = [...g.__open.filter((n) => n.tag !== tag), open];
      return Promise.resolve();
    };
    g.registration.getNotifications = (filter?: { tag?: string }) => {
      g.__queried.push(filter?.tag ?? "");
      return Promise.resolve(g.__open.filter((n) => !filter?.tag || n.tag === filter.tag));
    };
  });
  const shown = () => worker.evaluate(() => (self as unknown as { __shown: Shown[] }).__shown);
  const onScreen = () => worker.evaluate(() => (self as unknown as { __open: Array<{ tag: string }> }).__open.map((n) => n.tag));
  const queried = () => worker.evaluate(() => (self as unknown as { __queried: string[] }).__queried);
  const push = async (data: string) => {
    const before = (await shown()).length;
    const origin = new URL(page.url()).origin;
    await cdp.send("ServiceWorker.deliverPushMessage", { origin, registrationId: registrations[0].registrationId, data });
    await expect.poll(async () => (await shown()).length).toBe(before + 1);
    return (await shown())[before];
  };
  /** Deliver a close push and wait until the worker has looked its tag up. */
  const pushClose = async (data: string) => {
    const before = (await queried()).length;
    const origin = new URL(page.url()).origin;
    await cdp.send("ServiceWorker.deliverPushMessage", { origin, registrationId: registrations[0].registrationId, data });
    await expect.poll(async () => (await queried()).length).toBe(before + 1);
  };
  return { worker, push, pushClose, shown, onScreen, queried, workerRequests };
}

/** Click a notification carrying `data`; returns what the worker focused, messaged and opened. */
function click(worker: Worker, data: unknown): Promise<Opened & { closed: boolean }> {
  return worker.evaluate(async (noticeData) => {
    const g = self as unknown as {
      clients: { openWindow: (url: string) => Promise<null> };
      WindowClient: { prototype: { focus: () => Promise<unknown>; postMessage: (m: unknown) => void } };
      onNotificationClick: (event: unknown) => void;
    };
    const out = { focused: [] as string[], opened: [] as string[], messages: [] as never[], closed: false };
    g.clients.openWindow = (url) => (out.opened.push(url), Promise.resolve(null));
    g.WindowClient.prototype.focus = function (this: { url: string }) {
      out.focused.push(this.url);
      return Promise.resolve(this);
    };
    g.WindowClient.prototype.postMessage = (m) => void out.messages.push(m as never);
    let done: Promise<unknown> = Promise.resolve();
    g.onNotificationClick({
      notification: { data: noticeData, close: () => (out.closed = true) },
      waitUntil: (p: Promise<unknown>) => (done = p),
    });
    await done;
    return out;
  }, data);
}

test("the manifest and the worker are served under /app/ without credentials", async ({ page, request }) => {
  const manifest = await request.get("/app/manifest.webmanifest");
  expect(manifest.status()).toBe(200);
  expect(manifest.headers()["content-type"]).toContain("application/manifest+json");
  const doc = await manifest.json();
  expect(doc).toMatchObject({ id: "/app/", start_url: "/app/", scope: "/app/", display: "standalone" });
  expect(doc.icons.map((i: { sizes: string }) => i.sizes)).toEqual(["192x192", "512x512"]);
  for (const icon of [...doc.icons.map((i: { src: string }) => i.src), "icons/apple-touch-icon.png"]) {
    const png = await request.get(`/app/${icon}`);
    expect(png.headers()["content-type"], icon).toBe("image/png");
  }

  const worker = await request.get("/app/sw.js");
  expect(worker.status()).toBe(200);
  expect(worker.headers()["content-type"]).toContain("javascript");
  expect(worker.headers()["cache-control"]).toBe("no-cache");
  // `push` and `notificationclick` only — no `fetch` listener, so nothing is intercepted or cached.
  const listeners = [...(await worker.text()).matchAll(/addEventListener\("([a-z]+)"/g)].map((m) => m[1]);
  expect(listeners.sort()).toEqual(["notificationclick", "push"]);

  await page.goto("/app/");
  await expect(page.locator('link[rel="manifest"]')).toHaveAttribute("href", "/app/manifest.webmanifest");
  await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveAttribute("href", "/app/icons/apple-touch-icon.png");
  await expect(page.locator('meta[name="apple-mobile-web-app-capable"]')).toHaveAttribute("content", "yes");
});

test("a valid push shows one generic notification tagged with its notice id", async ({ page, context }) => {
  const { push, shown, workerRequests } = await startWorker(page, context);
  const first = await push(JSON.stringify(NOTICE));
  expect(first).toEqual({
    title: NOTICE.title,
    body: NOTICE.body,
    tag: NOTICE.id,
    data: { v: 1, id: NOTICE.id, route: "/app/?notice=ntc_7Qm2x" },
  });
  // A repeat delivery carries the same tag, so the browser replaces the first notification.
  expect((await push(JSON.stringify(NOTICE))).tag).toBe(NOTICE.id);
  // A destination that is not a known console route is dropped; the notice is still shown.
  for (const route of ["https://evil.example/app/", "//evil.example/app/", "/api/config", "/app/../api/config", "/app/?notice=other", "/app/?next=https://evil.example"]) {
    expect((await push(JSON.stringify({ ...NOTICE, route }))).data.route, route).toBe("/app/");
  }
  expect((await push(JSON.stringify({ ...NOTICE, route: "/app/agent/ava-12/?notice=ntc_7Qm2x" }))).data.route).toBe(
    "/app/agent/ava-12/?notice=ntc_7Qm2x",
  );
  expect(await shown()).toHaveLength(9);
  expect(workerRequests).toEqual([]);
  // The worker stores nothing: no caches, and it never asked for one.
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
});

test("a malformed or unknown push shows the generic fallback", async ({ page, context }) => {
  const { push, workerRequests } = await startWorker(page, context);
  const malformed = [
    "not json",
    "[]",
    JSON.stringify({ ...NOTICE, v: 2 }),
    // Build the hostile ID as data so the CI dependency scanner does not read it as a file import.
    JSON.stringify({ ...NOTICE, id: ["..", "..", "etc"].join("/") }),
    JSON.stringify({ ...NOTICE, kind: "run_command" }),
    JSON.stringify({ ...NOTICE, importance: "apocalyptic" }),
    JSON.stringify({ ...NOTICE, title: "x".repeat(500) }),
    JSON.stringify({ ...NOTICE, body: 42 }),
    JSON.stringify({ ...NOTICE, body: "y".repeat(5000) }),
  ];
  for (const data of malformed) {
    expect(await push(data), data.slice(0, 60)).toEqual({ ...FALLBACK, data: { v: 1, id: "", route: "/app/" } });
  }
  expect(workerRequests).toEqual([]);
});

test("a close push closes its tag's notification and shows none", async ({ page, context }) => {
  const { push, pushClose, shown, onScreen, queried, workerRequests } = await startWorker(page, context);
  const decision = { ...NOTICE, id: "decision-dcn_42", route: "/app/?notice=decision-dcn_42" };
  expect((await push(JSON.stringify(decision))).tag).toBe("decision-dcn_42");
  expect((await push(JSON.stringify(NOTICE))).tag).toBe(NOTICE.id);
  expect(await onScreen()).toEqual(["decision-dcn_42", NOTICE.id]);

  // A close for another tag leaves the decision's notification in place.
  await pushClose(JSON.stringify({ v: 1, type: "close", tag: "decision-other" }));
  expect(await onScreen()).toEqual(["decision-dcn_42", NOTICE.id]);

  // A close for its tag closes it, and nothing new is shown.
  await pushClose(JSON.stringify({ v: 1, type: "close", tag: "decision-dcn_42" }));
  expect(await onScreen()).toEqual([NOTICE.id]);
  expect(await queried()).toEqual(["decision-other", "decision-dcn_42"]);
  expect(await shown()).toHaveLength(2);
  expect(workerRequests).toEqual([]);
});

test("a malformed close push shows the generic fallback and closes nothing", async ({ page, context }) => {
  const { push, onScreen, queried } = await startWorker(page, context);
  const decision = { ...NOTICE, id: "decision-dcn_42", route: "/app/?notice=decision-dcn_42" };
  await push(JSON.stringify(decision));
  const malformed = [
    JSON.stringify({ v: 1, type: "close" }),
    JSON.stringify({ v: 1, type: "close", tag: "" }),
    JSON.stringify({ v: 1, type: "close", tag: 42 }),
    JSON.stringify({ v: 1, type: "close", tag: ["..", "..", "etc"].join("/") }),
    JSON.stringify({ v: 2, type: "close", tag: "decision-dcn_42" }),
    JSON.stringify({ v: 1, type: "dismiss", tag: "decision-dcn_42" }),
    JSON.stringify({ v: 1, type: "close", tag: "decision-dcn_42", body: "Deploy prod-db now?" }),
  ];
  for (const data of malformed) {
    expect(await push(data), data).toEqual({ ...FALLBACK, data: { v: 1, id: "", route: "/app/" } });
  }
  expect(await queried()).toEqual([]);
  expect(await onScreen()).toEqual(["decision-dcn_42", FALLBACK.tag]);
});

test("a click focuses the open console window, or opens a known console route", async ({ page, context, baseURL }) => {
  const { worker } = await startWorker(page, context);
  const origin = new URL(baseURL!).origin;

  // The console is open on the notice's page: focus it and tell it which notice was clicked.
  const focused = await click(worker, { v: 1, id: NOTICE.id, route: NOTICE.route });
  expect(focused.closed).toBe(true);
  expect(focused.focused).toEqual([`${origin}/app/`]);
  expect(focused.messages).toEqual([{ type: "protoagent:notice-click", v: 1, id: NOTICE.id, route: NOTICE.route }]);
  expect(focused.opened).toEqual([]);

  // No window on that page, but the console is open on another: focus that one and tell it.
  const route = `/app/agent/ava-12/?notice=${NOTICE.id}`;
  const other = await click(worker, { v: 1, id: NOTICE.id, route });
  expect(other.focused).toEqual([`${origin}/app/`]);
  expect(other.messages).toEqual([{ type: "protoagent:notice-click", v: 1, id: NOTICE.id, route }]);
  expect(other.opened).toEqual([]);

  // Any other destination is ignored: the click lands on the console and nowhere else.
  const hostile = [
    "https://evil.example/",
    "//evil.example/app/",
    "javascript:alert(1)",
    "/api/config",
    "/app/../a2a",
    "/app/agent/ava-12/../../../api/config",
    `/app/?notice=${NOTICE.id}&next=https://evil.example`,
    `/app/#/x`,
  ];
  for (const bad of hostile) {
    const result = await click(worker, { v: 1, id: NOTICE.id, route: bad });
    expect(result.focused, bad).toEqual([`${origin}/app/`]);
    expect(result.opened, bad).toEqual([]);
    expect(result.messages[0].route, bad).toBe("/app/");
  }
  for (const data of [null, "text", { route: 7 }]) {
    expect((await click(worker, data)).focused).toEqual([`${origin}/app/`]);
  }
});

test("a click while the console is open on another page focuses that window instead of opening a second", async ({ page, context, baseURL }) => {
  // The operator's phone tap log (#5664): one console open on the member, a tap on a notice
  // routed to /app/, and the worker opened a second console beside it.
  const { worker } = await startWorker(page, context, "/app/agent/member-x/");
  const origin = new URL(baseURL!).origin;
  const route = "/app/?notice=spend-cap-1";
  const tapped = await click(worker, { v: 1, id: "spend-cap-1", route });
  expect(tapped.focused).toEqual([`${origin}/app/agent/member-x/`]);
  expect(tapped.messages).toEqual([{ type: "protoagent:notice-click", v: 1, id: "spend-cap-1", route }]);
  expect(tapped.opened).toEqual([]);
  const steps = await page.evaluate(async () => {
    const hit = await (await caches.open("protoagent-notice-trace")).match("/app/__notice-trace");
    return ((await hit!.json()) as Array<{ who: string; step: string; detail: string }>).map((r) => `${r.who} ${r.step}: ${r.detail}`);
  });
  expect(steps).toContain("worker message: sent to /app/agent/member-x/ (other page)");

  // A window on the route's exact page is still preferred over any other console window.
  const second = await context.newPage();
  await second.goto("/app/");
  const exact = await click(worker, { v: 1, id: "spend-cap-1", route });
  expect(exact.focused).toEqual([`${origin}/app/`]);
  expect(exact.opened).toEqual([]);
  await second.close();

  // A same-origin window that is not a console is never reused: the route opens.
  await page.goto("/app/manifest.webmanifest");
  const notConsole = await click(worker, { v: 1, id: "spend-cap-1", route });
  expect(notConsole.focused).toEqual([]);
  expect(notConsole.messages).toEqual([]);
  expect(notConsole.opened).toEqual([`${origin}${route}`]);

  // No window of this origin at all: the route opens.
  await page.goto("about:blank");
  const none = await click(worker, { v: 1, id: "spend-cap-1", route });
  expect(none.focused).toEqual([]);
  expect(none.opened).toEqual([`${origin}${route}`]);
});

test("a click leaves the notice id where a console that got no URL and no message will find it", async ({ page, context }) => {
  // An installed iOS console can come to the front on the page it was left on: the route is
  // not followed and the message is not delivered. The worker's note is the third way in.
  const { worker } = await startWorker(page, context);
  const before = Date.now();
  await click(worker, { v: 1, id: NOTICE.id, route: NOTICE.route });
  const left = await page.evaluate(async () => {
    const hit = await (await caches.open("protoagent-notice-click")).match("/app/__notice-click");
    return hit ? await hit.json() : null;
  });
  expect(left).toMatchObject({ v: 1, id: NOTICE.id });
  expect(left.at).toBeGreaterThanOrEqual(before);
  // Nothing is left for a click with no usable id.
  await page.evaluate(() => caches.delete("protoagent-notice-click"));
  await click(worker, { v: 1, id: "../nope", route: "/app/" });
  // Only the tap trace remains: what the worker did with each tap, for Settings > Devices.
  expect(await page.evaluate(() => caches.keys())).toEqual(["protoagent-notice-trace"]);
  const steps = await page.evaluate(async () => {
    const hit = await (await caches.open("protoagent-notice-trace")).match("/app/__notice-trace");
    return ((await hit!.json()) as Array<{ who: string; step: string; detail: string }>).map((r) => `${r.who} ${r.step}: ${r.detail}`);
  });
  expect(steps[0]).toBe(`worker tap: ${NOTICE.id} → ${NOTICE.route}`);
  expect(steps).toContain("worker note: written");
  expect(steps).toContain("worker note: not written: no usable id");
  expect(steps.some((line) => line.startsWith("worker windows: "))).toBe(true);
  expect(steps.join("\n")).not.toMatch(/token|endpoint|Bearer/i);
});
