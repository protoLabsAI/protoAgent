import { expect, test } from "@playwright/test";

// The already-drained steer (#3214). Cancelling a queued message races the agent reading it:
// `DELETE …/steer/{id}` answers `removed: false` when the agent got there first. The console
// must then RESTORE the pending bubble rather than drop it — dropping it would claim a message
// never ran when it is already shaping the reply. Until now the mock always answered
// `removed: true`, so this branch — including the marker-race guard behind it and the notice
// #3212 added to the ↑ path — had no e2e coverage at all.
//
// A steer whose text contains "too late" is remembered by the mock as already drained (same
// shape as the "hold the turn open" sentinel): the spec picks the branch by what it types.
const FIELD = ".chat-session-slot:not([hidden]) .pl-prompt__field";

/** Start a turn the mock holds open and queue one steer that the "agent" has already read. */
async function queueDrainedSteer(page: import("@playwright/test").Page, text: string) {
  await page.goto("/app/", { waitUntil: "load" });
  const composer = page.locator(FIELD);
  await expect(composer).toBeVisible();
  await composer.fill("hold the turn open");
  await composer.press("Enter");
  await expect(page.getByPlaceholder(/Steer the agent/i)).toBeVisible();
  await composer.fill(text);
  await composer.press("Enter");
  await expect(page.locator(".pl-message--queued")).toHaveText(new RegExp(text));
  return composer;
}

test("✕ on a steer the agent already read restores the bubble instead of dropping it", async ({ page }) => {
  await queueDrainedSteer(page, "too late to change this");

  const deleted = page.waitForResponse(
    (r) => r.request().method() === "DELETE" && /\/steer\/[^/]+$/.test(r.url()),
  );
  await page.getByRole("button", { name: "Cancel queued message" }).click();
  expect(await (await deleted).json()).toMatchObject({ removed: false });

  // The message is still on its way to the agent, so its bubble comes back and stays.
  await expect(page.locator(".pl-message--queued")).toHaveCount(1);
  await expect(page.locator(".pl-message--queued")).toHaveText(/too late to change this/);
});

test("↑ on a steer the agent already read restores the bubble, says so, and keeps the text", async ({ page }) => {
  const composer = await queueDrainedSteer(page, "too late to edit this");

  const deleted = page.waitForResponse(
    (r) => r.request().method() === "DELETE" && /\/steer\/[^/]+$/.test(r.url()),
  );
  await composer.press("ArrowUp");
  expect(await (await deleted).json()).toMatchObject({ removed: false });

  // Bubble back (the message still runs), an explicit notice (silently leaving a duplicate is
  // the failure mode this avoids), and the pulled text left in the composer — destroying an
  // operator's in-hand edit to undo our own optimism would be worse than the duplicate.
  await expect(page.locator(".pl-message--queued")).toHaveCount(1);
  await expect(page.locator(".pl-toast")).toContainText(/already read that message/i);
  await expect(composer).toHaveValue("too late to edit this");
});
