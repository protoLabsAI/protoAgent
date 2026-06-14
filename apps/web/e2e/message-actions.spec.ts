import { expect, test } from "@playwright/test";

// The DS Message toolbar adopted in bd-1rn: copy, fork-from-here, and
// regenerate, shown on a settled assistant message (hover-revealed). Drives the
// canned mock turn (default scenario answers "Done — found 8 results.").
test.use({ permissions: ["clipboard-read", "clipboard-write"] });

async function send(page, prompt: string) {
  const composer = page.getByPlaceholder(/Message protoAgent/i);
  await composer.waitFor({ state: "visible" });
  await composer.fill(prompt);
  await composer.press("Enter");
}

// The active chat slot — fork spawns a second, hidden slot, so scope queries here.
const visibleSlot = (page) => page.locator(".chat-session-slot:not([hidden])");

test.beforeEach(async ({ page }) => {
  await page.goto("/app/", { waitUntil: "load" });
  await expect(page.getByPlaceholder(/Message protoAgent/i)).toBeVisible();
});

test("copy action writes the assistant answer to the clipboard", async ({ page }) => {
  await send(page, "hello there");
  const assistant = visibleSlot(page).locator(".pl-message--assistant").last();
  await expect(assistant.locator(".markdown")).toContainText("Done — found 8 results.");

  await assistant.getByRole("button", { name: "Copy" }).click();
  await expect(assistant.getByRole("button", { name: "Copied" })).toBeVisible();

  const clip = await page.evaluate(() => navigator.clipboard.readText());
  expect(clip).toContain("Done — found 8 results.");
});

test("fork opens a new tab seeded with the history through that message", async ({ page }) => {
  await send(page, "remember this");
  const assistant = visibleSlot(page).locator(".pl-message--assistant").last();
  await expect(assistant.locator(".markdown")).toContainText("Done — found 8 results.");
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(1);

  await assistant.getByRole("button", { name: "Fork from here" }).click();

  // A new tab is added and becomes active, seeded with the same history; the
  // original is untouched (now two tabs).
  await expect(page.locator(".pl-tabbar__tab")).toHaveCount(2);
  await expect(visibleSlot(page).locator(".pl-message--user")).toHaveText(/remember this/);
  await expect(visibleSlot(page).locator(".pl-message--assistant .markdown")).toContainText(
    "Done — found 8 results.",
  );
});

test("regenerate re-runs the last turn without duplicating the user bubble", async ({ page }) => {
  await send(page, "hello there");
  const slot = visibleSlot(page);
  await expect(slot.locator(".pl-message--assistant .markdown")).toContainText("Done — found 8 results.");
  await expect(slot.locator(".pl-message--user")).toHaveCount(1);

  await slot.locator(".pl-message--assistant").last().getByRole("button", { name: "Regenerate" }).click();

  // Exactly one user bubble still (the `hidden` re-run path adds no duplicate),
  // and a fresh assistant answer comes back.
  await expect(slot.locator(".pl-message--user")).toHaveCount(1);
  await expect(slot.locator(".pl-message--assistant .markdown").last()).toContainText(
    "Done — found 8 results.",
  );
});
