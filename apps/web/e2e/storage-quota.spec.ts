import { randomUUID } from "node:crypto";
import { expect, test as base, type Request } from "@playwright/test";

// Keep every error: these records diagnose failures, never allowlist them.
const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    const scope = randomUUID();
    await page.setExtraHTTPHeaders({ "x-e2e-lifecycle": scope });
    const events: object[] = [];
    const ids = new Map<Request, number>();
    const record = (event: string, detail: object) => events.push({ time: Date.now(), event, ...detail });
    const detail = (request: Request) => ({
      id: ids.get(request), method: request.method(),
      url: new URL(request.url()).origin + new URL(request.url()).pathname,
      resourceType: request.resourceType(), navigation: request.isNavigationRequest(),
      origin: request.headers().origin ?? null,
      preflightMethod: request.headers()["access-control-request-method"] ?? null,
      preflightHeaders: request.headers()["access-control-request-headers"] ?? null,
    });
    page.on("request", (request) => {
      ids.set(request, ids.size + 1);
      record("request", detail(request));
    });
    page.on("requestfailed", (request) => record("requestfailed", {
      ...detail(request), failure: request.failure(),
    }));
    page.on("requestfinished", (request) => record("requestfinished", detail(request)));
    page.on("response", (response) => {
      const headers = response.headers();
      record("response", {
        ...detail(response.request()), status: response.status(),
        serverId: headers["x-e2e-request-id"] ?? null,
        allowOrigin: headers["access-control-allow-origin"] ?? null,
        allowCredentials: headers["access-control-allow-credentials"] ?? null,
        allowMethods: headers["access-control-allow-methods"] ?? null,
        allowHeaders: headers["access-control-allow-headers"] ?? null,
      });
    });
    page.on("framenavigated", (frame) => {
      if (frame === page.mainFrame()) record("navigation", { url: new URL(frame.url()).pathname });
    });
    page.on("pageerror", (error) => record("pageerror", { message: error.message, stack: error.stack }));
    page.on("console", (message) => {
      if (message.type() === "error") record("console.error", { message: message.text() });
    });
    try {
      await use(page);
    } finally {
      const response = await page.request.get("/__e2e/request-lifecycle", {
        headers: { "x-e2e-lifecycle": scope },
      });
      await testInfo.attach("request-lifecycle", {
        body: JSON.stringify({ browser: events, server: await response.json() }, null, 2),
        contentType: "application/json",
      });
    }
  },
});


// ADR 0114 slice 1 — a full browser-storage quota never crashes the console.
//
// On 2026-09-27 the console fell through to AppCrash with "The quota has been exceeded.",
// and Reload looped straight back: zustand's persist called localStorage.setItem inside
// `set()` with no catch, and useUI setters run in mount effects. Every storage write now goes
// through lib/storage.ts, which never throws.

