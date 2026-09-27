import { expect, test } from "@playwright/test";

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

  // Layout setters (useUI) and a chat turn all persist — every write fails, none throws.
  await page.getByTestId("settings-widget").click();
  await expect(page.locator(".settings-overlay")).toBeVisible();
  await page.keyboard.press("Escape");
  await composer.fill("hello from a full quota");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--user").last()).toContainText("hello from a full quota");

  // The old failure mode was a crash LOOP — Reload must boot too.
  await page.reload({ waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  await expect(page.locator(".app-crash")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("the simulateQuotaBytes dev flag fails writes without crashing", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  // A 1 KB "quota" the pre-seeded key already exceeds. The knob is gated like the Developer
  // panel (ADR 0068): this is a production build, so it goes live once /api/flags reports the
  // mock's non-prod channel — every seam write to localStorage fails from then on.
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
  // A >64 KB saved transcript (the realistic hog) plus keys the recovery must NOT touch.
  // While the transcript key exists, the e2e hook forces a render-time quota throw — so the
  // crash reproduces on every load until the recovery actually clears it (a Reload loop). The
  // hook is gated like the Developer panel; the mock's non-prod channel turns it on.
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("e2e.seeded")) {
      sessionStorage.setItem("e2e.seeded", "1");
      const big = "lorem ipsum ".repeat(8000); // ~96 KB of characters
      const blob = {
        version: 1,
        currentSessionId: "s-big",
        sessions: [
          {
            id: "s-big",
            title: "A very long chat",
            createdAt: Date.now(),
            updatedAt: Date.now(),
            messages: [{ id: "a1", role: "assistant", content: big, status: "done" }],
          },
        ],
      };
      localStorage.setItem("protoagent.chat.sessions", JSON.stringify(blob));
      localStorage.setItem("protoagent.palette.chat", JSON.stringify({ contextId: "c1", messages: [] }));
      localStorage.setItem("protoagent.chat.sessions.dismissed", JSON.stringify(["kept"]));
      localStorage.setItem("protoagent.keybindings", JSON.stringify({ state: { overrides: {} }, version: 0 }));
    }
    if (localStorage.getItem("protoagent.chat.sessions")) {
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

  // Reloaded into a working console.
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
  await expect(page.locator(".app-crash")).toHaveCount(0);
  const kept = await page.evaluate(() => ({
    dismissed: localStorage.getItem("protoagent.chat.sessions.dismissed"),
    keybindings: localStorage.getItem("protoagent.keybindings"),
    palette: localStorage.getItem("protoagent.palette.chat"),
  }));
  expect(kept.dismissed).toBe(JSON.stringify(["kept"]));
  expect(kept.keybindings).not.toBeNull();
  expect(kept.palette).toBeNull(); // palette threads are transcripts too
  // The crashed page's unload flush did NOT write the old transcript back.
  const transcript = await page.evaluate(() => localStorage.getItem("protoagent.chat.sessions") ?? "");
  expect(transcript).not.toContain("lorem ipsum");
});
