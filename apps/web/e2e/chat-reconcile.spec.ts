import { expect, test } from "@playwright/test";

// Stuck-stream self-heal (reconcile): a chat message left in `streaming` after an
// interrupted stream (reload / network / a stale tab) must reconcile against the
// server's durable task on load — A2A tasks/get — and finalize, instead of
// spinning forever. We seed a stuck session into localStorage; the mock serves a
// completed task carrying the answer.

test("a stuck 'streaming' message reconciles to the server's completed answer on load", async ({ page }) => {
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
            { id: "u1", role: "user", content: "what's the answer?", status: "done" },
            // assistant turn whose stream was cut off mid-flight: still "streaming",
            // carries the A2A task id the reconcile will query.
            { id: "a1", role: "assistant", content: "", status: "streaming", taskId: "task-stuck-1" },
          ],
        },
      ],
    };
    window.localStorage.setItem("protoagent.chat.sessions", JSON.stringify(stuck));
  });

  await page.goto("/app/", { waitUntil: "load" });

  // On mount the reconcile fires: tasks/get → completed → finalize. The answer
  // from the server's artifact appears, and the message is no longer streaming.
  await expect(page.locator(".pl-message--assistant")).toContainText("RECONCILED ANSWER");
  // No longer spinning — the streaming loader is gone once finalized.
  await expect(page.locator(".pl-message--assistant .spin")).toHaveCount(0);
});