test("boots, navigates and reloads with localStorage filled to the real quota", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  // Fill THIS origin's real localStorage until the browser refuses, before any app script
  // runs. Once per tab (sessionStorage marker), so the reload below boots on the full store.
  await page.addInitScript(() => {
    if (sessionStorage.getItem("e2e.filled")) return;
    sessionStorage.setItem("e2e.filled", "1");
    let n = 0;
    for (let chunk = 1 << 20; chunk >= 64; chunk >>= 1) {
      const s = "x".repeat(chunk);
      for (;;) {
        try {
          localStorage.setItem(`e2e.fill.${n++}`, s);
        } catch {
          break; // this size no longer fits — halve it
        }
      }
    }
  });

  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await expect(composer).toBeVisible();
  await expect(page.locator(".app-crash")).toHaveCount(0);

  // The store really is full: a raw write throws a quota error.
  const rawWrite = await page.evaluate(() => {
    try {
      localStorage.setItem("e2e.probe", "y".repeat(4096));
      return "stored";
    } catch (e) {
      return (e as Error).name;
    }
  });
  expect(rawWrite).toBe("QuotaExceededError");
  // Chats save to IndexedDB (epic #4981), so a full localStorage only stops layout and
  // preferences saving: the banner says exactly that, not that messages aren't saved.
  await expect(page.getByText("Some console settings aren't being saved in this browser", { exact: true })).toBeVisible();

  // Layout setters (useUI) and a chat turn all persist — every write fails, none throws.
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await page.keyboard.press("Escape");
  const feedback = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/feedback");
  await composer.fill("hello from a full quota");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user").last()).toContainText("hello from a full quota");

  // Finish the turn before exercising persisted-state recovery. Reloading immediately
  // after the optimistic user row cancels the stream and starts feedback during unload;
  // the isolated engine control below covers that diagnostic separately.
  await expect(page.locator(".pl-message--assistant .markdown")).toContainText("Done — found 8 results.");
  expect(await (await feedback).finished()).toBeNull();

  // The old failure mode was a crash LOOP — repeated Reload must boot too.
  for (let reload = 0; reload < 10; reload++) {
    // Dwell on the restored console: its composer paints before session hydration
    // effects finish. Back-to-back document destruction tests the engine control.
    await page.waitForTimeout(1000);
    await page.reload({ waitUntil: "load" });
    await expect(composer).toBeVisible();
    await expect(page.locator(".pl-message--user").last()).toContainText("hello from a full quota");
  }
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  await expect(page.locator(".app-crash")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("the simulateQuotaBytes dev flag fails writes without crashing", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  // A 1 KB "quota" the pre-seeded key already exceeds. The knob is gated like the Developer
  // panel (ADR 0068): this is a production build, so it goes live once /api/flags reports the
  // mock's dev channel — every seam write to localStorage fails from then on.
  await page.addInitScript(() => localStorage.setItem("e2e.seed", "z".repeat(2048)));
  const flags = page.waitForResponse((r) => r.url().includes("/api/flags"));
  await page.goto("/app/?flag:storage.simulateQuotaBytes=1024", { waitUntil: "load" });
  await flags;
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await expect(composer).toBeVisible();
  // Sending records input history (a seam write) — it must fail quietly, not crash.
  await composer.fill("remember me?");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user").last()).toContainText("remember me?");
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await expect(page.locator(".app-crash")).toHaveCount(0);
  const history = await page.evaluate(() => localStorage.getItem("protoagent.chat.inputHistory"));
  expect(history).toBeNull(); // the flag really gated the seam
  expect(errors).toEqual([]);
});

test("a render-time quota crash recovers through Free up space & reload", async ({ page }) => {
  // A >64 KB localStorage transcript (the realistic hog) plus keys the recovery must NOT
  // touch. While the hog exists, the e2e hook forces a render-time quota throw — so the crash
  // reproduces on every load until the recovery actually clears it (a Reload loop). The hook
  // is gated like the Developer panel; the mock's dev channel turns it on.
  //
  // The hog is a CORRUPT palette value, not a well-formed transcript: chat transcripts and
  // palette/DM threads move to IndexedDB at boot (epic #4981 S2/S3), so a valid one is gone
  // from localStorage before anything renders. A malformed leftover the migration can't read
  // stays, is still a transcript-category key, and is exactly what Free up space must clear.
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("e2e.seeded")) {
      sessionStorage.setItem("e2e.seeded", "1");
      const big = "lorem ipsum ".repeat(8000); // ~96 KB of characters
      localStorage.setItem(
        "protoagent.palette.chat",
        JSON.stringify({ contextId: "c1", messages: big }), // `messages` isn't a list: unreadable
      );
      localStorage.setItem("protoagent.chat.sessions.dismissed", JSON.stringify(["kept"]));
      localStorage.setItem("protoagent.keybindings", JSON.stringify({ state: { overrides: {} }, version: 0 }));
    }
    if (localStorage.getItem("protoagent.palette.chat")?.includes("lorem ipsum")) {
      (window as unknown as { __protoagentForceQuotaCrash?: boolean }).__protoagentForceQuotaCrash = true;
    }
  });

  await page.goto("/app/", { waitUntil: "load" });
  const crash = page.locator(".app-crash");
  await expect(crash).toBeVisible();
  await expect(crash).toContainText("The quota has been exceeded.");

  // Plain Reload loops back — the bug's shape.
  await crash.getByRole("button", { name: /^Reload$/ }).click();
  await expect(page.locator(".app-crash")).toBeVisible();

  await page.locator(".app-crash").getByRole("button", { name: /Free up space/ }).click();

  // Reloaded into a working console (freeing the >64 KB hog reloads; it doesn't stop at the
  // largest-keys list).
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  await expect(page.locator(".app-crash")).toHaveCount(0);
  const kept = await page.evaluate(() => ({
    dismissed: localStorage.getItem("protoagent.chat.sessions.dismissed"),
    keybindings: localStorage.getItem("protoagent.keybindings"),
    palette: localStorage.getItem("protoagent.palette.chat") ?? "",
  }));
  expect(kept.dismissed).toBe(JSON.stringify(["kept"]));
  expect(kept.keybindings).not.toBeNull();
  // The hog is gone, and the crashed page's unload flush did NOT write it back.
  expect(kept.palette).not.toContain("lorem ipsum");
});

