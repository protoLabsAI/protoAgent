import { expect, test } from "@playwright/test";

// Reattach vs a PAUSED turn (#3082, #3930): after a fleet agent switch / reload / a fresh
// browser, a turn parked on operator input (input-required — a pending HITL form or
// approval gate) must come back USABLE, immediately.
//
// The mock mirrors the REAL server (a2a-sdk 1.1.5): `SubscribeToTask` on an input-required
// task answers with the Task snapshot and then HOLDS THE STREAM OPEN — spec-correct, since
// input-required is interrupted, not terminal, and a subscription ends only at a terminal
// state (A2A §3.1.6). The mock used to REJECT the resubscribe ("as the real server does for
// non-running tasks" — it doesn't), which routed the console onto the GetTask fallback and
// hid #3930: the real console waited on the held stream to close, holding the session
// "streaming" (Stop up, the card's buttons disabled) for as long as the form went
// unanswered. The fix: the reattach settles as paused off the snapshot itself.
//
// Mirrors chat-reconcile.spec.ts: seed a stuck `streaming` session carrying the paused
// task's id (the mock keys "paused" task ids to the parked snapshot).

const SLOT = ".chat-session-slot:not([hidden])";

test("a reattached paused turn re-renders its approval gate with live buttons", async ({ page }) => {
  await page.addInitScript(() => {
    const stuck = {
      version: 1,
      currentSessionId: "s-stuck",
      sessions: [
        {
          id: "s-stuck",
          title: "Interrupted turn",
          createdAt: Date.now(),
          updatedAt: Date.now(),
          messages: [
            { id: "u1", role: "user", content: "deploy the release", status: "done" },
            // Stuck mid-turn: still "streaming", carrying the paused task's id.
            { id: "a1", role: "assistant", content: "", status: "streaming", taskId: "task-stuck-paused-1" },
          ],
        },
      ],
    };
    window.localStorage.setItem("protoagent.chat.sessions", JSON.stringify(stuck));
  });

  // The reattach must take the REAL path: a held-open subscription, not a rejected one.
  const subscribed = page.waitForRequest(
    (req) =>
      req.url().endsWith("/a2a") &&
      (req.postData() ?? "").includes("SubscribeToTask") &&
      (req.postData() ?? "").includes("task-stuck-paused-1"),
  );

  await page.goto("/app/", { waitUntil: "load" });
  await subscribed;

  // The subscription snapshot re-renders the pending approval gate…
  const card = page.locator(`${SLOT} .hitl-float .hitl-card`);
  await expect(card).toBeVisible();
  await expect(card).toContainText("Approve the deploy?");
  await expect(card).toContainText("kubectl apply -f prod.yaml");

  // …and it is ACTIONABLE straight away, although the server never closes the stream: the
  // session is not held "streaming" (no Stop), so the buttons are live. A short timeout on
  // purpose — before the fix this waited on the held stream forever. Approve matched
  // exactly so "Approve & don't ask again" can't satisfy it by accident.
  await expect(page.locator(SLOT).getByRole("button", { name: "Stop", exact: true })).toHaveCount(0, {
    timeout: 3_000,
  });
  await expect(card.getByRole("button", { name: "Approve", exact: true })).toBeEnabled({ timeout: 3_000 });
  await expect(card.getByRole("button", { name: "Deny" })).toBeEnabled({ timeout: 3_000 });
});
