import { expect, test } from "@playwright/test";

// Mid-turn steering ✕-cancel (#1103, shipped in #1104). While a turn streams, a
// message the user submits is QUEUED as a steer (folds into the agent's work at its
// next model call) and shown as a dimmed pending bubble with a ✕. Clicking the ✕
// must dequeue it server-side (DELETE /api/chat/sessions/{id}/steer/{msgId}) and drop
// the bubble — so a cancelled steer never reaches the agent. The DS `Message queued`
// renders `.pl-message--queued` + a "Cancel queued message" button (@protolabsai/ui).

test("✕ on a queued steer dequeues it and removes the pending bubble", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await expect(composer).toBeVisible();

  // Start a turn the mock HOLDS OPEN, so the surface stays "streaming" — the state
  // where Enter queues a steer instead of sending a fresh turn.
  await composer.fill("hold the turn open");
  await composer.press("Enter");

  // The composer flips to its steering placeholder once the turn is running.
  const steerComposer = page.getByPlaceholder(/Steer the agent/i);
  await expect(steerComposer).toBeVisible();

  // Queue a steer → an optimistic dimmed bubble with a ✕ appears.
  await steerComposer.fill("actually, do X instead");
  await steerComposer.press("Enter");
  const queued = page.locator(".pl-message--queued");
  await expect(queued).toHaveText(/do X instead/);

  // Clicking ✕ must hit the dequeue endpoint — prove it, not just the optimistic drop.
  const deleted = page.waitForRequest(
    (r) => r.method() === "DELETE" && /\/api\/chat\/sessions\/[^/]+\/steer\/[^/]+$/.test(r.url()),
  );
  await page.getByRole("button", { name: "Cancel queued message" }).click();
  await deleted;

  // The bubble is gone — the steer was cancelled before the agent saw it.
  await expect(page.locator(".pl-message--queued")).toHaveCount(0);
});