for (const channel of ["beta", "prod"]) {
  test(`${channel} ignores destructive storage QA hooks`, async ({ page }) => {
    await page.route("**/api/flags", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ response, json: { ...body, channel } });
    });
    await page.addInitScript(() => {
      (window as unknown as { __protoagentForceQuotaCrash?: boolean }).__protoagentForceQuotaCrash = true;
    });
    const flags = page.waitForResponse((r) => r.url().includes("/api/flags"));
    await page.goto("/app/?flag:storage.simulateQuotaBytes=0");
    await flags;
    const composer = page.getByPlaceholder(/Message protoAgent/i);
    await expect(composer).toBeVisible();
    await composer.fill("QA hooks must stay off");
    await composer.press("Enter");
    await expect(page.locator(".pl-message--user").last()).toContainText("QA hooks must stay off");
    await expect(page.locator(".app-crash")).toHaveCount(0);
    const history = await page.evaluate(() => localStorage.getItem("protoagent.chat.inputHistory"));
    expect(history).toContain("QA hooks must stay off");
  });
}

test("a claimed pairing token that cannot be stored shows recovery before the console", async ({ page }) => {
  await page.route("**/api/pairing/claim", (route) => route.fulfill({
    json: { token: "new-device-token", device: { id: "new-device" } },
  }));
  await page.addInitScript(() => {
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      // The device token's own key (#4336 S3) — what a claim writes strictly.
      if (key === "protoagent.deviceToken") throw new DOMException("The quota has been exceeded.", "QuotaExceededError");
      return setItem.call(this, key, value);
    };
  });
  await page.goto("/app/#pair=one-use-code");
  await expect(page.getByRole("heading", { name: "Device pairing failed" })).toBeVisible();
  await expect(page.getByText(/request a new pairing code/)).toBeVisible();
  expect(new URL(page.url()).hash).toBe("");
  expect(await page.evaluate(() => localStorage.getItem("protoagent.deviceToken"))).toBeNull();
  expect(await page.evaluate(() => localStorage.getItem("protoagent.authToken"))).toBeNull();
  await page.getByRole("button", { name: "Continue to console" }).click();
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});


