import { expect, test } from "@playwright/test";

// Reattach vs a PAUSED turn (#3082): after a fleet agent switch / reload, a turn
// parked on operator input (input-required — a pending HITL form or approval
// gate) must come back USABLE. The old reattach poller treated paused states as
// in-flight and spun its full 10-minute budget with the session stuck
// "streaming" — which fed HitlForm busy={true} and disabled every button. The
// fix: the poller stops immediately on a paused state and returns the session
// to idle WITHOUT finalizing the message (the server still owns the turn).
//
// Mirrors chat-reconcile.spec.ts: seed a stuck `streaming` session; the mock's
// GetTask serves an input-required task (id carries "paused") whose status
// message holds the approval payload. Resubscribe is rejected (as the real
// server does for non-running tasks), so this exercises the fallback-poll path.

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

  await page.goto("/app/", { waitUntil: "load" });

  // The snapshot replay re-renders the pending approval gate…
  const card = page.locator(".chat-session-slot:not([hidden]) .hitl-float .hitl-card");
  await expect(card).toBeVisible();
  await expect(card).toContainText("Approve the deploy?");
  await expect(card).toContainText("kubectl apply -f prod.yaml");

  // …and its buttons are LIVE: the poller went idle instead of holding the
  // session "streaming" (busy) for its 10-minute budget. Approve matched
  // exactly so "Approve & don't ask again" can't satisfy it by accident.
  await expect(card.getByRole("button", { name: "Approve", exact: true })).toBeEnabled();
  await expect(card.getByRole("button", { name: "Deny" })).toBeEnabled();
});
