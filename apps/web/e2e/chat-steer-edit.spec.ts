import { expect, test } from "@playwright/test";

// ↑-to-edit a queued steer (#2837 follow-up). The composer promises "Press ↑ to edit queued
// message"; ↑ must PULL that message out of the running turn — dequeue it server-side
// (DELETE /api/chat/sessions/{id}/steer/{msgId}, the same call the bubble's ✕ makes), drop
// the pending bubble, and land the text in the composer — so the edit REPLACES the queued
// message instead of the agent receiving both. Before the fix ↑ only recalled a copy from
// the input-history ring and the original stayed queued.
//
// The composer's placeholder flips between three strings (composerPlaceholder.ts), so the
// field is addressed by its DS class instead — a placeholder locator would stop resolving
// the moment the queue drains.
const FIELD = ".chat-session-slot:not([hidden]) .pl-prompt__field";

test("↑ pulls the queued message out of the turn and into the composer", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.locator(FIELD);
  await expect(composer).toBeVisible();

  // A turn the mock HOLDS OPEN — the state where Enter queues a steer.
  await composer.fill("hold the turn open");
  await composer.press("Enter");
  await expect(page.getByPlaceholder(/Steer the agent/i)).toBeVisible();

  // Queue a steer, then confirm the hint that advertises ↑ is actually showing.
  await composer.fill("actually, do X instead");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--queued")).toHaveText(/do X instead/);
  await expect(page.getByPlaceholder(/Press ↑ to edit queued message/i)).toBeVisible();

  // ↑ must hit the dequeue endpoint — prove it, not just the optimistic bubble drop.
  const deleted = page.waitForRequest(
    (r) => r.method() === "DELETE" && /\/api\/chat\/sessions\/[^/]+\/steer\/[^/]+$/.test(r.url()),
  );
  await composer.press("ArrowUp");
  await deleted;

  // Out of the turn, into the composer, editable.
  await expect(page.locator(".pl-message--queued")).toHaveCount(0);
  await expect(composer).toHaveValue("actually, do X instead");

  // Re-sending queues the EDITED text — and only that: the original is gone, not doubled.
  await composer.press("End");
  await composer.type(", not Y");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--queued")).toHaveCount(1);
  await expect(page.locator(".pl-message--queued")).toHaveText(/do X instead, not Y/);
});

test("↑ still walks input history once something is typed", async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.locator(FIELD);
  await expect(composer).toBeVisible();

  await composer.fill("hold the turn open");
  await composer.press("Enter");
  await expect(page.getByPlaceholder(/Steer the agent/i)).toBeVisible();
  await composer.fill("queued one");
  await composer.press("Enter");
  await expect(page.locator(".pl-message--queued")).toHaveText(/queued one/);

  // A draft in progress owns ↑ (readline history nav): the queued message stays queued and
  // the ring answers instead — the pull would otherwise clobber what the operator is typing.
  await composer.fill("half a thought");
  await composer.press("ArrowUp");
  await expect(composer).toHaveValue("queued one"); // the ring's newest entry, not a pull
  await expect(page.locator(".pl-message--queued")).toHaveCount(1);
});
