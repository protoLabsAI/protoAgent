import { expect, test } from "@playwright/test";

// #2009 — a chat tab reflects a BACKGROUND server-initiated turn (push-resume / scheduled /
// watch reaction) as a pulsing "processing" dot, since those turns don't touch the
// foreground streaming state (sessionStatusMap) and would otherwise leave the tab reading
// idle. The mock's /api/events stream emits `turn.started` then `turn.finished` for the
// session named by the `x-e2e-turn-session` header (set below), so this drives the real
// bus → ServerTurnWatch → server-turn-store → tab-dot path end to end.

test.use({ extraHTTPHeaders: { "x-e2e-turn-session": "e2e-proc-session" } });

test("a background server turn lights the tab's processing dot, then clears when it finishes", async ({ page }) => {
  await page.addInitScript(() => {
    const session = {
      version: 1,
      currentSessionId: "e2e-proc-session",
      sessions: [
        { id: "e2e-proc-session", title: "Nightly report", createdAt: 1, updatedAt: 2, messages: [] },
      ],
    };
    window.localStorage.setItem("protoagent.chat.sessions", JSON.stringify(session));
  });
  await page.goto("/app/", { waitUntil: "load" });

  const dot = page.locator(".pl-tabbar__tab .session-dot.processing");
  // `turn.started` (t≈300ms) arms it; the assertion polls until it appears.
  await expect(dot).toBeVisible({ timeout: 5_000 });
  // `turn.finished` (t≈2500ms) clears it — no stale/stuck processing state.
  await expect(dot).toHaveCount(0, { timeout: 6_000 });
});