// This is an engine characterization, not an exception to the console's zero-error
// assertions. WebKit emits a pageerror even with a rejection handler, before the
// fetch enters the network lifecycle. Native WKWebView still needs device acceptance.
test("classifies a handled unload fetch independently of the console", async ({ page, browserName }) => {
  const errors: string[] = [];
  const requests: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => requests.push(new URL(request.url()).pathname));
  await page.goto("/__e2e/unload-fetch");
  // Establish that the same-origin endpoint works while the document is alive.
  expect(await page.evaluate(async () => (await fetch("/__e2e/unload-target")).json())).toEqual({ ok: true });
  requests.length = 0;
  await page.goto("/__e2e/unload-target");
  if (browserName === "webkit") {
    expect(errors).toHaveLength(1);
    expect(errors[0]).toMatch(/\/__e2e\/unload-target.*due to access control checks/);
    // Only the destination document reaches the network, not the pagehide fetch.
    expect(requests).toEqual(["/__e2e/unload-target"]);
  } else {
    expect(errors).toEqual([]);
  }
  const lifecycle = await page.evaluate(async () => (await fetch("/__e2e/request-lifecycle")).json());
  expect(lifecycle).toEqual(expect.arrayContaining([
    expect.objectContaining({ pathname: "/__e2e/unload-target", event: "finish", status: 200, finished: true }),
  ]));
  expect(lifecycle.every((event: { allowOrigin: unknown }) => event.allowOrigin === null)).toBe(true);
  const isolated = await page.request.get("/__e2e/request-lifecycle", {
    headers: { "x-e2e-lifecycle": randomUUID() },
  });
  expect(await isolated.json()).toEqual([]);
});

test("migrates all 2400 legacy messages and retains them through ten reloads", async ({ page }) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    if (sessionStorage.getItem("e2e.migrated")) return;
    sessionStorage.setItem("e2e.migrated", "1");
    localStorage.setItem("protoagent.chat.sessions", JSON.stringify({
      version: 1, currentSessionId: "migration-qa",
      sessions: [{
        id: "migration-qa", title: "Migration QA", createdAt: 1, updatedAt: 1,
        messages: Array.from({ length: 2400 }, (_, i) => ({
          id: `legacy-${i}`, role: "user", status: "done", content: `message ${i}`,
        })),
      }],
    }));
  });
  const attendance = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/chat/attend");
  const python = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/runtime/python");
  await page.goto("/app/");
  expect((await attendance).headers()["content-type"]).toBe("text/event-stream");
  expect((await python).status()).toBe(200);
  const expected = Array.from({ length: 2400 }, (_, i) => ({ id: `legacy-${i}`, content: `message ${i}` }));
  for (let reload = 0; reload <= 10; reload++) {
    await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
    await expect.poll(() => page.evaluate(() => localStorage.getItem("protoagent.chat.sessions"))).toBeNull();
    const messages = await page.evaluate(async () => {
      const db = await new Promise<IDBDatabase>((resolve, reject) => {
        const request = indexedDB.open("protoagent-transcripts");
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      try {
        const tx = db.transaction(["sessions", "chunks"], "readonly");
        type Message = { id: string; content: string };
        type Session = { slug: string; id: string; chunks: string[]; tail: Message[] };
        type Chunk = { slug: string; sessionId: string; hash: string; messages: Message[] };
        const read = <T,>(store: string) => new Promise<T[]>((resolve, reject) => {
          const request = tx.objectStore(store).getAll();
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
        });
        const [sessions, chunks] = await Promise.all([read<Session>("sessions"), read<Chunk>("chunks")]);
        const session = sessions.find((record) => record.slug === "host" && record.id === "migration-qa");
        if (!session) return [];
        return [
          ...session.chunks.flatMap((hash: string) => chunks.find((chunk) =>
            chunk.slug === "host" && chunk.sessionId === "migration-qa" && chunk.hash === hash)?.messages ?? []),
          ...session.tail,
        ].map(({ id, content }) => ({ id, content }));
      } finally {
        db.close();
      }
    });
    expect(messages).toEqual(expected);
    await expect(page.locator(".app-crash")).toHaveCount(0);
    if (reload < 10) {
      await page.waitForTimeout(1000);
      await page.reload();
    }
  }
  expect(errors).toEqual([]);
});
